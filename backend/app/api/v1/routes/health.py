"""Health and readiness endpoints.

Two endpoints with different jobs, which is a distinction worth keeping:

`/health` is a liveness probe. It answers "is this process running?" and
nothing else, so it must never touch the database or any external API - a
liveness probe that fails when a dependency is down causes the platform to
restart a perfectly healthy process, turning a partial outage into a total
one. It also doubles as the keep-warm target for the scheduled ping that stops
free-tier hosting from sleeping.

`/health/ready` is a readiness and diagnostic probe. It does check
dependencies, reports each one separately, and is the first thing to look at
when the deployment misbehaves. It is unauthenticated but deliberately reveals
nothing sensitive: which dependencies are configured and reachable, never
values or error details that would help an attacker.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import AppSettings, get_database
from app.core.logging import get_logger
from app.db.session import Database

logger = get_logger(__name__)

router = APIRouter(tags=["health"])

_STARTED_AT = time.time()


@router.get("/health", summary="Liveness probe")
async def health(settings: AppSettings) -> dict[str, Any]:
    """Report that the process is alive.

    Touches no dependency by design. Also the endpoint the scheduled keep-warm
    job pings to stop free-tier hosting from idling the service to sleep.
    """
    return {
        "status": "ok",
        "environment": settings.environment,
        "uptime_seconds": int(time.time() - _STARTED_AT),
    }


@router.get("/health/ready", summary="Readiness and dependency diagnostics")
async def readiness(
    request: Request,
    settings: AppSettings,
    database: Annotated[Database, Depends(get_database)],
) -> dict[str, Any]:
    """Report whether each dependency is configured and reachable.

    Returns 200 even when something is broken - the body carries the verdict.
    A non-200 here would be indistinguishable from the service being down,
    which is exactly the information this endpoint exists to disambiguate.
    """
    checks: dict[str, Any] = {}

    checks["database"] = await database.healthcheck()

    embeddings = getattr(request.app.state, "embeddings", None)
    if embeddings is not None:
        checks["embeddings"] = await embeddings.healthcheck()
    else:
        checks["embeddings"] = {"ok": False, "reason": "not_configured"}

    # Presence only. Never report key values or lengths.
    checks["credentials"] = {
        "groq": bool(settings.groq_api_key.get_secret_value()),
        "gemini": bool(settings.gemini_api_key.get_secret_value()),
        "tavily": bool(settings.tavily_api_key.get_secret_value()),
        "geoapify": bool(settings.geoapify_api_key.get_secret_value()),
        "supabase_jwks": bool(settings.supabase_jwks_url),
    }

    # Key-pool health. The most useful line in this response when the agent
    # starts failing: it distinguishes "quota exhausted" from "broken".
    from app.services.keys import all_pool_status

    checks["key_pools"] = all_pool_status()

    checks["agent"] = {
        "runner": getattr(request.app.state, "runner", None) is not None,
        "memory": getattr(request.app.state, "memory_service", None) is not None,
        "retriever": getattr(request.app.state, "retriever", None) is not None,
        "flight_provider": settings.flight_provider,
    }

    ready = bool(checks["database"].get("ok")) and checks["agent"]["runner"]

    return {"ready": ready, "checks": checks}
