"""Error taxonomy.

The governing rule in this codebase is **a failing tool must not fail the
run**. A weather API timing out should cost the agent one piece of
information, not the entire itinerary. That principle only works if failures
are *typed*, because the correct response differs by kind:

* `ExternalServiceError` - degrade: tell the agent this source is unavailable
  and let it replan around the gap.
* `RateLimitError` - back off and retry, then degrade.
* `ConfigurationError` - fail loudly at startup; retrying cannot help.
* `AuthenticationError` / `AuthorizationError` - reject the request; never
  degrade, because silently continuing would be a security bug.
* `AgentBudgetExceededError` - stop the loop and answer with what we have.

Each error carries an HTTP status and a stable machine-readable `code`, so the
API layer can translate any of them into a consistent JSON envelope without a
chain of `isinstance` checks.
"""

from __future__ import annotations

from typing import Any


class TripPlannerError(Exception):
    """Base class for every error this application raises deliberately.

    Attributes:
        message: Human-readable description, safe to return to a client.
        code: Stable machine-readable identifier for programmatic handling.
        status_code: HTTP status the API layer should respond with.
        details: Structured context for logs and debugging. Must never
            contain secrets - it is serialised into API responses.
        retryable: Whether repeating the same call could plausibly succeed.
    """

    code: str = "internal_error"
    status_code: int = 500
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description.
            details: Structured context to attach to logs and the response.
            retryable: Overrides the class-level default when a specific
                instance is known to be (non-)retryable.
        """
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if retryable is not None:
            self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        """Render the error as the API's JSON error envelope."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "retryable": self.retryable,
            }
        }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ConfigurationError(TripPlannerError):
    """A required setting is missing or invalid.

    Deliberately not retryable: no amount of waiting supplies a missing API
    key. Raised at startup so deployments fail fast rather than on first use.
    """

    code = "configuration_error"
    status_code = 500
    retryable = False


# ---------------------------------------------------------------------------
# External services
# ---------------------------------------------------------------------------


class ExternalServiceError(TripPlannerError):
    """An upstream dependency failed.

    Raised by service clients and caught at the tool boundary, where it is
    converted into a structured "this source was unavailable" result that the
    agent can reason about and route around.
    """

    code = "external_service_error"
    status_code = 502
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        service: str,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description.
            service: Short name of the upstream dependency, e.g. `"tavily"`.
                Recorded on every failure so the admin dashboard can rank
                dependencies by error rate.
            details: Structured context (status code, endpoint, ...).
            retryable: Overrides the default retryability.
        """
        super().__init__(
            message, details={**(details or {}), "service": service}, retryable=retryable
        )
        self.service = service


class ExternalServiceTimeout(ExternalServiceError):
    """An upstream dependency did not respond within its deadline."""

    code = "external_service_timeout"
    status_code = 504
    retryable = True


class RateLimitError(ExternalServiceError):
    """An upstream dependency rejected the call for exceeding a quota.

    Distinguished from a generic failure because the remedy is different:
    back off and retry rather than fail over. Relevant on the Groq free tier,
    where the per-day request ceiling is a real operational limit.
    """

    code = "rate_limited"
    status_code = 429
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        service: str,
        retry_after_seconds: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description.
            service: Short name of the rate-limiting dependency.
            retry_after_seconds: Server-advertised backoff, when provided.
            details: Additional structured context.
        """
        merged = {**(details or {})}
        if retry_after_seconds is not None:
            merged["retry_after_seconds"] = retry_after_seconds
        super().__init__(message, service=service, details=merged)
        self.retry_after_seconds = retry_after_seconds


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class ToolExecutionError(TripPlannerError):
    """A tool failed for a reason other than an upstream outage.

    Typically bad arguments produced by the model - a malformed date, an
    unresolvable city name. The agent can usually recover by correcting the
    arguments, so the message is written to be actionable *by the model*.
    """

    code = "tool_execution_error"
    status_code = 400
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        tool_name: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialise the error.

        Args:
            message: Description phrased so the model can act on it.
            tool_name: Name of the failing tool.
            details: Structured context, e.g. the offending arguments.
        """
        super().__init__(message, details={**(details or {}), "tool": tool_name})
        self.tool_name = tool_name


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class AgentBudgetExceededError(TripPlannerError):
    """The agent hit a guardrail: too many steps, replans, or hops.

    Not a bug - a designed stop condition. Current agentic-RAG practice is
    unambiguous that unbounded retrieve/replan loops are the dominant failure
    mode, so every loop in this system has an explicit ceiling. When one
    trips, the agent answers with the evidence gathered so far and flags the
    result as partial rather than looping indefinitely.
    """

    code = "agent_budget_exceeded"
    status_code = 200  # The user still receives a (partial) answer.
    retryable = False


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class AuthenticationError(TripPlannerError):
    """The caller's identity could not be established."""

    code = "unauthenticated"
    status_code = 401
    retryable = False


class AuthorizationError(TripPlannerError):
    """The caller is known but not permitted to perform this action.

    Raised for privilege failures such as a non-admin calling an admin route.
    Kept distinct from `AuthenticationError` so the API returns 403 rather
    than 401 and does not prompt the client to re-authenticate pointlessly.
    """

    code = "forbidden"
    status_code = 403
    retryable = False


class ResourceNotFoundError(TripPlannerError):
    """A requested resource does not exist, or is not visible to the caller.

    Deliberately conflates "absent" and "not yours": returning 404 rather than
    403 for another user's session avoids leaking the existence of that
    session's id.
    """

    code = "not_found"
    status_code = 404
    retryable = False
