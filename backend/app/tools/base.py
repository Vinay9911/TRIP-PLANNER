"""Tool result envelope and failure handling.

Two problems are solved here, and both are about what a tool does when the
world does not cooperate.

**1. A failing tool must not fail the run.**

The obvious implementation - let the exception propagate - is wrong for an
agent. If the weather API is down while planning two days in Kyoto, the right
outcome is an itinerary without weather advice, not a 502 for the user. So
every tool returns a `ToolResult` instead of raising, and upstream failures
become `status="degraded"` results.

**2. A degraded result must be actionable *by the model*.**

This is the part that is easy to get wrong. Returning `{"error": "timeout"}`
tells the model nothing about what to do next, and models faced with an
unexplained failure tend to either retry the same call forever or invent the
missing data. So every degraded result carries an explicit instruction:

    "Weather data is unavailable for Kyoto. Continue planning without
     weather-specific advice and tell the user that forecasts could not be
     retrieved. Do not call this tool again for this location."

That last sentence prevents the retry loop. The tool docstrings and these
messages are prompt engineering as much as they are error handling, which is
why they are written in full sentences rather than error codes.

Every invocation is also recorded through a context-local collector, so the
agent can persist a complete tool-call trace without threading a recorder
object through every call site.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from enum import StrEnum
from typing import Any, ParamSpec, TypeVar

from pydantic import BaseModel, Field

from app.core.errors import ExternalServiceError, RateLimitError, ToolExecutionError
from app.core.logging import get_logger

logger = get_logger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


class ToolStatus(StrEnum):
    """Outcome of a tool invocation."""

    OK = "ok"
    #: An upstream dependency failed; a usable but incomplete result was
    #: returned so the agent can continue without this information.
    DEGRADED = "degraded"
    #: The model supplied arguments the tool cannot act on. Recoverable by
    #: calling again with corrected arguments.
    INVALID = "invalid"


class ToolResult(BaseModel):
    """Uniform envelope returned by every tool.

    Attributes:
        status: Whether the call succeeded, degraded, or was misused.
        source: Which data source produced this, e.g. `"open-meteo"`. Surfaced
            to the user so an itinerary can be attributed, and used by the
            grounding check to verify claims trace to a real retrieval.
        data: The payload. Shape is tool-specific.
        message: Guidance written for the model. On a degraded result this
            must say what to do instead, including whether to retry.
    """

    status: ToolStatus = ToolStatus.OK
    source: str
    data: Any = None
    message: str = ""
    #: True when the payload came from a cache rather than a live call.
    #: Shown in the admin trace so a suspiciously fast run is explicable.
    cached: bool = False

    @classmethod
    def ok(cls, *, source: str, data: Any, message: str = "") -> ToolResult:
        """Build a successful result.

        Args:
            source: Data source identifier.
            data: The payload.
            message: Optional note for the model.

        Returns:
            A result with `status=OK`.
        """
        return cls(status=ToolStatus.OK, source=source, data=data, message=message)

    @classmethod
    def degraded(cls, *, source: str, message: str, data: Any = None) -> ToolResult:
        """Build a result for an upstream failure the agent should route around.

        Args:
            source: Data source identifier.
            message: Instruction for the model. State what is missing and what
                to do about it, and say explicitly if it should not retry.
            data: Any partial payload worth keeping.

        Returns:
            A result with `status=DEGRADED`.
        """
        return cls(status=ToolStatus.DEGRADED, source=source, data=data, message=message)

    @classmethod
    def invalid(cls, *, source: str, message: str) -> ToolResult:
        """Build a result for arguments the tool cannot act on.

        Args:
            source: Data source identifier.
            message: What was wrong and what a valid argument looks like.
                Phrased so the model can correct itself on the next call.

        Returns:
            A result with `status=INVALID`.
        """
        return cls(status=ToolStatus.INVALID, source=source, data=None, message=message)


class ToolCallRecord(BaseModel):
    """One recorded tool invocation, persisted as part of a run's trace."""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: ToolStatus
    source: str
    latency_ms: int
    result_summary: str = ""
    error_code: str | None = None
    error_message: str | None = None


# Context-local, so concurrent requests each accumulate their own records
# without a shared mutable list or an explicit parameter on every tool.
_recorder: ContextVar[list[ToolCallRecord] | None] = ContextVar("tool_recorder", default=None)


def start_recording() -> list[ToolCallRecord]:
    """Begin collecting tool-call records for the current context.

    Returns:
        The list that will accumulate records. The caller holds the reference
        and reads it once the agent run finishes.
    """
    records: list[ToolCallRecord] = []
    _recorder.set(records)
    return records


def stop_recording() -> None:
    """Stop collecting tool-call records for the current context."""
    _recorder.set(None)


def _record(record: ToolCallRecord) -> None:
    """Append a record if collection is active, otherwise do nothing."""
    records = _recorder.get()
    if records is not None:
        records.append(record)


def _summarise(result: ToolResult, limit: int = 400) -> str:
    """Produce a short, storable description of a result.

    Full tool payloads can be large, and some contain third-party content we
    have no licence to retain. The trace keeps a summary; the complete payload
    stays in the request-scoped log only.

    Args:
        result: The result to summarise.
        limit: Maximum characters to keep.

    Returns:
        A truncated description.
    """
    if result.data is None:
        return result.message[:limit]
    text = str(result.data)
    return text[:limit] + ("..." if len(text) > limit else "")


def resilient_tool(
    *,
    source: str,
    unavailable_message: str,
) -> Callable[[Callable[P, Awaitable[ToolResult]]], Callable[P, Awaitable[ToolResult]]]:
    """Wrap a tool so upstream failures degrade instead of propagating.

    Also times every call and records it for the execution trace.

    Args:
        source: Data source identifier stamped onto results and records.
        unavailable_message: What the model should be told when this
            dependency fails. Must state what is missing *and* what to do -
            see the module docstring for why a bare error string is not
            enough.

    Returns:
        A decorator for an async function returning a `ToolResult`.

    Example:
        >>> @resilient_tool(
        ...     source="open-meteo",
        ...     unavailable_message=(
        ...         "Weather data is unavailable. Continue without weather "
        ...         "advice, tell the user forecasts could not be retrieved, "
        ...         "and do not call this tool again for this location."
        ...     ),
        ... )
        ... async def get_weather_forecast(city: str) -> ToolResult: ...
    """

    def decorator(func: Callable[P, Awaitable[ToolResult]]) -> Callable[P, Awaitable[ToolResult]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> ToolResult:
            started = time.perf_counter()
            tool_name = func.__name__
            error_code: str | None = None
            error_message: str | None = None

            try:
                result = await func(*args, **kwargs)

            except (ExternalServiceError, RateLimitError) as exc:
                # The dependency is down or throttling us. Degrade: the agent
                # keeps its plan and works around the gap.
                error_code, error_message = exc.code, exc.message
                logger.warning(
                    "tool.degraded",
                    tool=tool_name,
                    source=source,
                    error_code=exc.code,
                    error=exc.message,
                )
                result = ToolResult.degraded(source=source, message=unavailable_message)

            except ToolExecutionError as exc:
                # The model called us wrongly. Hand back the reason so it can
                # correct the arguments and try again.
                error_code, error_message = exc.code, exc.message
                logger.info("tool.invalid_arguments", tool=tool_name, error=exc.message)
                result = ToolResult.invalid(source=source, message=exc.message)

            except Exception as exc:
                # An unanticipated bug in our own tool code. Still degrade
                # rather than propagate: one broken tool should not take down
                # a run that five working tools could still answer. Logged
                # with a traceback because, unlike the branches above, this
                # one always indicates a defect worth fixing.
                error_code, error_message = "unexpected_error", str(exc)[:200]
                logger.exception("tool.unexpected_error", tool=tool_name, source=source)
                result = ToolResult.degraded(source=source, message=unavailable_message)

            latency_ms = int((time.perf_counter() - started) * 1000)

            _record(
                ToolCallRecord(
                    tool_name=tool_name,
                    # Positional args are dropped deliberately: every tool in
                    # this project is called with keywords by the model, and
                    # `args` would otherwise capture `self`-like values that
                    # are noise in a trace.
                    arguments={
                        key: value for key, value in kwargs.items() if _is_recordable(value)
                    },
                    status=result.status,
                    source=source,
                    latency_ms=latency_ms,
                    result_summary=_summarise(result),
                    error_code=error_code,
                    error_message=error_message,
                )
            )

            logger.info(
                "tool.completed",
                tool=tool_name,
                source=source,
                status=result.status.value,
                latency_ms=latency_ms,
            )
            return result

        return wrapper

    return decorator


def _is_recordable(value: Any) -> bool:
    """Whether a tool argument is safe and useful to persist in a trace.

    Filters out large or non-serialisable values (injected clients, database
    connections) that would bloat the trace or fail to serialise.

    Args:
        value: The argument value.

    Returns:
        True if the value should be stored.
    """
    if value is None or isinstance(value, bool | int | float):
        return True
    if isinstance(value, str):
        return len(value) <= 500
    if isinstance(value, list | dict):
        return len(str(value)) <= 1000
    return False
