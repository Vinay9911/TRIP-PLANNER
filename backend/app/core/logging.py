"""Structured logging.

The brief asks for "logging that would actually help debug a failed run".
For an agent, that specifically means being able to reconstruct *one* run out
of interleaved concurrent requests, and seeing why the agent made the choices
it made - not just that an exception happened.

Two mechanisms provide that:

1. **Structured events.** Every log line is a dict, not a formatted string.
   `logger.info("tool.completed", tool="get_weather_forecast", ms=412)` is
   greppable and machine-queryable; "Tool get_weather_forecast took 412ms" is
   not.

2. **Contextual binding.** `request_id`, `user_id` and `run_id` are bound once
   per request into a context-local store and then automatically attached to
   every subsequent log line in that request - including lines emitted deep
   inside a tool or a graph node that has no idea a request exists. This is
   what makes `grep run_id=...` reconstruct a full agent trajectory.

In development the output is human-readable and coloured; in production it is
one JSON object per line, which is what log aggregators expect.
"""

from __future__ import annotations

import logging
import sys
from typing import Any
from uuid import uuid4

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars
from structlog.types import EventDict, Processor

from app.core.config import Settings


def _drop_color_message_key(_: object, __: str, event_dict: EventDict) -> EventDict:
    """Remove uvicorn's duplicate `color_message` key.

    Uvicorn logs the same text twice - once plain, once with ANSI codes. In
    JSON output the second copy is noise.
    """
    event_dict.pop("color_message", None)
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Install structlog processors and route stdlib logging through them.

    Third-party libraries (uvicorn, httpx, langchain) use the standard
    library logger. Routing those through structlog's `ProcessorFormatter`
    means their output gets the same shape and the same bound context as ours,
    so a failing httpx call inside a tool still carries the `run_id`.

    Args:
        settings: Application settings supplying log level and output format.
    """
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _drop_color_message_key,
    ]

    if settings.log_format == "json":
        # `format_exc_info` renders tracebacks into a string field. Only used
        # for JSON; the console renderer prints prettier tracebacks itself.
        shared_processors.append(structlog.processors.format_exc_info)
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace rather than append, so reloads during development do not stack
    # duplicate handlers and print every line twice.
    root.handlers = [handler]
    root.setLevel(settings.log_level)

    # Uvicorn installs its own handlers; clear them so lines are not emitted
    # twice in two different formats.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True

    # These are chatty at DEBUG and rarely tell us anything we want.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a named structured logger.

    Args:
        name: Logger name, conventionally the module's `__name__`.

    Returns:
        A bound logger that inherits any context bound for the current request.
    """
    return structlog.stdlib.get_logger(name)


def bind_request_context(
    *,
    request_id: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Bind correlation identifiers for the current async context.

    Everything logged downstream - including inside tools and graph nodes -
    inherits these fields automatically. Call `clear_request_context` when the
    request finishes so identifiers do not leak between requests served by the
    same worker.

    Args:
        request_id: Unique id for the HTTP request. Generated if omitted.
        user_id: Authenticated user id, when known.
        session_id: Conversation/thread id, when known.
        run_id: Unique id for one agent execution.

    Returns:
        The identifiers that were bound, so callers can echo `request_id` back
        in a response header.
    """
    context = {
        "request_id": request_id or str(uuid4()),
        "user_id": user_id,
        "session_id": session_id,
        "run_id": run_id,
    }
    bound = {key: value for key, value in context.items() if value is not None}
    bind_contextvars(**bound)
    return bound


def clear_request_context() -> None:
    """Drop all bound correlation identifiers for the current context."""
    clear_contextvars()
