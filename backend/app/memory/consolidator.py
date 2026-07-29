"""Reconciling a new fact against what is already known.

Extraction alone produces a memory system that degrades over time. Tell the
assistant you are vegetarian in five separate sessions and a naive pipeline
stores five near-identical rows, all of which then compete for the same
retrieval slots and crowd out everything else. Change your mind about budget
and it stores the contradiction alongside the original, leaving the planner to
choose between "travels cheaply" and "happy to splurge" at random.

This module is the fix. Every candidate is compared against existing memories
before it is written, and lands in one of three bands of cosine similarity:

    similarity >= 0.90          -> REINFORCE. Same fact, said again. Bump the
                                   mention count and confidence; write nothing
                                   new. This is what stops unbounded growth.

    0.75 <= similarity < 0.90   -> ARBITRATE. Related but not identical, so it
                                   could be a contradiction ("budget: cheap"
                                   vs "budget: happy to splurge") or a genuine
                                   refinement ("likes museums" vs "likes
                                   modern art museums"). One cheap model call
                                   decides.

    similarity < 0.75           -> INSERT. Genuinely new.

The arbitration band is narrow by design. Model calls cost quota, so the
expensive path only runs for the small set of candidates where similarity
alone cannot distinguish a contradiction from a refinement. Comparison is also
scoped to the same subject slot, which keeps the neighbour set tiny.

When arbitration finds a contradiction, the older memory is marked superseded
and points at its replacement rather than being deleted. Retrieval ignores it;
the admin inspector can still show how someone's profile evolved, which is the
first thing you want when diagnosing an extractor that has learned something
wrong.
"""

from __future__ import annotations

from typing import Final, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.memory.schemas import (
    CandidateMemory,
    ConsolidationAction,
    ConsolidationOutcome,
    StoredMemory,
)
from app.memory.store import MemoryStore
from app.services.llm import ModelRole, structured_call

logger = get_logger(__name__)


class MemoryRelationship(BaseModel):
    """How a new fact relates to an existing one in the same slot."""

    relationship: Literal["duplicate", "contradiction", "compatible"] = Field(
        description=(
            "duplicate = the same fact restated, adding nothing new. "
            "contradiction = both cannot be true of the same person at the same "
            "time, so the newer one replaces the older. "
            "compatible = both can be true simultaneously; keep both."
        )
    )
    reasoning: str = Field(
        default="", max_length=200, description="One sentence justifying the choice."
    )


ARBITRATION_PROMPT: Final[str] = """\
You compare two facts about the same traveller and classify their relationship.

Choose exactly one:

duplicate     - The same fact, reworded. Nothing new is added.
                'Traveller is vegetarian.' vs 'Traveller does not eat meat.'

contradiction - Both cannot be true of the same person at the same time. The
                newer statement represents a change of circumstance or a
                correction, and should replace the older one.
                'Traveller travels on a tight budget.' vs 'Traveller is happy
                to spend freely on hotels.'

compatible    - Both can be true at once. Keep both.
                'Traveller likes museums.' vs 'Traveller likes hiking.'
                'Traveller is vegetarian.' vs 'Traveller avoids spicy food.'

Be conservative about contradiction. A more specific version of the same
preference is a refinement, not a contradiction: 'likes museums' and 'likes
modern art museums' are compatible. Only choose contradiction when believing
both at once would produce genuinely incoherent travel advice.\
"""


async def _classify_relationship(
    candidate: CandidateMemory,
    existing: StoredMemory,
    settings: Settings,
) -> MemoryRelationship:
    """Ask the utility model how two facts relate.

    Args:
        candidate: The newly extracted fact.
        existing: The stored fact it resembles.
        settings: Application settings.

    Returns:
        The classified relationship. Defaults to `compatible` if the model
        call fails - keeping both facts is the safe failure mode, since
        wrongly discarding a constraint could produce harmful advice while
        wrongly keeping one merely costs a retrieval slot.
    """
    try:
        return await structured_call(
            ModelRole.UTILITY,
            [
                SystemMessage(content=ARBITRATION_PROMPT),
                HumanMessage(
                    content=(
                        f"EXISTING (stored {existing.mention_count}x): {existing.content}\n"
                        f"NEW: {candidate.content}"
                    )
                ),
            ],
            MemoryRelationship,
            purpose="arbitrate_memory_conflict",
            settings=settings,
        )
    except ExternalServiceError:
        logger.warning(
            "memory.arbitration_failed",
            existing_id=existing.id,
            fallback="compatible",
            exc_info=True,
        )
        return MemoryRelationship(
            relationship="compatible", reasoning="Arbitration unavailable; kept both."
        )


async def consolidate_candidate(
    user_id: str,
    candidate: CandidateMemory,
    embedding: list[float],
    store: MemoryStore,
    *,
    source_message_id: str | None = None,
    source_session_id: str | None = None,
    source_lang: str | None = None,
    extractor_model: str | None = None,
    settings: Settings | None = None,
) -> ConsolidationOutcome:
    """Reconcile one candidate against existing memories and persist the result.

    Args:
        user_id: Owner of the memory.
        candidate: The extracted fact.
        embedding: The candidate's embedding.
        store: Where memories live.
        source_message_id: Message this came from, for provenance.
        source_session_id: Session this came from.
        source_lang: Language the user originally wrote in.
        extractor_model: Which model performed the extraction.
        settings: Settings override, for tests.

    Returns:
        What was done and why, for logging and the admin memory inspector.
    """
    cfg = settings or get_settings()

    # Scoped to the same subject slot: a dietary fact is only ever compared
    # against other dietary facts, which keeps the neighbour set small and the
    # arbitration call rare.
    neighbours = await store.find_neighbours(
        user_id, embedding, subject=candidate.subject.value, limit=3
    )

    if not neighbours:
        return await _insert(
            user_id, candidate, embedding, store,
            source_message_id=source_message_id,
            source_session_id=source_session_id,
            source_lang=source_lang,
            extractor_model=extractor_model,
            reason="No existing memory in this subject slot.",
        )

    closest, similarity = neighbours[0]

    # --- Band 1: duplicate ------------------------------------------------
    if similarity >= cfg.memory_dedupe_threshold:
        await store.reinforce(closest.id, confidence=candidate.confidence)
        logger.info(
            "memory.reinforced_existing",
            memory_id=closest.id,
            similarity=round(similarity, 3),
            subject=candidate.subject.value,
        )
        return ConsolidationOutcome(
            action=ConsolidationAction.REINFORCED,
            candidate=candidate,
            memory_id=closest.id,
            matched_memory_id=closest.id,
            similarity=similarity,
            reason=(
                f"Near-identical to an existing memory (similarity {similarity:.2f}); "
                "reinforced rather than duplicated."
            ),
        )

    # --- Band 2: related, needs arbitration -------------------------------
    if similarity >= cfg.memory_conflict_threshold:
        verdict = await _classify_relationship(candidate, closest, cfg)

        if verdict.relationship == "duplicate":
            await store.reinforce(closest.id, confidence=candidate.confidence)
            return ConsolidationOutcome(
                action=ConsolidationAction.REINFORCED,
                candidate=candidate,
                memory_id=closest.id,
                matched_memory_id=closest.id,
                similarity=similarity,
                reason=f"Judged a restatement: {verdict.reasoning}",
            )

        if verdict.relationship == "contradiction":
            new_id = await store.insert(
                user_id, candidate, embedding,
                source_message_id=source_message_id,
                source_session_id=source_session_id,
                source_lang=source_lang,
                extractor_model=extractor_model,
            )
            await store.supersede(closest.id, new_id)
            logger.info(
                "memory.contradiction_resolved",
                old_id=closest.id,
                new_id=new_id,
                subject=candidate.subject.value,
            )
            return ConsolidationOutcome(
                action=ConsolidationAction.SUPERSEDED,
                candidate=candidate,
                memory_id=new_id,
                matched_memory_id=closest.id,
                similarity=similarity,
                reason=f"Contradicted an earlier memory, which was retired: {verdict.reasoning}",
            )

        # compatible - fall through and insert alongside.
        return await _insert(
            user_id, candidate, embedding, store,
            source_message_id=source_message_id,
            source_session_id=source_session_id,
            source_lang=source_lang,
            extractor_model=extractor_model,
            reason=f"Related but independently true: {verdict.reasoning}",
        )

    # --- Band 3: genuinely new --------------------------------------------
    return await _insert(
        user_id, candidate, embedding, store,
        source_message_id=source_message_id,
        source_session_id=source_session_id,
        source_lang=source_lang,
        extractor_model=extractor_model,
        reason=f"No similar memory found (closest {similarity:.2f}).",
    )


async def _insert(
    user_id: str,
    candidate: CandidateMemory,
    embedding: list[float],
    store: MemoryStore,
    *,
    source_message_id: str | None,
    source_session_id: str | None,
    source_lang: str | None,
    extractor_model: str | None,
    reason: str,
) -> ConsolidationOutcome:
    """Persist a candidate as a new memory.

    Args:
        user_id: Owner.
        candidate: The fact to store.
        embedding: Its embedding.
        store: Where memories live.
        source_message_id: Provenance.
        source_session_id: Provenance.
        source_lang: Provenance.
        extractor_model: Provenance.
        reason: Why insertion was chosen, recorded for the admin inspector.

    Returns:
        The outcome describing the insertion.
    """
    memory_id = await store.insert(
        user_id, candidate, embedding,
        source_message_id=source_message_id,
        source_session_id=source_session_id,
        source_lang=source_lang,
        extractor_model=extractor_model,
    )
    return ConsolidationOutcome(
        action=ConsolidationAction.INSERTED,
        candidate=candidate,
        memory_id=memory_id,
        reason=reason,
    )
