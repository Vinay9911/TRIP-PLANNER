"""Request-scoped context available to tools.

Some tools need to know *who is asking*. `recall_user_preferences` must search
one specific user's memories, and it must be impossible for the model to
choose whose. If `user_id` were a tool argument, the model would supply it -
and a model that can name any user id is one prompt injection away from
reading someone else's memories.

So identity is never a tool parameter. It is bound into a context variable at
the start of a request, before the agent runs, and read directly by the tools
that need it. The model can neither see nor influence it.

The same mechanism carries the service objects tools depend on, which keeps
tool signatures clean enough for the model to call reliably: every parameter
in a tool's schema is one more thing the model can get wrong, so the schema
should contain only what the model genuinely decides.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.errors import ConfigurationError

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance
    from app.memory.service import MemoryService
    from app.rag.retriever import MultiHopRetriever


@dataclass
class ToolContext:
    """Everything a tool may need that the model must not control.

    Attributes:
        user_id: Authenticated user, from a verified JWT. Never model-supplied.
        session_id: Current conversation, for provenance on any memory written.
        memory_service: Long-term memory read/write access.
        retriever: Multi-hop RAG retriever.
        language: Detected language of the conversation, so tools can request
            localised results where the upstream API supports it.
    """

    user_id: str
    session_id: str | None = None
    memory_service: MemoryService | None = None
    retriever: MultiHopRetriever | None = None
    language: str = "en"


_context: ContextVar[ToolContext | None] = ContextVar("tool_context", default=None)


def set_tool_context(context: ToolContext) -> Token[ToolContext | None]:
    """Bind a tool context for the current async context.

    Args:
        context: The context to bind.

    Returns:
        A token that `reset_tool_context` can use to restore the previous
        value. Resetting by token rather than setting `None` keeps nesting
        correct if a request ever runs inside another.
    """
    return _context.set(context)


def reset_tool_context(token: Token[ToolContext | None]) -> None:
    """Restore the tool context that was bound before `set_tool_context`.

    Args:
        token: The token returned by `set_tool_context`.
    """
    _context.reset(token)


def get_tool_context() -> ToolContext:
    """Return the current tool context.

    Returns:
        The bound context.

    Raises:
        ConfigurationError: If no context is bound. This is always a
            programming error - a tool requiring identity was reachable
            without one - so it fails loudly rather than defaulting to some
            anonymous user.
    """
    context = _context.get()
    if context is None:
        raise ConfigurationError(
            "No tool context is bound. Tools that need user identity must be invoked "
            "inside an agent run that called set_tool_context()."
        )
    return context


def try_get_tool_context() -> ToolContext | None:
    """Return the current tool context, or None if none is bound.

    For tools that can operate without identity but would like it if present.

    Returns:
        The bound context, or None.
    """
    return _context.get()
