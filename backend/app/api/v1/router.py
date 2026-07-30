"""Version 1 API router.

Aggregates the route modules under a single `/api/v1` prefix so the version
lives in exactly one place and a future v2 can be mounted alongside rather
than replacing this one.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import admin, chat, health, images, memory

api_router = APIRouter()

# Health is registered without the version prefix as well (see `main.py`), so
# platform probes and the keep-warm cron can use a stable `/health` path that
# survives an API version bump.
api_router.include_router(health.router)
api_router.include_router(chat.router)
api_router.include_router(memory.router)
api_router.include_router(images.router)
api_router.include_router(admin.router)
