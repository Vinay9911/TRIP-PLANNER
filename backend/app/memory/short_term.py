"""Short-term memory: conversation state within a session.

This is the second of the two memory systems the brief asks for, and it solves
a different problem from long-term memory. Long-term memory answers "who is
this traveller?" across sessions. Short-term memory answers "what are we
talking about right now?" - it is what lets "make day two more relaxed" mean
anything.

It is implemented with LangGraph's checkpointer, keyed by `thread_id`, which
is the same UUID as the session row. Because the checkpointer persists the
whole graph state rather than just a message list, a conversation survives a
process restart mid-run - relevant on free-tier hosting, where the instance is
shut down after fifteen minutes of inactivity and cold-started on the next
request.

**The context window problem.** A planning conversation accumulates tool
results fast: one itinerary turn can add several thousand tokens of weather,
places and article text. Left alone, a long session eventually exceeds the
model's context window and every subsequent turn fails. Two mechanisms keep
it bounded:

* `trim_conversation` keeps a recent window, always preserving the system
  message and never orphaning a tool result from the call that produced it -
  a dangling `ToolMessage` is a hard API error, not a degraded prompt.
* `summarise_conversation` compresses older turns into a short brief once the
  history grows past a threshold, so the *substance* of early turns survives
  even after the messages themselves are dropped.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Final

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.services.llm import ModelRole, get_model, invoke_with_retry

logger = get_logger(__name__)

# Turns kept verbatim before summarisation kicks in. Six exchanges is enough
# for the follow-up questions people actually ask ("make day two lighter",
# "what about the other neighbourhood?") without carrying an entire planning
# session in every prompt.
RECENT_TURN_LIMIT: Final[int] = 12

# Rough character budget for the retained window. Deliberately conservative:
# tool results are verbose, and the planner also needs room for retrieved
# memories and RAG passages in the same request.
MAX_CONTEXT_CHARS: Final[int] = 12000


@dataclass
class CheckpointerHandle:
    """An open checkpointer together with whatever must be closed to release it.

    `AsyncPostgresSaver.from_conn_string` is an async context manager, but the
    application's lifetime is not a `with` block - the saver is opened at
    startup and closed at shutdown, arbitrarily far apart. Pairing the saver
    with its context manager in one object keeps that dependency explicit,
    rather than stashing the context manager on a private attribute of the
    saver and hoping nothing else touches it.

    Attributes:
        saver: The LangGraph checkpointer to hand to the compiled graph.
        closer: The context manager to exit at shutdown, if any.
    """

    saver: BaseCheckpointSaver
    closer: AbstractAsyncContextManager[BaseCheckpointSaver] | None = None


async def build_checkpointer(settings: Settings | None = None) -> CheckpointerHandle:
    """Create the conversation checkpointer.

    Uses Postgres when a database is configured so conversations survive
    restarts, and an in-process saver otherwise. The fallback is what allows
    the test suite and a first-run clone to work with no cloud credentials at
    all.

    Args:
        settings: Settings override, for tests.

    Returns:
        A handle holding the open checkpointer. Pass it to
        `close_checkpointer` at shutdown.
    """
    cfg = settings or get_settings()

    if not cfg.has_database:
        from langgraph.checkpoint.memory import InMemorySaver

        logger.warning(
            "checkpointer.in_memory",
            reason="No DATABASE_URL configured; conversations will not survive a restart",
        )
        return CheckpointerHandle(saver=InMemorySaver())

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    closer = AsyncPostgresSaver.from_conn_string(cfg.database_url.get_secret_value())
    saver = await closer.__aenter__()

    # Idempotent: creates the checkpoint tables on first run and is a no-op
    # afterwards. Called here rather than in a migration because the schema
    # belongs to LangGraph and may change between library versions.
    await saver.setup()

    logger.info("checkpointer.postgres_ready")
    return CheckpointerHandle(saver=saver, closer=closer)


async def close_checkpointer(handle: CheckpointerHandle) -> None:
    """Release a checkpointer's resources.

    Args:
        handle: The handle returned by `build_checkpointer`.
    """
    if handle.closer is not None:
        await handle.closer.__aexit__(None, None, None)
        logger.info("checkpointer.closed")


def trim_conversation(
    messages: list[AnyMessage],
    *,
    max_turns: int = RECENT_TURN_LIMIT,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> list[AnyMessage]:
    """Keep a bounded recent window of a conversation.

    Preserves any leading system message, then keeps the most recent messages
    within both limits.

    The subtle part is tool-call integrity. Chat APIs reject a `ToolMessage`
    whose originating `AIMessage` tool call is absent from the same request -
    it is a 400, not a degraded response. A naive "keep the last N messages"
    slice cuts exactly there surprisingly often, so this function walks
    forward from the cut point and drops orphaned tool results.

    Args:
        messages: Full conversation history.
        max_turns: Maximum non-system messages to keep.
        max_chars: Approximate character budget for kept content.

    Returns:
        The trimmed conversation, oldest first.
    """
    if not messages:
        return []

    system_messages = [m for m in messages if isinstance(m, SystemMessage)]
    conversation = [m for m in messages if not isinstance(m, SystemMessage)]

    kept: list[AnyMessage] = []
    total_chars = 0

    for message in reversed(conversation):
        content_length = len(str(message.content))
        if kept and (len(kept) >= max_turns or total_chars + content_length > max_chars):
            break
        kept.append(message)
        total_chars += content_length

    kept.reverse()
    return [*system_messages, *_drop_orphan_tool_messages(kept)]


def _drop_orphan_tool_messages(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Remove tool results whose originating tool call was trimmed away.

    Args:
        messages: A candidate message window, oldest first.

    Returns:
        The window with orphaned `ToolMessage` entries removed.
    """
    available_call_ids: set[str] = set()
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                call_id = call.get("id")
                if call_id:
                    available_call_ids.add(call_id)

    cleaned: list[AnyMessage] = []
    for message in messages:
        if isinstance(message, ToolMessage) and message.tool_call_id not in available_call_ids:
            logger.debug("conversation.dropped_orphan_tool_message",
                         tool_call_id=message.tool_call_id)
            continue
        cleaned.append(message)

    return cleaned


def needs_summary(messages: list[AnyMessage], *, threshold: int = RECENT_TURN_LIMIT * 2) -> bool:
    """Whether a conversation has grown long enough to warrant summarising.

    Args:
        messages: Full conversation history.
        threshold: Message count above which summarisation is worthwhile.

    Returns:
        True if the conversation should be summarised.
    """
    return len([m for m in messages if not isinstance(m, SystemMessage)]) > threshold


async def summarise_conversation(
    messages: list[AnyMessage],
    *,
    settings: Settings | None = None,
) -> str:
    """Compress older conversation turns into a short brief.

    Summarises what matters for *continuing to plan* - decisions made,
    constraints stated, things rejected - rather than producing a neutral
    précis. A summary that omits "they rejected the Shibuya hotel as too
    noisy" will see that hotel suggested again two turns later, which reads to
    the user as the assistant not listening.

    Args:
        messages: Messages to compress.
        settings: Settings override, for tests.

    Returns:
        A plain-text summary, or an empty string if summarisation failed.
        Failure is non-fatal: the caller falls back to plain trimming.
    """
    cfg = settings or get_settings()

    transcript_lines: list[str] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            transcript_lines.append(f"Traveller: {message.content}")
        elif isinstance(message, AIMessage) and message.content:
            transcript_lines.append(f"Assistant: {str(message.content)[:500]}")

    if not transcript_lines:
        return ""

    prompt = (
        "Summarise this travel planning conversation so another assistant can pick it up "
        "with no other context. Capture, in at most 150 words:\n"
        "- the destination, dates and trip length agreed so far\n"
        "- any constraints or requirements the traveller stated\n"
        "- decisions already made and suggestions they explicitly rejected, and why\n"
        "- what is still open or undecided\n\n"
        "Omit pleasantries and anything already settled that will not affect what comes "
        "next. Write plain prose, no headings.\n\n"
        + "\n".join(transcript_lines)
    )

    try:
        model = get_model(ModelRole.UTILITY, settings=cfg)
        response: BaseMessage = await invoke_with_retry(
            model, [HumanMessage(content=prompt)], purpose="summarise_conversation"
        )
    except ExternalServiceError:
        logger.warning("conversation.summarisation_failed", exc_info=True)
        return ""

    summary = str(response.content).strip()
    logger.info("conversation.summarised", input_messages=len(messages), summary_chars=len(summary))
    return summary
