"""Tests for the memory write path.

Consolidation is the most intricate logic in the project and the part where a
subtle bug is least visible in normal use - a system that quietly stores five
copies of the same fact still *works*, it just degrades. These tests pin the
three similarity bands and the lifecycle rules that keep it honest.
"""

from __future__ import annotations

import pytest

from app.memory.consolidator import consolidate_candidate
from app.memory.extractor import _apply_confidence_floor, looks_worth_extracting
from app.memory.schemas import (
    CandidateMemory,
    ConsolidationAction,
    MemoryStatus,
    MemorySubject,
    MemoryType,
)
from app.memory.service import MemoryContext
from app.memory.store import InMemoryMemoryStore


def make_candidate(
    content: str,
    *,
    memory_type: MemoryType = MemoryType.PREFERENCE,
    subject: MemorySubject = MemorySubject.INTERESTS,
    confidence: float = 0.9,
) -> CandidateMemory:
    """Build a candidate memory for a test."""
    return CandidateMemory(
        memory_type=memory_type, subject=subject, content=content, confidence=confidence
    )


async def store_candidate(
    candidate: CandidateMemory,
    *,
    user_id: str,
    store: InMemoryMemoryStore,
    embeddings,
    settings,
):
    """Embed and consolidate a candidate, returning the outcome."""
    vector = (await embeddings.embed_documents([candidate.content]))[0]
    return await consolidate_candidate(
        user_id, candidate, vector, store, settings=settings
    )


# ---------------------------------------------------------------------------
# Band 3: genuinely new
# ---------------------------------------------------------------------------


async def test_first_memory_is_inserted(user_id, memory_store, embeddings, settings):
    outcome = await store_candidate(
        make_candidate("Traveller is vegetarian and does not eat fish.",
                       memory_type=MemoryType.CONSTRAINT, subject=MemorySubject.DIET),
        user_id=user_id, store=memory_store, embeddings=embeddings, settings=settings,
    )

    assert outcome.action is ConsolidationAction.INSERTED
    assert outcome.memory_id is not None

    stored = await memory_store.list_for_user(user_id)
    assert len(stored) == 1
    assert stored[0].mention_count == 1


async def test_unrelated_facts_coexist(user_id, memory_store, embeddings, settings):
    await store_candidate(
        make_candidate("Traveller enjoys visiting art museums and galleries.",
                       subject=MemorySubject.INTERESTS),
        user_id=user_id, store=memory_store, embeddings=embeddings, settings=settings,
    )
    outcome = await store_candidate(
        make_candidate("Traveller prefers hiking mountain trails outdoors.",
                       subject=MemorySubject.INTERESTS),
        user_id=user_id, store=memory_store, embeddings=embeddings, settings=settings,
    )

    assert outcome.action is ConsolidationAction.INSERTED
    assert len(await memory_store.list_for_user(user_id)) == 2


# ---------------------------------------------------------------------------
# Band 1: duplicates are reinforced, not re-stored
# ---------------------------------------------------------------------------


async def test_identical_fact_reinforces_instead_of_duplicating(
    user_id, memory_store, embeddings, settings
):
    """The anti-hoarding guarantee: repeating a fact must not grow the store."""
    content = "Traveller is vegetarian and does not eat fish."

    first = await store_candidate(
        make_candidate(content, memory_type=MemoryType.CONSTRAINT, subject=MemorySubject.DIET),
        user_id=user_id, store=memory_store, embeddings=embeddings, settings=settings,
    )
    second = await store_candidate(
        make_candidate(content, memory_type=MemoryType.CONSTRAINT, subject=MemorySubject.DIET),
        user_id=user_id, store=memory_store, embeddings=embeddings, settings=settings,
    )

    assert first.action is ConsolidationAction.INSERTED
    assert second.action is ConsolidationAction.REINFORCED
    assert second.matched_memory_id == first.memory_id

    stored = await memory_store.list_for_user(user_id)
    assert len(stored) == 1, "restating a fact must not create a second row"
    assert stored[0].mention_count == 2


async def test_reinforcement_raises_confidence_but_caps_at_one(
    user_id, memory_store, embeddings, settings
):
    content = "Traveller prefers slow relaxed itineraries with few activities."
    candidate = make_candidate(content, subject=MemorySubject.PACE, confidence=0.7)

    for _ in range(8):
        await store_candidate(
            candidate, user_id=user_id, store=memory_store,
            embeddings=embeddings, settings=settings,
        )

    stored = (await memory_store.list_for_user(user_id))[0]
    assert stored.mention_count == 8
    assert stored.confidence <= 1.0


# ---------------------------------------------------------------------------
# Band 2: arbitration
# ---------------------------------------------------------------------------


async def test_contradiction_supersedes_the_older_memory(
    user_id, memory_store, embeddings, settings, monkeypatch
):
    """A changed budget must retire the old one, not sit beside it."""
    from app.memory import consolidator
    from app.memory.consolidator import MemoryRelationship

    async def fake_classify(candidate, existing, cfg):
        return MemoryRelationship(relationship="contradiction", reasoning="Budget changed.")

    monkeypatch.setattr(consolidator, "_classify_relationship", fake_classify)

    old = await store_candidate(
        make_candidate("Traveller travels on a tight budget.", subject=MemorySubject.BUDGET),
        user_id=user_id, store=memory_store, embeddings=embeddings, settings=settings,
    )
    # Overlapping wording puts this in the arbitration band rather than the
    # duplicate band.
    new = await store_candidate(
        make_candidate("Traveller travels on a generous budget.", subject=MemorySubject.BUDGET),
        user_id=user_id, store=memory_store, embeddings=embeddings, settings=settings,
    )

    assert new.action is ConsolidationAction.SUPERSEDED

    active = await memory_store.list_for_user(user_id)
    assert len(active) == 1
    assert active[0].id == new.memory_id

    everything = await memory_store.list_for_user(user_id, include_inactive=True)
    retired = next(m for m in everything if m.id == old.memory_id)
    assert retired.status is MemoryStatus.SUPERSEDED
    assert retired.superseded_by == new.memory_id, "audit trail must survive"


async def test_compatible_refinement_keeps_both(
    user_id, memory_store, embeddings, settings, monkeypatch
):
    from app.memory import consolidator
    from app.memory.consolidator import MemoryRelationship

    async def fake_classify(candidate, existing, cfg):
        return MemoryRelationship(relationship="compatible", reasoning="A refinement.")

    monkeypatch.setattr(consolidator, "_classify_relationship", fake_classify)

    await store_candidate(
        make_candidate("Traveller enjoys visiting museums often.", subject=MemorySubject.INTERESTS),
        user_id=user_id, store=memory_store, embeddings=embeddings, settings=settings,
    )
    outcome = await store_candidate(
        make_candidate("Traveller enjoys visiting modern galleries often.",
                       subject=MemorySubject.INTERESTS),
        user_id=user_id, store=memory_store, embeddings=embeddings, settings=settings,
    )

    assert outcome.action is ConsolidationAction.INSERTED
    assert len(await memory_store.list_for_user(user_id)) == 2


async def test_arbitration_failure_keeps_both_memories(
    user_id, memory_store, embeddings, settings, monkeypatch
):
    """If the arbitrating model is unreachable, never silently drop a fact."""
    from app.core.errors import ExternalServiceError
    from app.memory import consolidator

    async def exploding_call(*args, **kwargs):
        raise ExternalServiceError("provider down", service="groq")

    monkeypatch.setattr(consolidator, "structured_call", exploding_call)

    await store_candidate(
        make_candidate("Traveller travels on a tight budget.", subject=MemorySubject.BUDGET),
        user_id=user_id, store=memory_store, embeddings=embeddings, settings=settings,
    )
    outcome = await store_candidate(
        make_candidate("Traveller travels on a generous budget.", subject=MemorySubject.BUDGET),
        user_id=user_id, store=memory_store, embeddings=embeddings, settings=settings,
    )

    assert outcome.action is ConsolidationAction.INSERTED
    assert len(await memory_store.list_for_user(user_id)) == 2


# ---------------------------------------------------------------------------
# Slot scoping
# ---------------------------------------------------------------------------


async def test_contradiction_detection_is_scoped_to_a_subject_slot(
    user_id, memory_store, embeddings, settings, monkeypatch
):
    """A dietary fact must never be compared against a budget fact."""
    from app.memory import consolidator

    seen: list[tuple[str, str]] = []

    async def recording_classify(candidate, existing, cfg):
        seen.append((candidate.subject.value, existing.subject))
        from app.memory.consolidator import MemoryRelationship

        return MemoryRelationship(relationship="compatible")

    monkeypatch.setattr(consolidator, "_classify_relationship", recording_classify)

    await store_candidate(
        make_candidate("Traveller avoids eating meat entirely.",
                       memory_type=MemoryType.CONSTRAINT, subject=MemorySubject.DIET),
        user_id=user_id, store=memory_store, embeddings=embeddings, settings=settings,
    )
    await store_candidate(
        make_candidate("Traveller avoids eating expensive restaurants.",
                       subject=MemorySubject.BUDGET),
        user_id=user_id, store=memory_store, embeddings=embeddings, settings=settings,
    )

    assert all(pair[0] == pair[1] for pair in seen), (
        "arbitration compared memories from different subject slots"
    )


async def test_memories_are_isolated_between_users(memory_store, embeddings, settings):
    alice = "aaaaaaaa-1111-4111-8111-111111111111"
    bob = "bbbbbbbb-2222-4222-8222-222222222222"
    content = "Traveller is vegetarian and does not eat fish."

    await store_candidate(
        make_candidate(content, memory_type=MemoryType.CONSTRAINT, subject=MemorySubject.DIET),
        user_id=alice, store=memory_store, embeddings=embeddings, settings=settings,
    )
    outcome = await store_candidate(
        make_candidate(content, memory_type=MemoryType.CONSTRAINT, subject=MemorySubject.DIET),
        user_id=bob, store=memory_store, embeddings=embeddings, settings=settings,
    )

    # Bob's identical fact must not be treated as a duplicate of Alice's.
    assert outcome.action is ConsolidationAction.INSERTED
    assert len(await memory_store.list_for_user(alice)) == 1
    assert len(await memory_store.list_for_user(bob)) == 1


# ---------------------------------------------------------------------------
# Extraction gate and filters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ok", False),
        ("thanks!", False),
        ("sounds good", False),
        ("what is the weather there", False),
        ("I am vegetarian", True),
        ("We travel with our dog", True),
        ("My budget is about 100 a night", True),
        ("We visited Rome last year and found it too crowded", True),
        ("I prefer a slow relaxed pace", True),
    ],
)
def test_heuristic_gate_admits_facts_and_skips_noise(message, expected):
    assert looks_worth_extracting(message) is expected


@pytest.mark.parametrize(
    "message",
    [
        "私はベジタリアンです",       # Japanese: "I am vegetarian" - 10 chars
        "أنا نباتي",                  # Arabic: "I am vegetarian" - 9 chars
        "我是素食主义者",              # Chinese: "I am a vegetarian" - 7 chars
        "저는 채식주의자입니다",        # Korean: "I am a vegetarian"
    ],
)
def test_heuristic_gate_admits_short_non_latin_statements(message):
    """Regression: a flat character floor disabled extraction for CJK users.

    These are complete, durable statements of a dietary constraint, but each
    is shorter than the twelve-character minimum tuned for English. Applying
    that floor to them switched off long-term memory for exactly the users the
    multilingual support exists to serve, with no error to reveal it.
    """
    assert looks_worth_extracting(message) is True


def test_confidence_floor_discards_low_confidence_candidates(settings):
    candidates = [
        make_candidate("Traveller is vegetarian.", confidence=0.95),
        make_candidate("Traveller might like jazz.", confidence=0.3),
        make_candidate("Traveller prefers trains.", confidence=0.61),
    ]

    kept = _apply_confidence_floor(candidates, settings.memory_min_confidence)

    assert len(kept) == 2
    assert all(candidate.confidence >= settings.memory_min_confidence for candidate in kept)


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def test_prompt_block_marks_constraints_distinctly():
    from app.memory.schemas import StoredMemory

    context = MemoryContext(
        constraints=[
            StoredMemory(
                id="1", user_id="u", memory_type=MemoryType.CONSTRAINT, subject="allergy",
                content="Traveller is allergic to nuts.", confidence=1.0, mention_count=3,
            )
        ],
        preferences=[],
    )

    block = context.as_prompt_block()

    assert "Traveller is allergic to nuts." in block
    assert "Hard requirements" in block
    assert "must be honoured" in block


def test_empty_memory_context_renders_nothing():
    assert MemoryContext().as_prompt_block() == ""
    assert MemoryContext().is_empty is True
