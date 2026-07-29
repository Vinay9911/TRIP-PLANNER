"""Long-term memory: the read and write paths.

**Write path** (`remember`), run in the background after a response is sent:

    heuristic gate -> extract -> confidence floor -> embed
        -> consolidate (reinforce / arbitrate / insert) -> store

**Read path** (`recall`), run before planning starts:

    build profile query -> embed -> similarity search
        -> UNION every hard constraint -> rank -> render prompt block

The read path's union step is the design decision most worth defending.

A pure similarity search retrieves what is *relevant to this query*. That is
right for preferences: if someone is planning Kyoto, their note about liking
Nordic design need not surface. It is wrong, and potentially harmful, for
constraints. A nut allergy is relevant to every restaurant recommendation ever
made, but the sentence "plan me two days in Kyoto" has low cosine similarity
to "Traveller is allergic to nuts" - the words share almost no meaning. Under
pure ranking, the allergy gets ranked out by a note about liking temples.

So constraints are retrieved by *type*, unconditionally, and merged with the
similarity results. It costs a handful of extra tokens per turn and removes an
entire class of failure. This is a correctness decision dressed as a retrieval
decision, which is why it is not tunable by a threshold.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.memory.consolidator import consolidate_candidate
from app.memory.extractor import extract_memories
from app.memory.schemas import (
    ConsolidationOutcome,
    MemoryType,
    RetrievedMemory,
    StoredMemory,
)
from app.memory.store import MemoryStore
from app.services.embeddings import EmbeddingService

logger = get_logger(__name__)


@dataclass
class MemoryContext:
    """What the system knows about a traveller, ready for prompt injection.

    Attributes:
        constraints: Hard requirements, always included regardless of score.
        preferences: Similarity-ranked soft facts.
        memory_ids: Every id included, recorded on the run so an answer can be
            traced back to the memories that shaped it.
    """

    constraints: list[StoredMemory] = field(default_factory=list)
    preferences: list[RetrievedMemory] = field(default_factory=list)
    memory_ids: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when nothing is known about this traveller yet."""
        return not self.constraints and not self.preferences

    def as_prompt_block(self) -> str:
        """Render the context for injection into a system prompt.

        Constraints are listed first and marked, because a planner scanning a
        flat list treats position and formatting as importance signals.

        Returns:
            A formatted block, or an empty string when nothing is known.
        """
        if self.is_empty:
            return ""

        lines = ["WHAT YOU ALREADY KNOW ABOUT THIS TRAVELLER"]

        if self.constraints:
            lines.append("")
            lines.append("Hard requirements - these must be honoured in every suggestion:")
            lines.extend(f"  ! {memory.content}" for memory in self.constraints)

        if self.preferences:
            lines.append("")
            lines.append(
                "Preferences and history - use these to personalise, not to override "
                "anything the traveller says now:"
            )
            lines.extend(f"  - {memory.content}" for memory in self.preferences)

        lines.append("")
        lines.append(
            "This came from earlier conversations. Apply it silently - do not re-ask what "
            "you already know, and do not recite the list back to the traveller. If "
            "something they say now contradicts it, believe what they say now."
        )
        return "\n".join(lines)


class MemoryService:
    """Orchestrates long-term memory extraction, storage and retrieval.

    Attributes:
        store: Where memories live.
        embeddings: Embedding provider.
        settings: Application settings.
    """

    def __init__(
        self,
        store: MemoryStore,
        embeddings: EmbeddingService,
        settings: Settings | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            store: Memory persistence implementation.
            embeddings: Embedding provider.
            settings: Settings override, for tests.
        """
        self.store = store
        self.embeddings = embeddings
        self.settings = settings or get_settings()

    # -- Read path --------------------------------------------------------

    async def recall(
        self,
        user_id: str,
        query: str,
        *,
        top_k: int | None = None,
    ) -> MemoryContext:
        """Retrieve what is known about a traveller, relevant to a query.

        Args:
            user_id: Whose memories to retrieve.
            query: The current request, used to rank preferences. Hard
                constraints are returned regardless of this.
            top_k: Maximum preferences to include. Defaults to the configured
                value.

        Returns:
            A `MemoryContext`. Empty on failure rather than raising - a
            personalisation miss should degrade the answer, not break it.
        """
        limit = top_k or self.settings.memory_retrieval_top_k

        try:
            query_embedding = await self.embeddings.embed_query(query)
        except ExternalServiceError:
            logger.warning("memory.recall_embedding_failed", user_id=user_id, exc_info=True)
            return MemoryContext()

        try:
            # Both reads issued concurrently: they hit different indexes and
            # neither depends on the other, so serialising them would just add
            # a round trip to every planning turn.
            ranked, constraints = await asyncio.gather(
                self.store.search(
                    user_id,
                    query_embedding,
                    top_k=limit,
                    types=[
                        MemoryType.PREFERENCE,
                        MemoryType.IDENTITY,
                        MemoryType.EXPERIENCE,
                    ],
                ),
                self.store.get_by_type(user_id, MemoryType.CONSTRAINT),
            )
        except Exception:
            logger.warning("memory.recall_failed", user_id=user_id, exc_info=True)
            return MemoryContext()

        context = MemoryContext(
            constraints=constraints,
            preferences=ranked,
            memory_ids=[m.id for m in constraints] + [m.id for m in ranked],
        )

        logger.info(
            "memory.recalled",
            user_id=user_id,
            constraints=len(constraints),
            preferences=len(ranked),
        )
        return context

    async def build_profile_query(self, user_message: str, destination: str | None) -> str:
        """Compose the query used to rank preferences at the start of a turn.

        The user's raw message is a poor retrieval query on its own: "plan me
        two days in Kyoto" is mostly about Kyoto, so it ranks a memory about
        Japan above one about travel pace. Padding it with the dimensions a
        planner actually needs pulls the query toward the *kind* of facts
        worth recalling, rather than toward the destination.

        Args:
            user_message: What the traveller just asked.
            destination: Resolved destination, when known.

        Returns:
            A query string for embedding.
        """
        parts = [user_message.strip()]
        if destination:
            parts.append(f"Trip to {destination}.")
        parts.append(
            "Traveller's food preferences and dietary needs, budget level, travel pace, "
            "accommodation style, interests, accessibility needs, travel companions, "
            "and places previously visited."
        )
        return " ".join(parts)

    # -- Write path -------------------------------------------------------

    async def remember(
        self,
        user_id: str,
        user_message: str,
        *,
        assistant_reply: str | None = None,
        session_id: str | None = None,
        message_id: str | None = None,
    ) -> list[ConsolidationOutcome]:
        """Extract durable facts from a turn and reconcile them into storage.

        Intended to run as a background task after the response has been sent.
        Never raises: a failure here must not surface to a user who has
        already received their itinerary.

        Args:
            user_id: Whose memory to update.
            user_message: What the traveller said.
            assistant_reply: The reply, as disambiguation context only.
            session_id: Session for provenance.
            message_id: Message for provenance.

        Returns:
            One outcome per candidate, for logging and the admin inspector.
            Empty if nothing was extracted or extraction failed.
        """
        try:
            extraction = await extract_memories(
                user_message, assistant_reply=assistant_reply, settings=self.settings
            )
        except Exception:
            logger.warning("memory.remember_extraction_failed", user_id=user_id, exc_info=True)
            return []

        if not extraction.memories:
            return []

        try:
            # One batched embedding call for all candidates rather than one per
            # candidate - the provider bills per request as well as per token.
            vectors = await self.embeddings.embed_documents(
                [candidate.content for candidate in extraction.memories]
            )
        except ExternalServiceError:
            logger.warning("memory.remember_embedding_failed", user_id=user_id, exc_info=True)
            return []

        outcomes: list[ConsolidationOutcome] = []

        # Sequential on purpose. Two candidates from the same turn can be near
        # duplicates of each other ("I'm vegetarian" and "no meat for me"), and
        # consolidating them concurrently would race: both would see an empty
        # neighbour set and both would insert. Processing in order lets the
        # second one find the first.
        for candidate, vector in zip(extraction.memories, vectors, strict=True):
            try:
                outcome = await consolidate_candidate(
                    user_id,
                    candidate,
                    vector,
                    self.store,
                    source_message_id=message_id,
                    source_session_id=session_id,
                    source_lang=extraction.detected_language,
                    extractor_model=self.settings.llm_utility_model,
                    settings=self.settings,
                )
                outcomes.append(outcome)
            except Exception:
                logger.warning(
                    "memory.consolidation_failed",
                    user_id=user_id,
                    content=candidate.content[:80],
                    exc_info=True,
                )

        logger.info(
            "memory.remembered",
            user_id=user_id,
            candidates=len(extraction.memories),
            inserted=sum(1 for o in outcomes if o.action == "inserted"),
            reinforced=sum(1 for o in outcomes if o.action == "reinforced"),
            superseded=sum(1 for o in outcomes if o.action == "superseded"),
        )
        return outcomes
