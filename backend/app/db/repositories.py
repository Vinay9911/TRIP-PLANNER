"""Data access.

All SQL lives here. Routes and agent nodes call repository methods; they never
build queries themselves. That keeps two things true: the RLS scoping decision
(`user_scope` versus `service_scope`) is made in one auditable place, and the
schema can change without a search through the whole codebase.

The scoping rule, restated because it is the important one:

* Anything done *for* a signed-in user goes through `user_scope`, where the
  database enforces isolation.
* Trace writes go through `service_scope`. They are system bookkeeping about a
  request, and traces are deliberately not user-writable - migration 0007
  grants users `select` on them and nothing else, so a user-scoped insert
  would be refused by the very policy that makes the trace trustworthy.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.agent.state import PlanStep
from app.core.errors import ResourceNotFoundError
from app.core.logging import get_logger
from app.db.session import Database
from app.tools.base import ToolCallRecord

logger = get_logger(__name__)


class SessionRepository:
    """Conversations and their messages."""

    def __init__(self, db: Database) -> None:
        """Initialise the repository.

        Args:
            db: Connection pool wrapper.
        """
        self.db = db

    async def create(
        self, user_id: str, *, title: str | None = None, language: str | None = None
    ) -> dict[str, Any]:
        """Create a conversation.

        Args:
            user_id: Owner.
            title: Optional label.
            language: Optional BCP-47 language tag.

        Returns:
            The new session row.
        """
        async with self.db.user_scope(user_id) as conn:
            cursor = await conn.execute(
                """
                insert into public.sessions (user_id, title, language)
                values (%s, %s, %s)
                returning id::text, user_id::text, title, destination, language,
                          message_count, created_at, updated_at
                """,
                (user_id, title, language),
            )
            row = await cursor.fetchone()
        return dict(row or {})

    async def get(self, session_id: str, user_id: str) -> dict[str, Any]:
        """Fetch one conversation.

        Args:
            session_id: Conversation id.
            user_id: Caller, enforced by RLS.

        Returns:
            The session row.

        Raises:
            ResourceNotFoundError: If it does not exist or is not visible.
                Deliberately the same error for both, so the response cannot
                be used to probe for other users' session ids.
        """
        async with self.db.user_scope(user_id) as conn:
            cursor = await conn.execute(
                """
                select id::text, user_id::text, title, destination, language,
                       message_count, created_at, updated_at
                  from public.sessions
                 where id = %s and archived_at is null
                """,
                (session_id,),
            )
            row = await cursor.fetchone()

        if row is None:
            raise ResourceNotFoundError(f"Session {session_id} was not found.")
        return dict(row)

    async def list_for_user(self, user_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """List a user's conversations, most recent first."""
        async with self.db.user_scope(user_id) as conn:
            cursor = await conn.execute(
                """
                select id::text, title, destination, language, message_count,
                       created_at, updated_at
                  from public.sessions
                 where archived_at is null
                 order by updated_at desc
                 limit %s
                """,
                (limit,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def update_context(
        self,
        session_id: str,
        user_id: str,
        *,
        title: str | None = None,
        destination: str | None = None,
        language: str | None = None,
    ) -> None:
        """Fill in session metadata discovered during a run.

        Uses `coalesce` so a null argument leaves the existing value alone -
        a later turn that does not mention a destination must not erase the
        one established earlier.
        """
        async with self.db.user_scope(user_id) as conn:
            await conn.execute(
                """
                update public.sessions
                   set title       = coalesce(title, %s),
                       destination = coalesce(%s, destination),
                       language    = coalesce(%s, language)
                 where id = %s
                """,
                (title, destination, language, session_id),
            )

    async def archive(self, session_id: str, user_id: str) -> bool:
        """Soft-delete a conversation."""
        async with self.db.user_scope(user_id) as conn:
            cursor = await conn.execute(
                "update public.sessions set archived_at = now() where id = %s",
                (session_id,),
            )
            return cursor.rowcount > 0

    async def add_message(
        self,
        session_id: str,
        user_id: str,
        *,
        role: str,
        content: str,
        language: str | None = None,
        token_count: int | None = None,
    ) -> dict[str, Any]:
        """Append a message to a conversation.

        Returns:
            The new message row, including its id for memory provenance.
        """
        async with self.db.user_scope(user_id) as conn:
            cursor = await conn.execute(
                """
                insert into public.messages
                    (session_id, user_id, role, content, language, token_count)
                values (%s, %s, %s::public.message_role, %s, %s, %s)
                returning id::text, session_id::text, role, content, language, created_at
                """,
                (session_id, user_id, role, content, language, token_count),
            )
            row = await cursor.fetchone()
        return dict(row or {})

    async def get_messages(
        self, session_id: str, user_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Fetch a conversation's messages in chronological order."""
        async with self.db.user_scope(user_id) as conn:
            cursor = await conn.execute(
                """
                select id::text, role, content, language, created_at
                  from public.messages
                 where session_id = %s
                 order by created_at
                 limit %s
                """,
                (session_id, limit),
            )
            return [dict(row) for row in await cursor.fetchall()]


class TraceRepository:
    """Agent execution traces.

    Written through `service_scope` because traces are system bookkeeping:
    users may read their own but must not be able to write or alter them, so
    a user-scoped insert would be blocked by the policy that gives the trace
    its value.
    """

    def __init__(self, db: Database) -> None:
        """Initialise the repository.

        Args:
            db: Connection pool wrapper.
        """
        self.db = db

    async def start_run(
        self,
        *,
        run_id: str,
        session_id: str,
        user_id: str,
        request_message_id: str | None,
    ) -> None:
        """Record the beginning of an agent run."""
        async with self.db.service_scope(reason="agent trace: start run") as conn:
            await conn.execute(
                """
                insert into public.agent_runs (id, session_id, user_id, request_message_id)
                values (%s, %s, %s, %s)
                on conflict (id) do nothing
                """,
                (run_id, session_id, user_id, request_message_id),
            )

    async def finish_run(
        self,
        *,
        run_id: str,
        status: str,
        initial_plan: list[PlanStep],
        replan_count: int,
        injected_memory_ids: list[str],
        rag_hops: int,
        detected_language: str | None,
        latency_ms: int,
        response_message_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        """Record the completion of an agent run."""
        plan_json = [step.model_dump() for step in initial_plan]

        async with self.db.service_scope(reason="agent trace: finish run") as conn:
            await conn.execute(
                """
                update public.agent_runs
                   set status              = %s::public.run_status,
                       initial_plan        = %s::jsonb,
                       replan_count        = %s,
                       injected_memory_ids = %s::uuid[],
                       rag_hops            = %s,
                       detected_language   = %s,
                       latency_ms          = %s,
                       response_message_id = %s,
                       error_code          = %s,
                       error_message       = %s,
                       prompt_tokens       = %s,
                       completion_tokens   = %s,
                       finished_at         = now()
                 where id = %s
                """,
                (
                    status,
                    json.dumps(plan_json),
                    replan_count,
                    injected_memory_ids,
                    rag_hops,
                    detected_language,
                    latency_ms,
                    response_message_id,
                    error_code,
                    error_message,
                    prompt_tokens,
                    completion_tokens,
                    run_id,
                ),
            )

    async def record_steps(self, run_id: str, results: list[Any]) -> dict[int, str]:
        """Persist the executed plan steps.

        Args:
            run_id: The run these belong to.
            results: `StepResult` objects in execution order.

        Returns:
            A mapping of step position to its database id, so tool calls can
            be attached to the right step.
        """
        ids: dict[int, str] = {}

        async with self.db.service_scope(reason="agent trace: steps") as conn:
            for position, result in enumerate(results):
                cursor = await conn.execute(
                    """
                    insert into public.agent_steps
                        (run_id, step_index, description, status, replan_cycle,
                         result_summary, error_message, latency_ms, finished_at)
                    values (%s, %s, %s, %s::public.step_status, %s, %s, %s, %s, now())
                    on conflict (run_id, replan_cycle, step_index) do update
                        set status = excluded.status,
                            result_summary = excluded.result_summary
                    returning id::text
                    """,
                    (
                        run_id,
                        position,
                        result.step.description,
                        "completed" if result.succeeded else "failed",
                        result.replan_cycle,
                        (result.output or "")[:2000],
                        result.error,
                        result.latency_ms,
                    ),
                )
                row = await cursor.fetchone()
                if row:
                    ids[position] = row["id"]

        return ids

    async def record_tool_calls(self, run_id: str, records: list[ToolCallRecord]) -> None:
        """Persist every tool invocation made during a run."""
        if not records:
            return

        async with self.db.service_scope(reason="agent trace: tool calls") as conn:
            for record in records:
                await conn.execute(
                    """
                    insert into public.tool_calls
                        (run_id, tool_name, arguments, result_summary, succeeded,
                         degraded, error_code, error_message, latency_ms)
                    values (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        record.tool_name,
                        json.dumps(record.arguments),
                        record.result_summary[:2000],
                        record.status != "invalid",
                        record.status == "degraded",
                        record.error_code,
                        record.error_message,
                        record.latency_ms,
                    ),
                )

    async def get_run(self, run_id: str, user_id: str) -> dict[str, Any]:
        """Fetch one run with its steps and tool calls.

        Read through `user_scope`, so a user can inspect their own runs and an
        admin can inspect anyone's - both governed by the policies rather than
        by a flag passed in here.
        """
        async with self.db.user_scope(user_id) as conn:
            cursor = await conn.execute(
                """
                select id::text, session_id::text, user_id::text, status, initial_plan,
                       replan_count, rag_hops, detected_language, latency_ms,
                       error_code, error_message, started_at, finished_at
                  from public.agent_runs
                 where id = %s
                """,
                (run_id,),
            )
            run = await cursor.fetchone()

            if run is None:
                raise ResourceNotFoundError(f"Run {run_id} was not found.")

            cursor = await conn.execute(
                """
                select step_index, description, status, replan_cycle, result_summary,
                       error_message, latency_ms
                  from public.agent_steps
                 where run_id = %s
                 order by replan_cycle, step_index
                """,
                (run_id,),
            )
            steps = [dict(row) for row in await cursor.fetchall()]

            cursor = await conn.execute(
                """
                select tool_name, arguments, result_summary, succeeded, degraded,
                       error_code, error_message, latency_ms, created_at
                  from public.tool_calls
                 where run_id = %s
                 order by created_at
                """,
                (run_id,),
            )
            tool_calls = [dict(row) for row in await cursor.fetchall()]

        return {**dict(run), "steps": steps, "tool_calls": tool_calls}

    async def list_runs_for_session(
        self, session_id: str, user_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List runs for one conversation, most recent first."""
        async with self.db.user_scope(user_id) as conn:
            cursor = await conn.execute(
                """
                select id::text, status, replan_count, rag_hops, latency_ms,
                       detected_language, started_at, finished_at
                  from public.agent_runs
                 where session_id = %s
                 order by started_at desc
                 limit %s
                """,
                (session_id, limit),
            )
            return [dict(row) for row in await cursor.fetchall()]


class ProfileRepository:
    """User profiles."""

    def __init__(self, db: Database) -> None:
        """Initialise the repository.

        Args:
            db: Connection pool wrapper.
        """
        self.db = db

    async def get(self, user_id: str) -> dict[str, Any] | None:
        """Fetch one profile, or None if it does not exist yet."""
        async with self.db.user_scope(user_id) as conn:
            cursor = await conn.execute(
                """
                select id::text, email, display_name, avatar_url, app_role,
                       preferred_language, memory_enabled, created_at, last_seen_at
                  from public.profiles
                 where id = %s
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def is_admin(self, user_id: str) -> bool:
        """Whether a user holds the admin role.

        Read through `service_scope` because it is an authorization decision:
        it must not itself depend on policies that could be misconfigured, and
        it runs before the caller's own scope has been established.
        """
        async with self.db.service_scope(reason="authorization check") as conn:
            cursor = await conn.execute(
                "select app_role = 'admin' as is_admin from public.profiles where id = %s",
                (user_id,),
            )
            row = await cursor.fetchone()
        return bool(row and row["is_admin"])

    async def touch_last_seen(self, user_id: str) -> None:
        """Record that a user was active, for the admin user list."""
        async with self.db.service_scope(reason="update last_seen_at") as conn:
            await conn.execute(
                "update public.profiles set last_seen_at = now() where id = %s", (user_id,)
            )

    async def memory_enabled(self, user_id: str) -> bool:
        """Whether long-term memory extraction is enabled for this user.

        Defaults to True when no profile row exists yet, matching the column
        default so a brand-new account behaves consistently.
        """
        async with self.db.service_scope(reason="memory opt-out check") as conn:
            cursor = await conn.execute(
                "select memory_enabled from public.profiles where id = %s", (user_id,)
            )
            row = await cursor.fetchone()
        return bool(row["memory_enabled"]) if row else True


class AdminRepository:
    """Cross-user queries backing the admin portal.

    Every method runs under `user_scope` for the *calling admin*, so the
    `public.is_admin()` predicate in the RLS policies is what actually grants
    the cross-user visibility. The API layer checks the role too, but the
    database is the enforcement point - an admin route that forgot its check
    would still return only the caller's own rows.
    """

    def __init__(self, db: Database) -> None:
        """Initialise the repository.

        Args:
            db: Connection pool wrapper.
        """
        self.db = db

    async def list_users(self, admin_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """List all users with activity counts."""
        async with self.db.user_scope(admin_id) as conn:
            cursor = await conn.execute(
                """
                select p.id::text, p.email, p.display_name, p.app_role,
                       p.preferred_language, p.memory_enabled,
                       p.created_at, p.last_seen_at,
                       (select count(*) from public.sessions s where s.user_id = p.id)
                           as session_count,
                       (select count(*) from public.messages m where m.user_id = p.id)
                           as message_count,
                       (select count(*) from public.memories mem
                         where mem.user_id = p.id and mem.status = 'active')
                           as memory_count
                  from public.profiles p
                 order by p.last_seen_at desc nulls last
                 limit %s
                """,
                (limit,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_user_detail(self, admin_id: str, target_user_id: str) -> dict[str, Any]:
        """Fetch one user's profile, sessions and memories."""
        async with self.db.user_scope(admin_id) as conn:
            cursor = await conn.execute(
                """
                select id::text, email, display_name, app_role, preferred_language,
                       memory_enabled, created_at, last_seen_at
                  from public.profiles where id = %s
                """,
                (target_user_id,),
            )
            profile = await cursor.fetchone()
            if profile is None:
                raise ResourceNotFoundError(f"User {target_user_id} was not found.")

            cursor = await conn.execute(
                """
                select id::text, title, destination, language, message_count,
                       created_at, updated_at
                  from public.sessions
                 where user_id = %s
                 order by updated_at desc
                 limit 50
                """,
                (target_user_id,),
            )
            sessions = [dict(row) for row in await cursor.fetchall()]

            cursor = await conn.execute(
                """
                select id::text, memory_type, subject, content, confidence,
                       mention_count, status, source_lang, superseded_by::text,
                       first_seen_at, last_seen_at
                  from public.memories
                 where user_id = %s
                 order by status, last_seen_at desc
                 limit 200
                """,
                (target_user_id,),
            )
            memories = [dict(row) for row in await cursor.fetchall()]

        return {"profile": dict(profile), "sessions": sessions, "memories": memories}

    async def tool_analytics(self, admin_id: str, *, days: int = 7) -> list[dict[str, Any]]:
        """Per-tool call volume, failure rate and latency percentiles.

        This is the evidence for the claim that tool selection is dynamic:
        differing call counts across tools show the model choosing between
        them rather than following a fixed script.
        """
        async with self.db.user_scope(admin_id) as conn:
            cursor = await conn.execute(
                """
                select tool_name,
                       count(*)                                    as calls,
                       count(*) filter (where not succeeded)        as failures,
                       count(*) filter (where degraded)             as degraded,
                       round(avg(latency_ms))                       as avg_latency_ms,
                       percentile_disc(0.95) within group (order by latency_ms)
                                                                    as p95_latency_ms
                  from public.tool_calls
                 where created_at > now() - make_interval(days => %s)
                 group by tool_name
                 order by calls desc
                """,
                (days,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def run_analytics(self, admin_id: str, *, days: int = 7) -> dict[str, Any]:
        """Aggregate run outcomes and latency."""
        async with self.db.user_scope(admin_id) as conn:
            cursor = await conn.execute(
                """
                select count(*)                                          as total_runs,
                       count(*) filter (where status = 'completed')      as completed,
                       count(*) filter (where status = 'clarifying')     as clarifying,
                       count(*) filter (where status = 'partial')        as partial,
                       count(*) filter (where status = 'failed')         as failed,
                       round(avg(latency_ms))                            as avg_latency_ms,
                       percentile_disc(0.95) within group (order by latency_ms)
                                                                         as p95_latency_ms,
                       round(avg(replan_count), 2)                       as avg_replans,
                       round(avg(rag_hops), 2)                           as avg_rag_hops,
                       round(avg(prompt_tokens + completion_tokens))      as avg_tokens_per_run,
                       max(prompt_tokens + completion_tokens)             as max_tokens_per_run,
                       sum(prompt_tokens + completion_tokens)             as total_tokens
                  from public.agent_runs
                 where started_at > now() - make_interval(days => %s)
                """,
                (days,),
            )
            row = await cursor.fetchone()
        return dict(row or {})

    async def recent_runs(self, admin_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """List recent runs across all users."""
        async with self.db.user_scope(admin_id) as conn:
            cursor = await conn.execute(
                """
                select r.id::text, r.user_id::text, p.email, r.session_id::text,
                       r.status, r.replan_count, r.rag_hops, r.latency_ms,
                       r.detected_language, r.started_at,
                       (select count(*) from public.tool_calls t where t.run_id = r.id)
                           as tool_call_count
                  from public.agent_runs r
                  left join public.profiles p on p.id = r.user_id
                 order by r.started_at desc
                 limit %s
                """,
                (limit,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def memory_analytics(self, admin_id: str) -> dict[str, Any]:
        """Aggregate memory counts by type and status.

        Shows the consolidation pipeline working: a healthy system has a
        meaningful `avg_mentions` above 1 (facts being reinforced rather than
        duplicated) and some superseded rows (contradictions being resolved).
        """
        async with self.db.user_scope(admin_id) as conn:
            cursor = await conn.execute(
                """
                select memory_type, status, count(*) as count,
                       round(avg(confidence)::numeric, 2)   as avg_confidence,
                       round(avg(mention_count)::numeric, 2) as avg_mentions
                  from public.memories
                 group by memory_type, status
                 order by memory_type, status
                """
            )
            breakdown = [dict(row) for row in await cursor.fetchall()]

            cursor = await conn.execute(
                """
                select count(*) as total,
                       count(*) filter (where status = 'active')     as active,
                       count(*) filter (where status = 'superseded') as superseded,
                       count(distinct user_id)                       as users_with_memories
                  from public.memories
                """
            )
            totals = await cursor.fetchone()

        return {"totals": dict(totals or {}), "breakdown": breakdown}

    async def log_action(
        self,
        admin_id: str,
        *,
        action: str,
        target_user_id: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        details: dict[str, Any] | None = None,
        ip_address: str | None = None,
    ) -> None:
        """Append an entry to the admin audit log.

        Written with `service_scope` because the table has no insert policy for
        any role - that is what makes it append-only and immutable to admins
        themselves. An audit log its subject can edit records nothing.
        """
        async with self.db.service_scope(reason="admin audit log") as conn:
            await conn.execute(
                """
                insert into public.admin_audit_log
                    (admin_id, action, target_user_id, target_type, target_id,
                     details, ip_address)
                values (%s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    admin_id,
                    action,
                    target_user_id,
                    target_type,
                    target_id,
                    json.dumps(details or {}),
                    ip_address,
                ),
            )

    async def get_audit_log(self, admin_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Read the admin audit log, most recent first."""
        async with self.db.user_scope(admin_id) as conn:
            cursor = await conn.execute(
                """
                select a.id::text, a.action, a.target_type, a.target_id::text,
                       a.details, a.created_at,
                       admin.email as admin_email,
                       target.email as target_email
                  from public.admin_audit_log a
                  left join public.profiles admin  on admin.id  = a.admin_id
                  left join public.profiles target on target.id = a.target_user_id
                 order by a.created_at desc
                 limit %s
                """,
                (limit,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def dependency_health(self, admin_id: str, *, hours: int = 24) -> list[dict[str, Any]]:
        """Recent failure rates per external dependency."""
        async with self.db.user_scope(admin_id) as conn:
            cursor = await conn.execute(
                """
                select tool_name,
                       count(*)                             as calls,
                       count(*) filter (where degraded)     as degraded,
                       max(created_at) filter (where degraded or not succeeded)
                                                            as last_failure_at,
                       max(error_message) filter (where error_message is not null)
                                                            as last_error
                  from public.tool_calls
                 where created_at > now() - make_interval(hours => %s)
                 group by tool_name
                 order by degraded desc, calls desc
                """,
                (hours,),
            )
            return [dict(row) for row in await cursor.fetchall()]


def serialise_datetimes(payload: Any) -> Any:
    """Recursively convert datetimes to ISO strings for JSON responses.

    Args:
        payload: Any JSON-shaped structure.

    Returns:
        The same structure with datetimes rendered as ISO 8601 strings.
    """
    if isinstance(payload, datetime):
        return payload.isoformat()
    if isinstance(payload, dict):
        return {key: serialise_datetimes(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [serialise_datetimes(item) for item in payload]
    return payload
