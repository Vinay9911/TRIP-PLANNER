"""System status: what this deployment can actually do, and what budget is left.

Two questions the interface cannot answer for itself, both of which it was
previously guessing at or hiding.

**"Can I use the local model?"** The honest answer is not "is the browser on
localhost" - someone can perfectly well run the frontend on their laptop
against the deployed API, and the local model would still be unreachable.
Ollama is reached by the *backend*, so only the backend can say. It is asked
here, cached briefly, and reported as a capability the client can disable a
control from. Guessing this in the browser produces the worst outcome
available: a switch that looks enabled, is not, and fails several seconds
later inside a plan the traveller is waiting on.

**"How much quota is left?"** Groq publishes a per-minute figure in headers and
never publishes the daily one, which is the limit that actually binds this
project (see `services/keys.py`). The answer here is therefore an *estimate*
built from metered spend, upgraded to a measurement for any key Groq has
already refused with real figures. Both are labelled as what they are, because
a number presented with more confidence than it deserves is worse than no
number - the whole point of showing this is knowing how much testing is left
before the demo, and a wrong reassurance defeats that.

Authenticated, like everything else: it reveals masked key identifiers and
usage totals, never key material.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter

from app.api.deps import AppSettings, CurrentUser
from app.core.logging import get_logger
from app.services.http import get_client
from app.services.keys import existing_pool

logger = get_logger(__name__)

router = APIRouter(tags=["system"])

# How long a local-model probe is trusted before asking again.
#
# The probe is a sub-second call to a loopback address when Ollama is running,
# and a fast connection refusal when it is not - but the chat page asks on
# every mount, so without a cache a tab left open would poll a service that
# changes state approximately never. Thirty seconds keeps "I just started
# Ollama" feeling immediate without making the check part of page load cost.
_PROBE_TTL_SECONDS = 30.0

_probe_cache: dict[str, Any] = {"checked_at": 0.0, "result": None}


async def _probe_local_models(base_url: str) -> dict[str, Any]:
    """Ask the configured Ollama server what it has, with a short timeout.

    Args:
        base_url: Ollama's base URL from settings.

    Returns:
        A dict with `available`, a human `reason`, and any model names found.
        Never raises: an unreachable local model is the normal case on a
        deployed instance, not an error worth surfacing as one.
    """
    now = time.monotonic()
    if _probe_cache["result"] is not None and now - _probe_cache["checked_at"] < _PROBE_TTL_SECONDS:
        return _probe_cache["result"]  # type: ignore[return-value]

    result: dict[str, Any]
    try:
        client = get_client()
        # A short timeout on purpose. On a deployed host this address is not
        # merely absent, it is the *host's own* loopback - so the failure mode
        # is a refusal or a hang, and a hang must not become page-load latency.
        response = await client.get(f"{base_url.rstrip('/')}/api/tags", timeout=2.0)
        response.raise_for_status()
        models = [entry.get("name", "") for entry in (response.json().get("models") or [])]
        result = {
            "available": True,
            "reason": "Ollama is running and reachable from the server.",
            "models": [name for name in models if name],
        }
    except Exception as exc:  # noqa: BLE001 - any failure means "not available"
        # Deliberately broad and deliberately quiet. Connection refused,
        # DNS failure, timeout and a malformed body all mean the same thing to
        # the caller, and none of them is a fault in this application.
        logger.debug("system.local_probe_failed", error=str(exc)[:120])
        result = {
            "available": False,
            "reason": (
                "No local model server is reachable from the API. This is "
                "expected on the hosted deployment - the local model runs on "
                "your own machine, so it is only available when you run the "
                "project locally."
            ),
            "models": [],
        }

    _probe_cache["checked_at"] = now
    _probe_cache["result"] = result
    return result


@router.get("/system/status", summary="Runtime capabilities and remaining quota")
async def system_status(user: CurrentUser, settings: AppSettings) -> dict[str, Any]:
    """Report what this deployment can do and how much cloud budget is left.

    Args:
        user: The authenticated caller. Required so the figures are not
            world-readable, even though they contain no key material.
        settings: Application settings.

    Returns:
        A dict with `local_llm` capability and `quota` for the Groq pool.
    """
    local = await _probe_local_models(settings.ollama_base_url)

    pool = existing_pool("groq")
    quota: dict[str, Any]

    if pool is None:
        quota = {"configured": False, "keys": [], "total": None}
    else:
        status = pool.status(include_quota=True)
        keys = status["keys"]

        # Summed across keys, because that is the number the operator actually
        # plans around: seven keys with 100k each is 700k of runway, and the
        # per-key breakdown only matters once one of them is spent.
        used = sum(int(entry["quota"]["used"]) for entry in keys)  # type: ignore[index]
        limit = sum(int(entry["quota"]["limit"]) for entry in keys)  # type: ignore[index]

        quota = {
            "configured": True,
            "provider": "groq",
            "total_keys": status["total_keys"],
            "available_keys": status["available_keys"],
            "keys": keys,
            "total": {
                "used": used,
                "limit": limit,
                "remaining": max(limit - used, 0),
            },
            # Said explicitly rather than left for the reader to infer. The
            # figures are this process's own arithmetic unless a key has been
            # refused, and a restart resets them - an operator who thinks they
            # are reading Groq's ledger will trust them further than they
            # deserve.
            "basis": (
                "Estimated from tokens this server has spent since it started. "
                "Groq does not publish a daily quota, so any key it has not yet "
                "refused is an estimate, not a reading."
            ),
        }

    return {
        "environment": settings.environment,
        "llm_provider_mode": settings.llm_provider,
        "local_llm": local,
        "quota": quota,
        # Reported so the interface can show the Admin link to everyone when
        # the dashboard has been opened up, rather than the link's visibility
        # and the endpoint's actual policy disagreeing - which would produce a
        # nav item that 403s for most people, the worst of both.
        "public_admin_dashboard": settings.public_admin_dashboard,
    }
