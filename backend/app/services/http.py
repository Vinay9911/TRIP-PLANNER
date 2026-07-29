"""Shared HTTP client for outbound calls.

Four external APIs are called from the tool layer. Giving each its own client
would mean four connection pools, four inconsistent timeout policies, and four
places to get retry behaviour subtly wrong. Instead every outbound request
goes through one pooled client configured here.

The timeout deserves a note, because "the request timed out" is the single
most common failure in this system and the default is a trap. `httpx`'s
default timeout is 5 seconds for *everything*, and it applies per-operation,
not to the request as a whole. A slow-but-alive API that trickles bytes can
therefore hang far longer than the number suggests. This module sets an
explicit `connect` / `read` / `write` / `pool` breakdown so the worst case is
bounded and predictable, which matters when the agent has its own step
deadline to respect.

Retries cover connection errors, timeouts, 429 and 5xx - failures where the
same request might succeed on a second attempt. A 400 or a 404 is never
retried: the request is wrong, and repeating it only wastes the step budget.
"""

from __future__ import annotations

from typing import Any, Final

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.errors import ExternalServiceError, ExternalServiceTimeout, RateLimitError
from app.core.logging import get_logger

logger = get_logger(__name__)

# Bounded per-phase, so a stalled read cannot extend a request indefinitely.
DEFAULT_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(
    connect=5.0,
    read=15.0,
    write=5.0,
    pool=5.0,
)

# Status codes worth trying again. Everything else is a client error that a
# retry cannot fix.
RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({408, 425, 429, 500, 502, 503, 504})

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """Return the process-wide HTTP client, creating it on first use.

    Returns:
        A pooled `httpx.AsyncClient` with sane timeouts and limits.
    """
    global _client
    if _client is None:
        from app.core.config import get_settings

        _client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=True,
            # Wikimedia rejects requests whose User-Agent does not identify the
            # client and a contact address - a 403, not a soft rate limit. See
            # `Settings.http_user_agent`.
            headers={"User-Agent": get_settings().http_user_agent},
        )
    return _client


async def close_client() -> None:
    """Close the shared client and release its connections."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _raise_for_status(response: httpx.Response, *, service: str) -> None:
    """Convert a non-2xx response into a typed error.

    Args:
        response: The HTTP response to inspect.
        service: Short dependency name for logs and error details.

    Raises:
        RateLimitError: On 429, carrying `Retry-After` when present.
        ExternalServiceError: On any other non-2xx status.
    """
    if response.is_success:
        return

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise RateLimitError(
            f"{service} rejected the request for exceeding its rate limit.",
            service=service,
            retry_after_seconds=float(retry_after)
            if retry_after and retry_after.isdigit()
            else None,
            details={"status": response.status_code},
        )

    raise ExternalServiceError(
        f"{service} returned HTTP {response.status_code}.",
        service=service,
        # Only retry server-side and throttling failures.
        retryable=response.status_code in RETRYABLE_STATUS,
        details={
            "status": response.status_code,
            # Truncated: upstream error bodies are occasionally enormous HTML
            # pages, and the useful part is always at the front.
            "body": response.text[:300],
        },
    )


async def request_json(
    method: str,
    url: str,
    *,
    service: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    max_attempts: int = 3,
) -> Any:
    """Make an HTTP request and parse the JSON response, with retries.

    Args:
        method: HTTP verb, e.g. `"GET"`.
        url: Absolute URL.
        service: Short dependency name, used in logs and errors. Every failure
            is attributed to this name so the admin dashboard can rank
            dependencies by error rate.
        params: Query string parameters.
        json_body: Request body, serialised as JSON.
        headers: Extra request headers.
        timeout: Overall timeout override in seconds.
        max_attempts: Total attempts including the first.

    Returns:
        The decoded JSON body.

    Raises:
        ExternalServiceTimeout: If the request timed out on every attempt.
        RateLimitError: If the service throttled every attempt.
        ExternalServiceError: On any other transport or protocol failure.
    """
    client = get_client()
    attempt_number = 0

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=0.5, max=8.0),
        retry=retry_if_exception_type(
            (ExternalServiceError, ExternalServiceTimeout, RateLimitError)
        ),
        reraise=True,
    ):
        with attempt:
            attempt_number += 1
            try:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
                )
            except httpx.TimeoutException as exc:
                logger.warning("http.timeout", service=service, url=url, attempt=attempt_number)
                raise ExternalServiceTimeout(
                    f"{service} did not respond in time.",
                    service=service,
                    details={"url": url},
                ) from exc
            except httpx.HTTPError as exc:
                logger.warning(
                    "http.transport_error",
                    service=service,
                    url=url,
                    attempt=attempt_number,
                    error=str(exc)[:200],
                )
                raise ExternalServiceError(
                    f"Could not reach {service}.",
                    service=service,
                    details={"url": url, "error": str(exc)[:200]},
                ) from exc

            _raise_for_status(response, service=service)

            try:
                payload = response.json()
            except ValueError as exc:
                # A 200 with an unparseable body means the upstream contract
                # changed or a proxy intercepted the call. Not retryable.
                raise ExternalServiceError(
                    f"{service} returned a response that is not valid JSON.",
                    service=service,
                    retryable=False,
                    details={"url": url, "body": response.text[:300]},
                ) from exc

            logger.debug(
                "http.completed",
                service=service,
                url=url,
                status=response.status_code,
                attempt=attempt_number,
                elapsed_ms=int(response.elapsed.total_seconds() * 1000),
            )
            return payload

    raise ExternalServiceError(  # pragma: no cover - unreachable with reraise=True
        f"Retry loop for {service} exited without a result.", service=service
    )
