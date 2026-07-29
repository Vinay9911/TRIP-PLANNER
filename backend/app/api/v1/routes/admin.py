"""Admin routes.

Backs the admin portal: who has signed in, what they talked about, what the
system remembers about them, and how the agent is behaving.

**Every route here is audited.** An admin can read any user's conversations
and stored preferences, which is a legitimate operational need and a genuine
privacy exposure. Pairing it with an append-only audit log means privileged
access is at least reviewable - and the log has no insert or update policy for
any role, so admins cannot edit their own trail.

**Authorization is enforced twice, deliberately.** `require_admin` checks the
role in the API layer, and the row level security policies check
`public.is_admin()` in the database. That is not redundancy for its own sake:
the API check produces a clean 403, while the database check means an admin
route that *forgot* its dependency would still return only the caller's own
rows rather than leaking everyone's.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.api.deps import Admin, AdminUser, Sessions, Traces, client_ip
from app.core.logging import get_logger
from app.db.repositories import serialise_datetimes
from app.schemas.chat import ErrorResponse

logger = get_logger(__name__)

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid token"},
        403: {"model": ErrorResponse, "description": "Not an administrator"},
    },
)


@router.get("/users", summary="List all users with activity counts")
async def list_users(
    request: Request,
    admin: AdminUser,
    repository: Admin,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List every user, with session, message and memory counts.

    Args:
        request: Incoming request, for the audit log's IP address.
        admin: The authenticated administrator.
        repository: Admin data access.
        limit: Maximum users to return.

    Returns:
        User rows ordered by most recently seen.
    """
    users = await repository.list_users(admin.id, limit=limit)
    await repository.log_action(
        admin.id,
        action="list_users",
        details={"count": len(users)},
        ip_address=client_ip(request),
    )
    return serialise_datetimes(users)  # type: ignore[no-any-return]


@router.get("/users/{user_id}", summary="Inspect one user in detail")
async def get_user(
    user_id: str,
    request: Request,
    admin: AdminUser,
    repository: Admin,
) -> dict[str, Any]:
    """Fetch one user's profile, conversations and stored memories.

    The memory list includes superseded entries, so the evolution of a
    traveller's profile is visible - the single most useful view when
    diagnosing an extractor that has learned something wrong.
    """
    detail = await repository.get_user_detail(admin.id, user_id)
    await repository.log_action(
        admin.id,
        action="view_user_detail",
        target_user_id=user_id,
        details={
            "sessions": len(detail["sessions"]),
            "memories": len(detail["memories"]),
        },
        ip_address=client_ip(request),
    )
    return serialise_datetimes(detail)  # type: ignore[no-any-return]


@router.get("/sessions/{session_id}/messages", summary="Read one conversation")
async def get_session_messages(
    session_id: str,
    request: Request,
    admin: AdminUser,
    repository: Admin,
    sessions: Sessions,
) -> list[dict[str, Any]]:
    """Read the full transcript of any conversation.

    Reading someone's private conversation is the most sensitive thing this
    portal does, so the audit entry records the specific session id rather
    than just the action.

    Cross-user visibility comes from the RLS policy's `is_admin()` branch, not
    from a privileged query - the same repository method a user calls for
    their own transcript simply returns more rows for an admin.
    """
    messages = await sessions.get_messages(session_id, admin.id, limit=500)

    await repository.log_action(
        admin.id,
        action="view_session_transcript",
        target_type="session",
        target_id=session_id,
        details={"message_count": len(messages)},
        ip_address=client_ip(request),
    )
    return serialise_datetimes(messages)  # type: ignore[no-any-return]


@router.get("/runs", summary="Recent agent runs across all users")
async def recent_runs(admin: AdminUser, repository: Admin, limit: int = 50) -> list[dict[str, Any]]:
    """List recent agent executions with status, latency and tool-call counts."""
    return serialise_datetimes(  # type: ignore[no-any-return]
        await repository.recent_runs(admin.id, limit=limit)
    )


@router.get("/runs/{run_id}", summary="Full execution trace for one run")
async def get_run_trace(run_id: str, admin: AdminUser, traces: Traces) -> dict[str, Any]:
    """Fetch the complete trace of one agent run.

    Returns the plan as generated, every executed step with its outcome, and
    every tool call with its arguments and latency.

    This is the most valuable view in the portal: it is the only thing that
    lets someone verify the agent genuinely plans and genuinely chooses tools,
    rather than taking the documentation's word for it. The tool `arguments`
    column is the direct evidence - the same toolbox producing different calls
    for different questions.
    """
    return serialise_datetimes(  # type: ignore[no-any-return]
        await traces.get_run(run_id, admin.id)
    )


@router.get("/analytics/tools", summary="Tool usage, failure rates and latency")
async def tool_analytics(
    admin: AdminUser, repository: Admin, days: int = 7
) -> list[dict[str, Any]]:
    """Per-tool call volume, failure rate and p95 latency.

    Differing call counts across tools are the quantitative evidence that
    selection is dynamic rather than scripted.
    """
    return serialise_datetimes(  # type: ignore[no-any-return]
        await repository.tool_analytics(admin.id, days=days)
    )


@router.get("/analytics/runs", summary="Aggregate run outcomes")
async def run_analytics(admin: AdminUser, repository: Admin, days: int = 7) -> dict[str, Any]:
    """Completion rates, latency percentiles, average replans and RAG hops."""
    return serialise_datetimes(  # type: ignore[no-any-return]
        await repository.run_analytics(admin.id, days=days)
    )


@router.get("/analytics/memory", summary="Long-term memory health")
async def memory_analytics(admin: AdminUser, repository: Admin) -> dict[str, Any]:
    """Memory counts by type and status.

    Read this as a health check on consolidation: `avg_mentions` above 1 means
    facts are being reinforced rather than duplicated, and a non-zero
    superseded count means contradictions are being resolved. Both at zero
    would indicate the consolidation pipeline is not running.
    """
    return serialise_datetimes(  # type: ignore[no-any-return]
        await repository.memory_analytics(admin.id)
    )


@router.get("/health/dependencies", summary="External dependency health")
async def dependency_health(
    admin: AdminUser, repository: Admin, hours: int = 24
) -> list[dict[str, Any]]:
    """Recent failure and degradation rates per external API."""
    return serialise_datetimes(  # type: ignore[no-any-return]
        await repository.dependency_health(admin.id, hours=hours)
    )


@router.get("/audit-log", summary="Administrator access log")
async def audit_log(admin: AdminUser, repository: Admin, limit: int = 100) -> list[dict[str, Any]]:
    """Read the append-only record of administrator actions.

    Readable by admins and writable by no one through the API - the table has
    no insert or update policy for any role, so entries are added only by the
    service role and cannot be altered afterwards.
    """
    return serialise_datetimes(  # type: ignore[no-any-return]
        await repository.get_audit_log(admin.id, limit=limit)
    )
