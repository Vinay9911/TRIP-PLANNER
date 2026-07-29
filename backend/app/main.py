"""Application entrypoint.

Builds the FastAPI app, wires services together at startup, and tears them
down cleanly at shutdown.

**Everything is constructed once, in `lifespan`.** The connection pool, the
embedding client, the compiled agent - all expensive, all reusable, all stored
on `app.state`. Building any of them per request would add a TLS handshake or
a pool creation to every call, which on a free-tier instance is the difference
between a usable demo and a slow one.

**Startup degrades rather than crashing.** If the database is unreachable the
application still starts and serves `/health`, reporting the failure through
`/health/ready`. A process that refuses to boot on a transient dependency
outage is much harder to diagnose than one that boots and tells you what is
wrong - particularly on a platform that will restart it repeatedly and
eventually give up.

**Errors are translated centrally.** Every deliberate error type carries its
own HTTP status and machine-readable code, so one exception handler produces a
consistent envelope for all of them instead of a chain of `isinstance` checks
scattered through the routes.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.api.v1.routes import health as health_routes
from app.core.config import Settings, get_settings
from app.core.errors import TripPlannerError
from app.core.logging import (
    bind_request_context,
    clear_request_context,
    configure_logging,
    get_logger,
)
from app.core.security import TokenVerifier

logger = get_logger(__name__)

DESCRIPTION = """
An AI travel-planning agent that plans personalised trips using tools, layered
memory and multi-hop retrieval.

**What it does**

* **Plans before acting.** A planner decomposes the request into steps, an
  executor carries each one out with tools, and a replanner revises the plan
  when something fails or turns out to be unnecessary.
* **Chooses its own tools.** Seven tools covering travel guides, mapped
  places, weather, live web search, flights and accommodation. There is no
  keyword routing anywhere - the model decides, every time.
* **Remembers you.** Durable preferences are extracted from conversations,
  deduplicated against what is already known, and applied to future trips to
  any city without you repeating yourself.
* **Retrieves in chained steps.** Each retrieval's query is built from the
  previous one's results: work out a city's districts, investigate the ones
  that suit you, then cross-reference your requirements against those
  specific neighbourhoods.
* **Replies in your language.** Detected per message; retrieval still runs
  against the English corpus so answer quality does not depend on the
  language you asked in.

**Getting started**

All endpoints except `/health` need a Supabase access token:
`Authorization: Bearer <token>`.

Send `POST /api/v1/chat` with a message. Reuse the returned `session_id` for
follow-ups. Use the returned `run_id` with the admin trace endpoint to see
exactly what the agent did.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Construct services at startup and release them at shutdown.

    Args:
        app: The FastAPI application.

    Yields:
        Control to the running application.
    """
    settings: Settings = get_settings()
    configure_logging(settings)

    logger.info(
        "app.starting",
        environment=settings.environment,
        flight_provider=settings.flight_provider,
    )

    if settings.is_production:
        missing = settings.missing_production_secrets()
        if missing:
            # Logged as a complete list rather than failing on the first
            # request that needs one of them.
            logger.error("app.missing_production_secrets", missing=missing)

    app.state.settings = settings
    app.state.verifier = TokenVerifier(settings)

    # Build the key pools eagerly. They would otherwise be created on the first
    # call that needs them, which means `/health/ready` reports no pools until
    # traffic arrives - exactly when an operator is most likely to be looking
    # at it. Constructing them here also surfaces a missing key at startup
    # rather than mid-run.
    from app.services.keys import get_pool

    for provider, raw in (
        ("groq", settings.groq_api_key.get_secret_value()),
        ("gemini", settings.gemini_api_key.get_secret_value()),
    ):
        try:
            pool = get_pool(provider, raw)
            logger.info("app.key_pool_ready", provider=provider, keys=pool.size)
        except Exception:  # noqa: BLE001
            logger.warning("app.key_pool_unavailable", provider=provider)

    # -- Database ---------------------------------------------------------
    from app.db.session import Database

    database = Database(settings)
    try:
        await database.connect()
    except Exception:
        logger.exception("app.database_unavailable")
    app.state.database = database

    # -- Embeddings -------------------------------------------------------
    from app.services.embeddings import EmbeddingService

    embeddings = EmbeddingService(settings)
    app.state.embeddings = embeddings

    # -- Memory, RAG, agent ----------------------------------------------
    from app.agent.runner import AgentRunner
    from app.db.repositories import SessionRepository, TraceRepository
    from app.memory.service import MemoryService
    from app.memory.short_term import build_checkpointer
    from app.memory.store import InMemoryMemoryStore, PostgresMemoryStore
    from app.rag.corpus import WikivoyageClient
    from app.rag.index import InMemoryRagIndex, PostgresRagIndex
    from app.rag.retriever import MultiHopRetriever

    if database.is_available:
        memory_store: Any = PostgresMemoryStore(database)
        rag_index: Any = PostgresRagIndex(database, settings)
        sessions: Any = SessionRepository(database)
        traces: Any = TraceRepository(database)
    else:
        # Keeps the API usable for a first run with no cloud credentials.
        # Nothing survives a restart, which `/health/ready` makes visible.
        logger.warning("app.degraded_mode", reason="no database; using in-memory stores")
        memory_store = InMemoryMemoryStore()
        rag_index = InMemoryRagIndex()
        sessions = None
        traces = None

    memory_service = MemoryService(memory_store, embeddings, settings)
    retriever = MultiHopRetriever(WikivoyageClient(settings), rag_index, embeddings, settings)

    checkpointer_handle = None
    try:
        checkpointer_handle = await build_checkpointer(settings)
    except Exception:
        logger.exception("app.checkpointer_unavailable")

    app.state.memory_service = memory_service
    app.state.retriever = retriever
    app.state.checkpointer_handle = checkpointer_handle
    app.state.runner = AgentRunner(
        settings=settings,
        memory_service=memory_service,
        retriever=retriever,
        sessions=sessions,
        traces=traces,
        checkpointer=checkpointer_handle.saver if checkpointer_handle else None,
    )

    logger.info("app.started")

    try:
        yield
    finally:
        logger.info("app.stopping")

        from app.memory.short_term import close_checkpointer
        from app.services.http import close_client

        if checkpointer_handle is not None:
            try:
                await close_checkpointer(checkpointer_handle)
            except Exception:
                logger.warning("app.checkpointer_close_failed", exc_info=True)

        await close_client()
        await database.close()
        logger.info("app.stopped")


def create_app() -> FastAPI:
    """Build the FastAPI application.

    A factory rather than a module-level instance so tests can construct an
    app with overridden dependencies, and so importing this module has no side
    effects.

    Returns:
        The configured application.
    """
    settings = get_settings()

    app = FastAPI(
        title="Trip Planner Agent",
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        contact={"name": "Trip Planner Agent"},
        license_info={"name": "MIT"},
    )

    app.add_middleware(
        CORSMiddleware,
        # An explicit list, never `["*"]`: the API is credentialed, and a
        # wildcard origin combined with credentials is both rejected by
        # browsers and a genuine security mistake.
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        """Bind a request id, time the request, and log its outcome.

        The request id is echoed in `X-Request-ID` so a user reporting a
        problem can quote it and have the full server-side trace found
        immediately.
        """
        started = time.perf_counter()
        context = bind_request_context(request_id=request.headers.get("x-request-id"))
        request_id = context["request_id"]

        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "http.unhandled_exception",
                path=request.url.path,
                method=request.method,
            )
            raise
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            # Health checks fire every few minutes from the keep-warm cron and
            # would otherwise dominate the logs.
            if not request.url.path.startswith("/health"):
                logger.info(
                    "http.request",
                    method=request.method,
                    path=request.url.path,
                    duration_ms=duration_ms,
                )
            clear_request_context()

        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(TripPlannerError)
    async def handle_application_error(request: Request, exc: TripPlannerError) -> JSONResponse:
        """Translate any deliberate error into the standard JSON envelope."""
        if exc.status_code >= 500:
            logger.error("api.error", code=exc.code, message=exc.message, details=exc.details)
        else:
            logger.info("api.client_error", code=exc.code, message=exc.message)

        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Return validation failures in the same envelope as everything else."""
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "The request body did not match the expected schema.",
                    "details": {"errors": exc.errors()[:5]},
                    "retryable": False,
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all so an unexpected bug still returns the standard envelope.

        The exception text is logged but never returned: internal messages
        routinely contain connection strings, file paths and query fragments.
        """
        logger.exception("api.unhandled_error", path=request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Something went wrong. Please try again.",
                    "details": {},
                    "retryable": True,
                }
            },
        )

    app.include_router(api_router, prefix="/api/v1")
    # Also mounted unprefixed so platform probes and the keep-warm cron have a
    # stable path that survives an API version bump.
    app.include_router(health_routes.router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        """Point a browser hitting the bare domain at the documentation."""
        return {
            "name": "Trip Planner Agent",
            "version": "0.1.0",
            "docs": "/docs",
            "health": "/health",
        }

    return app


app = create_app()
