"""Postgres connection pool with row-level-security scoping.

The interesting part of this module is `user_scope`.

The backend talks to Supabase's Postgres directly rather than through
PostgREST, which is faster and lets us use pgvector operators that PostgREST
does not expose. The catch is that a direct connection authenticates as the
`postgres` role, and that role can read every row in the database. Used
naively, the row level security policies written in migration 0007 would never
run, and per-user isolation would silently degrade to "the application
promises to always write `where user_id = ...`".

`user_scope` closes that gap. Inside a transaction it:

1. sets ``request.jwt.claims`` to the caller's identity, which is what
   Supabase's ``auth.uid()`` reads; then
2. issues ``set local role authenticated``.

Postgres evaluates RLS against the *current* role, so after step 2 the
connection is subject to exactly the same policies as a browser client. A
query that forgets its `where` clause returns the caller's rows and nothing
else. Both settings are transaction-local, so they unwind automatically on
commit or rollback and cannot leak into the next borrower of that pooled
connection.

`service_scope` is the deliberate escape hatch for work that is genuinely
system-level - writing execution traces, running the memory extractor as a
background job. It is a separate, greppable function name rather than a
boolean flag, so an audit of privileged database access is one search.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import Settings
from app.core.errors import ConfigurationError, ExternalServiceError
from app.core.logging import get_logger

logger = get_logger(__name__)

# Supabase's `authenticated` role. Matches the `to authenticated` clause on
# every policy in migration 0007.
AUTHENTICATED_ROLE: Final[str] = "authenticated"


class Database:
    """Async Postgres pool that can run queries under a specific user's RLS.

    Attributes:
        settings: Application settings supplying the connection string.
    """

    def __init__(self, settings: Settings) -> None:
        """Initialise the wrapper without opening any connections.

        Args:
            settings: Application settings. A missing `DATABASE_URL` is not an
                error here; it disables the pool and callers fall back to
                in-memory behaviour, which keeps unit tests offline.
        """
        self.settings = settings
        self._pool: AsyncConnectionPool[AsyncConnection[DictRow]] | None = None

    # -- Lifecycle --------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """Whether a live connection pool exists."""
        return self._pool is not None

    async def connect(self) -> None:
        """Open the connection pool and verify pgvector is usable.

        Pool sizing is deliberately small. Supabase's free tier permits a
        limited number of concurrent connections, and a single Render free
        instance serving one worker does not need more; oversizing here is a
        reliable way to exhaust the database's connection limit and take the
        whole project down.

        Raises:
            ExternalServiceError: If the database is unreachable at startup.
        """
        url = self.settings.database_url.get_secret_value()
        if not url:
            logger.warning(
                "db.disabled",
                reason="DATABASE_URL not configured; falling back to in-memory stores",
            )
            return

        # Registers the `vector` type adapter on every new connection, so
        # embeddings can be passed as plain Python lists and read back as
        # numpy-free sequences.
        async def _configure(conn: AsyncConnection[DictRow]) -> None:
            from pgvector.psycopg import register_vector_async

            await register_vector_async(conn)

        self._pool = AsyncConnectionPool(
            conninfo=url,
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row, "autocommit": False},
            configure=_configure,
            open=False,
            timeout=15.0,
        )

        try:
            await self._pool.open(wait=True, timeout=20.0)
            async with self._pool.connection() as conn:
                await conn.execute("select 1")
        except Exception as exc:
            self._pool = None
            raise ExternalServiceError(
                "Could not establish a database connection at startup.",
                service="postgres",
                details={"error": str(exc)},
            ) from exc

        logger.info("db.connected", min_size=1, max_size=5)

    async def close(self) -> None:
        """Close the pool and release all connections."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("db.closed")

    def _require_pool(self) -> AsyncConnectionPool[AsyncConnection[DictRow]]:
        """Return the pool, or raise if the database was never configured."""
        if self._pool is None:
            raise ConfigurationError(
                "A database operation was attempted but DATABASE_URL is not configured."
            )
        return self._pool

    # -- Scopes -----------------------------------------------------------

    @asynccontextmanager
    async def user_scope(self, user_id: str) -> AsyncIterator[AsyncConnection[DictRow]]:
        """Yield a connection whose queries are constrained to one user by RLS.

        Use this for every operation performed *on behalf of* a signed-in
        user. The database, not the calling code, guarantees that rows
        belonging to other users are invisible.

        Args:
            user_id: The authenticated user's UUID, taken from a verified JWT.
                Never accept this value from an unverified request body - it
                becomes the identity the database trusts.

        Yields:
            A connection inside an open transaction, running as the
            `authenticated` role with `auth.uid()` resolving to `user_id`.

        Example:
            >>> async with db.user_scope(user_id) as conn:
            ...     rows = await (await conn.execute("select * from memories")).fetchall()
            # Returns only this user's memories, despite the missing filter.
        """
        pool = self._require_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                # Mirrors the claim set a PostgREST request would carry, so
                # policies behave identically for both access paths.
                claims = json.dumps(
                    {
                        "sub": user_id,
                        "role": AUTHENTICATED_ROLE,
                        "aud": self.settings.supabase_jwt_audience,
                    }
                )
                # `true` makes both settings transaction-local: they are
                # discarded on commit or rollback and cannot bleed into the
                # next user who borrows this pooled connection.
                await conn.execute("select set_config('request.jwt.claims', %s, true)", (claims,))
                await conn.execute(f"set local role {AUTHENTICATED_ROLE}")
                yield conn

    @asynccontextmanager
    async def service_scope(self, *, reason: str) -> AsyncIterator[AsyncConnection[DictRow]]:
        """Yield a privileged connection that bypasses row level security.

        Reserved for work with no single owning user: writing execution
        traces, background memory extraction, RAG cache maintenance. Every
        call is logged with its stated reason, so privileged access is
        reviewable after the fact rather than invisible.

        Args:
            reason: Short description of why elevated access is required,
                recorded in the log line.

        Yields:
            A connection inside an open transaction, running as the connecting
            role with RLS bypassed.
        """
        pool = self._require_pool()
        logger.debug("db.service_scope", reason=reason)
        async with pool.connection() as conn:
            async with conn.transaction():
                yield conn

    # -- Convenience ------------------------------------------------------

    async def healthcheck(self) -> dict[str, Any]:
        """Report database reachability and pgvector availability.

        Returns:
            A dict with an `ok` flag and, on failure, the reason. Never
            raises - the health endpoint must be able to report a broken
            database rather than becoming broken itself.
        """
        if self._pool is None:
            return {"ok": False, "reason": "not_configured"}
        try:
            async with self._pool.connection() as conn:
                cursor = await conn.execute(
                    "select extversion from pg_extension where extname = 'vector'"
                )
                row = await cursor.fetchone()
            return {"ok": True, "pgvector": (row or {}).get("extversion")}
        except Exception as exc:  # noqa: BLE001 - health checks never raise
            logger.warning("db.healthcheck_failed", error=str(exc))
            return {"ok": False, "reason": str(exc)}
