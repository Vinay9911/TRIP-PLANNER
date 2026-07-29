"""Domain types for long-term memory.

The shape of these models *is* the memory policy. Rather than writing a long
prompt asking a model to "extract important facts" and hoping, the extractor
is handed a schema whose fields it must fill, with a constrained vocabulary
for the fields that matter. A model cannot invent a new memory category or a
free-form subject key, because the schema will not validate.

Two constrained vocabularies do most of the work:

`MemoryType` decides *retrieval policy*. Constraints are injected into every
planning turn unconditionally; preferences compete on similarity. Getting this
label right is the difference between "we noticed you like museums" and
"we forgot about your nut allergy".

`MemorySubject` decides *contradiction scope*. Two memories can only supersede
one another if they occupy the same slot, so a new budget statement replaces
the old budget rather than competing with an unrelated dietary fact. A free
text subject would make this unreliable - "budget" and "spending" would look
like different slots to a string comparison and like the same slot to a human.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints


class MemoryType(StrEnum):
    """Category of a remembered fact, which determines how it is retrieved."""

    #: Soft leanings: likes museums, prefers walking, dislikes crowds.
    #: Retrieved by similarity and subject to time decay.
    PREFERENCE = "preference"

    #: Hard requirements: dietary needs, allergies, mobility, pets, budget
    #: ceilings. Injected into every planning turn regardless of similarity
    #: score, and never decayed. Getting one of these wrong is a real-world
    #: harm, not a relevance miss.
    CONSTRAINT = "constraint"

    #: Stable attributes: home city, languages spoken, who they travel with.
    #: Long-lived and not decayed.
    IDENTITY = "identity"

    #: Travel history and verdicts: visited Rome in 2024, found Bali too busy.
    #: Useful for avoiding repeats and calibrating suggestions.
    EXPERIENCE = "experience"


class MemorySubject(StrEnum):
    """Slot a memory occupies, scoping contradiction detection.

    Closed vocabulary on purpose: a new statement can only supersede an older
    one within the same slot.
    """

    DIET = "diet"
    ALLERGY = "allergy"
    ACCESSIBILITY = "accessibility"
    BUDGET = "budget"
    PACE = "pace"
    ACCOMMODATION = "accommodation"
    TRANSPORT = "transport"
    INTERESTS = "interests"
    CLIMATE = "climate"
    COMPANIONS = "companions"
    PETS = "pets"
    HOME_BASE = "home_base"
    LANGUAGES = "languages"
    VISITED = "visited"
    AVOID = "avoid"
    OTHER = "other"


class MemoryStatus(StrEnum):
    """Lifecycle state of a stored memory."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


# Content is capped so one rambling message cannot produce a memory that
# dominates the retrieval budget. If a fact needs more than ~300 characters it
# is not atomic and should have been split into several memories.
MemoryContent = Annotated[
    str, StringConstraints(min_length=3, max_length=300, strip_whitespace=True)
]


class CandidateMemory(BaseModel):
    """A fact the extractor proposes storing, before consolidation.

    Not yet persisted: it still has to survive the confidence filter,
    deduplication against existing memories, and contradiction arbitration.
    """

    memory_type: MemoryType = Field(
        description=(
            "constraint = a hard requirement that must always be honoured (dietary, "
            "allergy, accessibility, pets, budget ceiling). preference = a soft "
            "leaning. identity = a stable attribute of the person. experience = "
            "somewhere they have been and what they thought of it."
        )
    )
    subject: MemorySubject = Field(
        description="Which slot this fact occupies. Used to detect contradictions later."
    )
    content: MemoryContent = Field(
        description=(
            "One atomic fact, written in English in the third person, e.g. "
            "'Traveller is vegetarian and does not eat fish.' Must be understandable "
            "with no surrounding conversation. Never include names, contact details, "
            "payment information, or anything identifying beyond travel preferences."
        )
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "How certain you are this is a durable fact about the traveller. "
            "Use 0.9+ for explicit statements ('I am vegetarian'), 0.6-0.8 for clear "
            "implications, below 0.5 for guesses - which will be discarded."
        ),
    )
    reasoning: str = Field(
        default="",
        max_length=200,
        description="One sentence on why this is durable rather than trip-specific.",
    )


class ExtractionResult(BaseModel):
    """Everything the extractor found in one conversational turn."""

    memories: list[CandidateMemory] = Field(
        default_factory=list,
        description=(
            "Durable facts worth remembering across future, unrelated trips. "
            "Return an empty list when the turn contains none - that is the common "
            "case and is always preferable to inventing something."
        ),
    )
    detected_language: str = Field(
        default="en",
        max_length=12,
        description="BCP-47 tag of the language the user wrote in, e.g. 'en', 'ja', 'es'.",
    )


class StoredMemory(BaseModel):
    """A memory as persisted, with its provenance and reinforcement history."""

    id: str
    user_id: str
    memory_type: MemoryType
    subject: str
    content: str
    confidence: float
    mention_count: int
    status: MemoryStatus = MemoryStatus.ACTIVE
    source_lang: str | None = None
    source_message_id: str | None = None
    source_session_id: str | None = None
    superseded_by: str | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None

    def as_prompt_line(self) -> str:
        """Render this memory for injection into a system prompt.

        Constraints are marked so the planner cannot treat a hard requirement
        as a soft preference when scanning a list.

        Returns:
            A single formatted line.
        """
        marker = "[MUST HONOUR]" if self.memory_type == MemoryType.CONSTRAINT else "-"
        return f"{marker} {self.content}"


class RetrievedMemory(StoredMemory):
    """A stored memory returned from a similarity search, with its scores.

    Attributes:
        similarity: Raw cosine similarity to the query, 0-1.
        salience: Weight from confidence, reinforcement and recency.
        score: The product used for ranking.
        always_included: True when this memory was returned because of its
            type rather than because it scored well - the mechanism that keeps
            hard constraints from being ranked out of the context window.
    """

    similarity: float = 0.0
    salience: float = 0.0
    score: float = 0.0
    always_included: bool = False


class ConsolidationAction(StrEnum):
    """What the consolidator decided to do with a candidate."""

    #: No sufficiently similar memory exists.
    INSERTED = "inserted"
    #: A near-identical memory exists; its counters were bumped instead.
    REINFORCED = "reinforced"
    #: Contradicts an existing memory in the same slot; the old one was retired.
    SUPERSEDED = "superseded"
    #: Rejected by the confidence floor or the content policy.
    DISCARDED = "discarded"


class ConsolidationOutcome(BaseModel):
    """Result of processing one candidate, recorded for the admin inspector."""

    action: ConsolidationAction
    candidate: CandidateMemory
    memory_id: str | None = None
    matched_memory_id: str | None = None
    similarity: float | None = None
    reason: str = ""
