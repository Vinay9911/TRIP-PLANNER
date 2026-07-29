"""Authentication: verifying Supabase JWTs.

Tokens are verified **locally** against Supabase's published public keys
rather than by calling the auth server on every request. Supabase signs with
asymmetric keys (ES256) by default for projects created after October 2025 and
publishes the public half at a JWKS endpoint, so verification is a signature
check against a cached key - no network round trip on the hot path, and the
API keeps working during a brief auth-service outage.

Three properties are non-negotiable here, and each has a failure mode that is
easy to ship by accident:

**Verify the signature.** Decoding a JWT without verifying it is the classic
mistake - the payload is base64, not encryption, so `user_id` from an
unverified token is attacker-controlled. That id is what this application
hands to Postgres as the identity RLS trusts, so an unverified decode would
turn per-user isolation into a suggestion.

**Pin the algorithms.** Accepting whatever the token's header asks for allows
the `alg: none` attack, and allowing HS256 alongside ES256 allows an attacker
to sign a token using the *public* key as an HMAC secret. Only the algorithms
we expect are permitted.

**Never trust a role claim from the token.** A JWT can carry arbitrary custom
claims. The admin check reads `profiles.app_role` from the database, which
only the service role can write.
"""

from __future__ import annotations

import time
from typing import Any, Final

import jwt
from jwt import PyJWKClient

from app.core.config import Settings
from app.core.errors import AuthenticationError, ConfigurationError
from app.core.logging import get_logger

logger = get_logger(__name__)

# ES256 is the current Supabase default; RS256 covers projects migrated from
# older configurations. HS256 is deliberately absent - see the module
# docstring for why mixing symmetric and asymmetric algorithms is unsafe.
ALLOWED_ALGORITHMS: Final[list[str]] = ["ES256", "RS256"]

# How long to keep a fetched JWKS before refetching. Long enough that key
# lookup is effectively free, short enough that a rotated key is picked up
# without a redeploy.
JWKS_CACHE_SECONDS: Final[int] = 600


class TokenVerifier:
    """Verifies Supabase-issued JWTs against cached public keys.

    Attributes:
        settings: Application settings supplying the JWKS URL and audience.
    """

    def __init__(self, settings: Settings) -> None:
        """Initialise the verifier.

        Args:
            settings: Application settings.
        """
        self.settings = settings
        self._client: PyJWKClient | None = None
        self._client_created_at: float = 0.0

    def _get_jwk_client(self) -> PyJWKClient:
        """Return a JWKS client, refreshing it periodically.

        Raises:
            ConfigurationError: If no JWKS URL is configured.
        """
        if not self.settings.supabase_jwks_url:
            raise ConfigurationError(
                "SUPABASE_JWKS_URL is not set. Find it under Project Settings -> API in "
                "your Supabase dashboard; it looks like "
                "https://<ref>.supabase.co/auth/v1/.well-known/jwks.json"
            )

        expired = (time.monotonic() - self._client_created_at) > JWKS_CACHE_SECONDS
        if self._client is None or expired:
            self._client = PyJWKClient(
                self.settings.supabase_jwks_url,
                cache_keys=True,
                lifespan=JWKS_CACHE_SECONDS,
            )
            self._client_created_at = time.monotonic()

        return self._client

    def verify(self, token: str) -> dict[str, Any]:
        """Verify a JWT and return its claims.

        Args:
            token: The bearer token, without the `Bearer ` prefix.

        Returns:
            The verified claims.

        Raises:
            AuthenticationError: If the token is missing, malformed, expired,
                or fails signature verification. The message stays generic -
                telling a caller exactly why verification failed helps an
                attacker more than a legitimate user.
        """
        if not token:
            raise AuthenticationError("No authentication token was supplied.")

        try:
            signing_key = self._get_jwk_client().get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                # Pinned, not read from the token header.
                algorithms=ALLOWED_ALGORITHMS,
                audience=self.settings.supabase_jwt_audience,
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_aud": True,
                    "require": ["exp", "sub"],
                },
            )

        except jwt.ExpiredSignatureError as exc:
            # Distinguished from other failures because the client's correct
            # response is different: refresh the token and retry.
            raise AuthenticationError(
                "Your session has expired. Please sign in again.",
                details={"reason": "expired"},
            ) from exc

        except jwt.InvalidTokenError as exc:
            logger.warning("auth.invalid_token", error=str(exc)[:200])
            raise AuthenticationError("The authentication token is not valid.") from exc

        except ConfigurationError:
            raise

        except Exception as exc:
            # Typically a JWKS fetch failure. Reported as an auth error rather
            # than a 500 because the caller cannot distinguish, and retrying
            # is the right client behaviour either way.
            logger.warning("auth.verification_error", error=str(exc)[:200])
            raise AuthenticationError("Could not verify the authentication token.") from exc

        if not claims.get("sub"):
            raise AuthenticationError("The authentication token has no subject claim.")

        return claims


def extract_bearer_token(authorization_header: str | None) -> str:
    """Pull the token out of an `Authorization` header.

    Args:
        authorization_header: The raw header value, or None.

    Returns:
        The token.

    Raises:
        AuthenticationError: If the header is missing or not a bearer scheme.
    """
    if not authorization_header:
        raise AuthenticationError("Authorization header is missing.")

    parts = authorization_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthenticationError("Authorization header must use the Bearer scheme.")

    return parts[1].strip()
