"""FastAPI dependencies.

Services are constructed once at startup and stored on `app.state`, then
handed to routes through these dependency functions. Two reasons that beats
module-level globals: it keeps connection pools and HTTP clients tied to the
application lifecycle rather than to import order, and it lets tests override
any single dependency without patching module internals.

`get_current_user` is the security boundary. Every authenticated route depends
on it, and the `user_id` it returns is what gets handed to Postgres as the
identity that row level security trusts - so it comes only from a
cryptographically verified token, never from a header, query parameter or
request body.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Header, Request

from app.core.config import Settings, get_settings
from app.core.errors import AuthorizationError, ConfigurationError
from app.core.logging import get_logger
from app.core.security import TokenVerifier, extract_bearer_token
from app.db.repositories import (
    AdminRepository,
    ProfileRepository,
    SessionRepository,
    TraceRepository,
)
from app.db.session import Database

logger = get_logger(__name__)


class AuthenticatedUser:
    """The verified caller.

    Attributes:
        id: The user's UUID, taken from the token's `sub` claim.
        email: Email address, when the token carries one.
        claims: All verified claims, for debugging.
    """

    def __init__(self, user_id: str, email: str | None, claims: dict[str, Any]) -> None:
        """Initialise the user.

        Args:
            user_id: Verified subject claim.
            email: Verified email claim, if present.
            claims: The full verified claim set.
        """
        self.id = user_id
        self.email = email
        self.claims = claims


def get_app_settings() -> Settings:
    """Return application settings."""
    return get_settings()


def get_database(request: Request) -> Database:
    """Return the connection pool wrapper.

    Raises:
        ConfigurationError: If the database was never initialised.
    """
    database: Database | None = getattr(request.app.state, "database", None)
    if database is None:
        raise ConfigurationError("The database is not configured on this deployment.")
    return database


def get_verifier(request: Request) -> TokenVerifier:
    """Return the JWT verifier built at startup."""
    verifier: TokenVerifier | None = getattr(request.app.state, "verifier", None)
    if verifier is None:
        raise ConfigurationError("Authentication is not configured on this deployment.")
    return verifier


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
    verifier: Annotated[TokenVerifier, Depends(get_verifier)] = None,  # type: ignore[assignment]
) -> AuthenticatedUser:
    """Resolve and verify the caller's identity.

    Args:
        authorization: The `Authorization` header.
        verifier: The token verifier.

    Returns:
        The authenticated user.

    Raises:
        AuthenticationError: If the token is missing or invalid.
    """
    token = extract_bearer_token(authorization)
    claims = verifier.verify(token)

    return AuthenticatedUser(
        user_id=str(claims["sub"]),
        email=claims.get("email"),
        claims=claims,
    )


async def require_admin(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> AuthenticatedUser:
    """Require that the caller holds the admin role.

    The role is read from `profiles.app_role` in the database, never from a
    token claim. A JWT can carry arbitrary custom claims, and Supabase's user
    metadata is writable by the user's own client - so a role trusted from a
    token would be self-assignable from the browser console.

    Args:
        request: The incoming request, used to reach app state.
        user: The authenticated caller.

    Returns:
        The caller, confirmed as an admin.

    Raises:
        AuthorizationError: If the caller is not an admin.
    """
    database = get_database(request)
    profiles = ProfileRepository(database)

    if not await profiles.is_admin(user.id):
        logger.warning("auth.admin_denied", user_id=user.id)
        raise AuthorizationError("This endpoint requires administrator access.")

    return user


# -- Repositories -----------------------------------------------------------


def get_session_repository(
    database: Annotated[Database, Depends(get_database)],
) -> SessionRepository:
    """Return the conversation repository."""
    return SessionRepository(database)


def get_trace_repository(
    database: Annotated[Database, Depends(get_database)],
) -> TraceRepository:
    """Return the trace repository."""
    return TraceRepository(database)


def get_profile_repository(
    database: Annotated[Database, Depends(get_database)],
) -> ProfileRepository:
    """Return the profile repository."""
    return ProfileRepository(database)


def get_admin_repository(
    database: Annotated[Database, Depends(get_database)],
) -> AdminRepository:
    """Return the admin repository."""
    return AdminRepository(database)


def get_memory_service(request: Request) -> Any:
    """Return the long-term memory service, or None if unavailable."""
    return getattr(request.app.state, "memory_service", None)


def get_runner(request: Request) -> Any:
    """Return the agent runner built at startup.

    Raises:
        ConfigurationError: If the agent was never initialised.
    """
    runner = getattr(request.app.state, "runner", None)
    if runner is None:
        raise ConfigurationError("The agent is not configured on this deployment.")
    return runner


def client_ip(request: Request) -> str | None:
    """Best-effort client IP for the audit log.

    Prefers `X-Forwarded-For`, since the application runs behind a proxy on
    every supported deployment target and the socket address would otherwise
    be the proxy's. Only the first entry is taken - the rest of that header is
    client-supplied and must not be trusted.

    Args:
        request: The incoming request.

    Returns:
        An IP address, or None if it could not be determined.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]
AdminUser = Annotated[AuthenticatedUser, Depends(require_admin)]
Sessions = Annotated[SessionRepository, Depends(get_session_repository)]
Traces = Annotated[TraceRepository, Depends(get_trace_repository)]
Profiles = Annotated[ProfileRepository, Depends(get_profile_repository)]
Admin = Annotated[AdminRepository, Depends(get_admin_repository)]
AppSettings = Annotated[Settings, Depends(get_app_settings)]
