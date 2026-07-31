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

**Repeat calls within one run are served from a cache.** Models re-ask the
same question with cosmetically different wording - observed in testing as six
`search_travel_guide` calls for the same city inside a single step, each a full
four-hop retrieval returning several kilobytes. They cannot return anything
new, but they consume the tokens-per-minute allowance the rest of the run
needs, and on a free tier that is what actually fails a request.

Exact-argument caching does not stop that, precisely because the wording
varies. So a tool may declare `cache_on=(...)`: the arguments that genuinely
determine its result. `search_travel_guide` caches on destination and intent,
because rephrasing the question does not change which city gets retrieved.
Tools without `cache_on` fall back to matching all arguments. The cache lives
for exactly one run.
"""

from __future__ import annotations

import functools
import inspect
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from enum import StrEnum
from typing import Any, ParamSpec, TypeVar

from pydantic import BaseModel, Field

from app.core.errors import ExternalServiceError, RateLimitError, ToolExecutionError
from app.core.logging import get_logger
from app.services.facts import record_fact
from app.services.progress import emit, label_for_tool

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

# Per-run memoisation of tool results, keyed by (tool name, arguments). Also
# context-local, so one request's cache can never serve another's - which
# matters because `recall_user_preferences` results are user-specific.
_result_cache: ContextVar[dict[str, ToolResult] | None] = ContextVar(
    "tool_result_cache", default=None
)


def start_recording() -> list[ToolCallRecord]:
    """Begin collecting tool-call records for the current context.

    Returns:
        The list that will accumulate records. The caller holds the reference
        and reads it once the agent run finishes.
    """
    records: list[ToolCallRecord] = []
    _recorder.set(records)
    _result_cache.set({})
    return records


def start_step_recording() -> list[ToolCallRecord]:
    """Give the calling task its own recorder, sharing the run's result cache.

    For steps running concurrently. The trace attributes tool calls to steps by
    slicing the shared list between a before and after length, which silently
    mis-attributes as soon as two steps interleave - "weather" would appear
    under whichever step happened to be measured around it.

    Called inside a task, `set` rebinds only that task's context, so each step
    accumulates its own records and the caller merges them back in step order.

    The result cache is deliberately *not* reset. It is a dict held in a
    context variable, so tasks inherit the same object by reference and a guide
    article fetched by one step is still free for its siblings - which is worth
    more here than anywhere, since concurrent steps about one destination are
    exactly the ones likely to ask for the same thing.

    Returns:
        The list this task will accumulate into.
    """
    records: list[ToolCallRecord] = []
    _recorder.set(records)
    return records


def stop_recording() -> None:
    """Stop collecting tool-call records and discard the per-run result cache."""
    _recorder.set(None)
    _result_cache.set(None)


def _bind_arguments(
    func: Callable[..., Awaitable[ToolResult]], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Resolve a call's positional and keyword arguments to one name-keyed dict.

    This is the fix for a bug that shipped once already: the trace recorder
    and the cache key both originally read `**kwargs` only, on the assumption
    that every tool would always be called with keywords. `registry.py` called
    every tool positionally instead, so the trace silently recorded `{}` for
    every single call and the result cache never had a stable key to hit.

    Binding against the function's actual signature - via `inspect.signature`,
    the same mechanism Python itself uses to resolve a call - makes the
    downstream code correct regardless of how the caller invokes it, rather
    than relying on every present and future call site to happen to use
    keywords. That is a structurally safer fix than fixing the one call site
    that broke it this time.

    Args:
        func: The wrapped tool function.
        args: Positional arguments from the actual call.
        kwargs: Keyword arguments from the actual call.

    Returns:
        Every supplied argument, keyed by parameter name. Falls back to an
        empty dict if binding fails for some unanticipated reason - a
        degraded trace is preferable to a broken tool call.
    """
    try:
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
    except TypeError:  # pragma: no cover - defensive; a real call always binds
        return {}
    return dict(bound.arguments)


def _cache_key(
    tool_name: str,
    bound_arguments: dict[str, Any],
    cache_on: tuple[str, ...] | None = None,
) -> str:
    """Build a stable cache key from a tool call's name and arguments.

    Args:
        tool_name: The tool being called.
        bound_arguments: Every argument, keyed by parameter name regardless of
            how the caller passed it - see `_bind_arguments`.
        cache_on: Parameter names that determine the result. When given, only
            these are used, so cosmetic rewording of the others still hits
            the cache. This is what makes `search_travel_guide` cache on
            destination alone: a model rewording its `question` on every call
            must not defeat caching aimed at exactly that rewording.

    Returns:
        A cache key.
    """
    import json

    if cache_on:
        identifying = {
            name: str(bound_arguments.get(name, "")).strip().lower() for name in cache_on
        }
        return f"{tool_name}:{json.dumps(identifying, sort_keys=True)}"

    try:
        rendered = json.dumps(bound_arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        rendered = str(sorted(bound_arguments.items(), key=str))
    return f"{tool_name}:{rendered}"


def _record(record: ToolCallRecord) -> None:
    """Append a record if collection is active, otherwise do nothing."""
    records = _recorder.get()
    if records is not None:
        records.append(record)

    # Announced on completion rather than on start because that is where the
    # record already exists - and a tool that finished in 200ms would
    # otherwise flash a "looking up..." line the eye cannot read anyway. The
    # slow ones are the ones worth narrating, and they narrate themselves by
    # being the last line on screen for several seconds.
    emit(
        "tool",
        label_for_tool(record.tool_name),
        detail=None if record.status == "ok" else f"{record.source} unavailable",
        tool=record.tool_name,
        status=record.status,
        latency_ms=record.latency_ms,
    )


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
    cache_on: tuple[str, ...] | None = None,
) -> Callable[[Callable[P, Awaitable[ToolResult]]], Callable[P, Awaitable[ToolResult]]]:
    """Wrap a tool so upstream failures degrade instead of propagating.

    Also times every call and records it for the execution trace.

    Args:
        source: Data source identifier stamped onto results and records.
        unavailable_message: What the model should be told when this
            dependency fails. Must state what is missing *and* what to do -
            see the module docstring for why a bare error string is not
            enough.
        cache_on: Keyword-argument names that determine this tool's result.
            Set it on expensive tools whose output does not change when the
            model rewords a free-text argument. Omit to match on all
            arguments.

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

            bound_arguments = _bind_arguments(func, args, kwargs)
            recordable_arguments = {
                name: value for name, value in bound_arguments.items() if _is_recordable(value)
            }

            cache = _result_cache.get()
            key = _cache_key(tool_name, bound_arguments, cache_on) if cache is not None else ""

            if cache is not None and key in cache:
                cached_result = cache[key].model_copy(update={"cached": True})
                logger.info("tool.cache_hit", tool=tool_name, source=source)
                _record(
                    ToolCallRecord(
                        tool_name=tool_name,
                        arguments=recordable_arguments,
                        status=cached_result.status,
                        source=source,
                        latency_ms=0,
                        result_summary="(served from this run's cache)",
                    )
                )
                return cached_result

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
                    arguments=recordable_arguments,
                    status=result.status,
                    source=source,
                    latency_ms=latency_ms,
                    result_summary=_summarise(result),
                    error_code=error_code,
                    error_message=error_message,
                )
            )

            # Structured payloads are kept for the interface to render, so a
            # forecast or a price reaches the screen as data rather than as a
            # sentence the model wrote about it. Successful calls only: a
            # degraded result has nothing worth showing.
            if result.status is ToolStatus.OK:
                record_fact(tool_name, source, result.data)

            # Only successful results are cached. A degraded result should not
            # pin an outage for the rest of the run - the dependency may
            # recover, and the message already tells the model not to retry.
            if cache is not None and result.status is ToolStatus.OK:
                cache[key] = result

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
