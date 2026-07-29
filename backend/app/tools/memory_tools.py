"""Tools giving the agent explicit control over long-term memory.

Long-term memory already works without these: relevant facts are retrieved and
injected before planning starts, and new facts are extracted in the background
afterwards. That automatic path is the primary mechanism.

These tools add a *hot path* on top, for the cases the automatic path handles
badly:

`recall_user_preferences` lets the agent look something up mid-conversation.
The pre-planning retrieval happens once, ranked against the opening message.
If the conversation then turns to dinner three turns later, the agent can ask
for dietary facts specifically rather than hoping they were in the original
top-k.

`save_user_preference` lets the agent record something immediately when the
user states it emphatically or corrects a mistake. Background extraction runs
after the response, so without this a correction made mid-conversation would
not apply until the *next* turn - and "no, I said I don't eat fish" needs to
take effect now.

Neither tool takes a user id. Identity comes from the request context, so the
model cannot address another user's memories. See `app/tools/context.py`.
"""

from __future__ import annotations

from typing import Final

from app.core.logging import get_logger
from app.memory.schemas import CandidateMemory, MemorySubject, MemoryType
from app.tools.base import ToolResult, resilient_tool
from app.tools.context import get_tool_context

logger = get_logger(__name__)

MEMORY_UNAVAILABLE: Final[str] = (
    "Stored traveller preferences could not be read right now. Continue using only "
    "what the traveller has said in this conversation, and ask them directly for "
    "anything you need. Do not retry this tool."
)

SAVE_UNAVAILABLE: Final[str] = (
    "The preference could not be saved. Carry on using it for the rest of this "
    "conversation, but do not claim it has been remembered for next time."
)


@resilient_tool(source="long-term-memory", unavailable_message=MEMORY_UNAVAILABLE)
async def recall_user_preferences(query: str, limit: int = 6) -> ToolResult:
    """Look up what is already known about this traveller.

    Relevant preferences are loaded automatically before planning begins, so
    reach for this only when the conversation turns to something the opening
    message did not cover - for example checking dietary needs before
    recommending restaurants, or budget before suggesting hotels.

    Args:
        query: What you want to know, phrased as a description of the facts
            you are after, e.g. `"dietary restrictions and food preferences"`
            or `"budget level and accommodation style"`.
        limit: Maximum facts to return, 1-15.

    Returns:
        A `ToolResult` whose data lists matching facts about the traveller.
    """
    context = get_tool_context()

    if context.memory_service is None:
        return ToolResult.degraded(source="long-term-memory", message=MEMORY_UNAVAILABLE)

    memory_context = await context.memory_service.recall(
        context.user_id, query, top_k=max(1, min(limit, 15))
    )

    if memory_context.is_empty:
        return ToolResult.ok(
            source="long-term-memory",
            data={"facts": []},
            message=(
                "Nothing is stored about this traveller yet. Ask them directly for what "
                "you need, and keep the questions brief."
            ),
        )

    facts = [
        {"fact": memory.content, "type": "constraint", "must_honour": True}
        for memory in memory_context.constraints
    ] + [
        {
            "fact": memory.content,
            "type": memory.memory_type.value,
            "must_honour": False,
            "relevance": round(memory.similarity, 2),
        }
        for memory in memory_context.preferences
    ]

    return ToolResult.ok(
        source="long-term-memory",
        data={"facts": facts},
        message=(
            f"{len(facts)} stored facts about this traveller. Entries marked "
            "must_honour are hard requirements and cannot be traded off. Apply all of "
            "this silently - do not read the list back to them."
        ),
    )


@resilient_tool(source="long-term-memory", unavailable_message=SAVE_UNAVAILABLE)
async def save_user_preference(
    fact: str,
    category: str = "preference",
    subject: str = "other",
) -> ToolResult:
    """Record a durable fact about the traveller for future conversations.

    Use this when they state something that will still matter on a completely
    different trip - a dietary requirement, a budget level, a travel style -
    and especially when they *correct* something you had wrong.

    Do not use it for trip-specific details (these dates, this hotel, this
    destination). Those are not useful next time and will clutter their
    profile.

    Args:
        fact: One atomic fact, third person, self-contained, e.g.
            `"Traveller is vegetarian and does not eat fish."` Never include
            names, contact details or payment information.
        category: One of `constraint` (a hard requirement that must always be
            honoured), `preference` (a soft leaning), `identity` (a stable
            personal attribute), or `experience` (somewhere they have been).
        subject: Which slot this occupies - `diet`, `allergy`,
            `accessibility`, `budget`, `pace`, `accommodation`, `transport`,
            `interests`, `climate`, `companions`, `pets`, `home_base`,
            `languages`, `visited`, `avoid`, or `other`. Used to detect when a
            later statement contradicts this one.

    Returns:
        A `ToolResult` confirming what was stored and how it was reconciled
        against existing memories.
    """
    context = get_tool_context()

    if context.memory_service is None:
        return ToolResult.degraded(source="long-term-memory", message=SAVE_UNAVAILABLE)

    try:
        memory_type = MemoryType(category.strip().lower())
    except ValueError:
        return ToolResult.invalid(
            source="long-term-memory",
            message=(
                f"{category!r} is not a valid category. Choose one of: "
                f"{', '.join(t.value for t in MemoryType)}."
            ),
        )

    try:
        memory_subject = MemorySubject(subject.strip().lower())
    except ValueError:
        # Falling back rather than rejecting: a wrong slot degrades
        # contradiction detection slightly, whereas losing the fact entirely
        # because the model guessed a slot name is a worse outcome.
        logger.info("memory.unknown_subject", supplied=subject, fallback="other")
        memory_subject = MemorySubject.OTHER

    service = context.memory_service
    candidate = CandidateMemory(
        memory_type=memory_type,
        subject=memory_subject,
        content=fact.strip(),
        # Written deliberately by the agent in response to a direct statement,
        # so this starts higher than a background-extracted inference - but
        # not at 1.0, since the agent is still interpreting.
        confidence=0.85,
        reasoning="Saved explicitly by the agent during the conversation.",
    )

    embedding = await service.embeddings.embed_documents([candidate.content])

    from app.memory.consolidator import consolidate_candidate

    outcome = await consolidate_candidate(
        context.user_id,
        candidate,
        embedding[0],
        service.store,
        source_session_id=context.session_id,
        source_lang=context.language,
        extractor_model="agent-explicit",
        settings=service.settings,
    )

    return ToolResult.ok(
        source="long-term-memory",
        data={
            "stored": candidate.content,
            "action": outcome.action.value,
            "reason": outcome.reason,
        },
        message=(
            f"Recorded ({outcome.action.value}). Do not mention to the traveller that you "
            "saved anything - just use it. Continue with what you were doing."
        ),
    )
