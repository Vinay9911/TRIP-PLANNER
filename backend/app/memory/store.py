"""Persistence for long-term memory.

Two implementations behind one Protocol:

`PostgresMemoryStore` is the real one. It delegates similarity search to the
`match_memories` and `find_similar_memories` functions defined in migration
0003 rather than assembling SQL here, so the ranking formula lives beside the
data it ranks and the query is written in the exact form pgvector's HNSW index
can serve.

`InMemoryMemoryStore` exists so the memory pipeline can be unit-tested with no
database and no network. That matters more than it sounds: the consolidation
logic - deduplicate, arbitrate, reinforce - is the most intricate code in this
project and the part most worth testing exhaustively. Tying those tests to a
live Postgres would make them slow enough that they would stop being run.

All user-facing reads and writes go through `Database.user_scope`, so row level
security applies. The one exception is the background extractor, which runs
after the HTTP response has been sent and therefore outside any request's
authenticated context; it uses `service_scope` and passes `user_id` explicitly.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from app.core.logging import get_logger
from app.db.session import Database
from app.memory.schemas import (
    CandidateMemory,
    MemoryStatus,
    MemoryType,
    RetrievedMemory,
    StoredMemory,
)

logger = get_logger(__name__)


class MemoryStore(Protocol):
    """Storage interface for long-term memories."""

    async def search(
        self,
        user_id: str,
        query_embedding: list[float],
        *,
        top_k: int = 8,
        min_similarity: float = 0.35,
        types: list[MemoryType] | None = None,
    ) -> list[RetrievedMemory]:
        """Find memories semantically similar to a query."""
        ...

    async def get_by_type(self, user_id: str, memory_type: MemoryType) -> list[StoredMemory]:
        """Fetch every active memory of one type, ignoring similarity."""
        ...

    async def find_neighbours(
        self,
        user_id: str,
        embedding: list[float],
        *,
        subject: str | None = None,
        limit: int = 5,
    ) -> list[tuple[StoredMemory, float]]:
        """Find the closest existing memories to a candidate, for consolidation."""
        ...

    async def insert(
        self,
        user_id: str,
        candidate: CandidateMemory,
        embedding: list[float],
        *,
        source_message_id: str | None = None,
        source_session_id: str | None = None,
        source_lang: str | None = None,
        extractor_model: str | None = None,
    ) -> str:
        """Store a new memory and return its id."""
        ...

    async def reinforce(self, memory_id: str, *, confidence: float) -> None:
        """Record that an existing memory was independently restated."""
        ...

    async def supersede(self, old_id: str, new_id: str) -> None:
        """Retire a memory that a newer, contradictory one replaces."""
        ...

    async def list_for_user(
        self, user_id: str, *, include_inactive: bool = False, limit: int = 200
    ) -> list[StoredMemory]:
        """List a user's memories, newest first."""
        ...

    async def delete(self, memory_id: str, user_id: str) -> bool:
        """Soft-delete a memory. Returns whether a row was affected."""
        ...


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------

_MEMORY_COLUMNS = """
    id::text, user_id::text, memory_type, subject, content, confidence,
    mention_count, status, source_lang, source_message_id::text,
    source_session_id::text, superseded_by::text, first_seen_at, last_seen_at
"""


def _row_to_memory(row: dict[str, Any]) -> StoredMemory:
    """Map a database row onto a `StoredMemory`.

    Args:
        row: A row from a query selecting `_MEMORY_COLUMNS`.

    Returns:
        The corresponding domain object.
    """
    return StoredMemory(
        id=row["id"],
        user_id=row["user_id"],
        memory_type=MemoryType(row["memory_type"]),
        subject=row["subject"],
        content=row["content"],
        confidence=float(row["confidence"]),
        mention_count=int(row["mention_count"]),
        status=MemoryStatus(row.get("status", "active")),
        source_lang=row.get("source_lang"),
        source_message_id=row.get("source_message_id"),
        source_session_id=row.get("source_session_id"),
        superseded_by=row.get("superseded_by"),
        first_seen_at=row.get("first_seen_at"),
        last_seen_at=row.get("last_seen_at"),
    )


class PostgresMemoryStore:
    """pgvector-backed memory storage.

    Attributes:
        db: The connection pool wrapper.
    """

    def __init__(self, db: Database) -> None:
        """Initialise the store.

        Args:
            db: Connection pool wrapper providing RLS-scoped connections.
        """
        self.db = db

    async def search(
        self,
        user_id: str,
        query_embedding: list[float],
        *,
        top_k: int = 8,
        min_similarity: float = 0.35,
        types: list[MemoryType] | None = None,
    ) -> list[RetrievedMemory]:
        """Find memories semantically similar to a query.

        Args:
            user_id: Whose memories to search.
            query_embedding: Embedded query vector.
            top_k: Maximum results.
            min_similarity: Cosine similarity floor.
            types: Restrict to these memory types, or None for all.

        Returns:
            Memories ranked by similarity x salience, highest first.
        """
        type_values = [t.value for t in types] if types else None

        async with self.db.user_scope(user_id) as conn:
            cursor = await conn.execute(
                """
                select id::text, memory_type, subject, content, confidence,
                       mention_count, last_seen_at, similarity, salience, score
                  from public.match_memories(%s, %s::vector, %s, %s, %s::public.memory_type[])
                """,
                (user_id, query_embedding, top_k, min_similarity, type_values),
            )
            rows = await cursor.fetchall()

        return [
            RetrievedMemory(
                id=row["id"],
                user_id=user_id,
                memory_type=MemoryType(row["memory_type"]),
                subject=row["subject"],
                content=row["content"],
                confidence=float(row["confidence"]),
                mention_count=int(row["mention_count"]),
                last_seen_at=row.get("last_seen_at"),
                similarity=float(row["similarity"]),
                salience=float(row["salience"]),
                score=float(row["score"]),
            )
            for row in rows
        ]

    async def get_by_type(self, user_id: str, memory_type: MemoryType) -> list[StoredMemory]:
        """Fetch every active memory of one type.

        Used for the unconditional constraint injection path, which
        deliberately bypasses similarity ranking.

        Args:
            user_id: Whose memories to fetch.
            memory_type: The type to fetch.

        Returns:
            Matching memories, most confident first.
        """
        async with self.db.user_scope(user_id) as conn:
            # S608: the only interpolated value is the _MEMORY_COLUMNS module
            # constant - a fixed column list, not caller input. Every value is
            # passed as a bound parameter.
            cursor = await conn.execute(
                f"""
                select {_MEMORY_COLUMNS}
                  from public.memories
                 where user_id = %s
                   and status = 'active'
                   and memory_type = %s::public.memory_type
                 order by confidence desc, mention_count desc
                """,  # noqa: S608
                (user_id, memory_type.value),
            )
            rows = await cursor.fetchall()

        return [_row_to_memory(row) for row in rows]

    async def find_neighbours(
        self,
        user_id: str,
        embedding: list[float],
        *,
        subject: str | None = None,
        limit: int = 5,
    ) -> list[tuple[StoredMemory, float]]:
        """Find the closest existing memories to a candidate.

        Args:
            user_id: Whose memories to search.
            embedding: The candidate's embedding.
            subject: Restrict to one slot, which is what keeps contradiction
                arbitration cheap.
            limit: Maximum neighbours to return.

        Returns:
            Pairs of (memory, cosine similarity), closest first.
        """
        async with self.db.service_scope(reason="memory consolidation neighbour lookup") as conn:
            cursor = await conn.execute(
                """
                select id::text, memory_type, subject, content, confidence,
                       mention_count, similarity
                  from public.find_similar_memories(%s, %s::vector, %s, %s)
                """,
                (user_id, embedding, subject, limit),
            )
            rows = await cursor.fetchall()

        return [
            (
                StoredMemory(
                    id=row["id"],
                    user_id=user_id,
                    memory_type=MemoryType(row["memory_type"]),
                    subject=row["subject"],
                    content=row["content"],
                    confidence=float(row["confidence"]),
                    mention_count=int(row["mention_count"]),
                ),
                float(row["similarity"]),
            )
            for row in rows
        ]

    async def insert(
        self,
        user_id: str,
        candidate: CandidateMemory,
        embedding: list[float],
        *,
        source_message_id: str | None = None,
        source_session_id: str | None = None,
        source_lang: str | None = None,
        extractor_model: str | None = None,
    ) -> str:
        """Store a new memory.

        Args:
            user_id: Owner.
            candidate: The extracted fact.
            embedding: Its embedding.
            source_message_id: Message this was derived from, for provenance.
            source_session_id: Session this was derived from.
            source_lang: Language the user originally wrote in.
            extractor_model: Which model performed the extraction.

        Returns:
            The new memory's id.
        """
        async with self.db.service_scope(reason="memory extraction insert") as conn:
            cursor = await conn.execute(
                """
                insert into public.memories (
                    user_id, memory_type, subject, content, embedding,
                    confidence, source_message_id, source_session_id,
                    source_lang, extractor_model
                )
                values (%s, %s::public.memory_type, %s, %s, %s::vector,
                        %s, %s, %s, %s, %s)
                returning id::text
                """,
                (
                    user_id,
                    candidate.memory_type.value,
                    candidate.subject.value,
                    candidate.content,
                    embedding,
                    candidate.confidence,
                    source_message_id,
                    source_session_id,
                    source_lang,
                    extractor_model,
                ),
            )
            row = await cursor.fetchone()

        memory_id = (row or {})["id"]
        logger.info(
            "memory.inserted",
            memory_id=memory_id,
            memory_type=candidate.memory_type.value,
            subject=candidate.subject.value,
        )
        return str(memory_id)

    async def reinforce(self, memory_id: str, *, confidence: float) -> None:
        """Record that a memory was independently restated.

        Increments the mention count and moves confidence toward the higher of
        the stored and new values. Confidence rises on repetition but is
        capped, so a fact repeated many times cannot exceed certainty.

        Args:
            memory_id: The memory to reinforce.
            confidence: Confidence of the restatement.
        """
        async with self.db.service_scope(reason="memory reinforcement") as conn:
            await conn.execute(
                """
                update public.memories
                   set mention_count = mention_count + 1,
                       confidence    = least(1.0, greatest(confidence, %s) + 0.05),
                       last_seen_at  = now()
                 where id = %s
                """,
                (confidence, memory_id),
            )
        logger.info("memory.reinforced", memory_id=memory_id)

    async def supersede(self, old_id: str, new_id: str) -> None:
        """Retire a memory in favour of a newer, contradictory one.

        The old row is kept and marked rather than deleted, so the admin
        inspector can show how a traveller's profile changed over time.

        Args:
            old_id: The memory being replaced.
            new_id: The memory replacing it.
        """
        async with self.db.service_scope(reason="memory contradiction resolution") as conn:
            await conn.execute(
                """
                update public.memories
                   set status = 'superseded', superseded_by = %s
                 where id = %s
                """,
                (new_id, old_id),
            )
        logger.info("memory.superseded", old_id=old_id, new_id=new_id)

    async def list_for_user(
        self, user_id: str, *, include_inactive: bool = False, limit: int = 200
    ) -> list[StoredMemory]:
        """List a user's memories, newest first.

        Args:
            user_id: Whose memories to list.
            include_inactive: Whether to include superseded and deleted rows.
            limit: Maximum rows.

        Returns:
            The user's memories.
        """
        # Chosen from two hardcoded literals; `include_inactive` is a bool and
        # never reaches the query text.
        status_clause = "" if include_inactive else "and status = 'active'"

        async with self.db.user_scope(user_id) as conn:
            # S608: interpolates only the module-level column constant and the
            # literal clause above. Values are bound parameters.
            cursor = await conn.execute(
                f"""
                select {_MEMORY_COLUMNS}
                  from public.memories
                 where user_id = %s {status_clause}
                 order by last_seen_at desc
                 limit %s
                """,  # noqa: S608
                (user_id, limit),
            )
            rows = await cursor.fetchall()

        return [_row_to_memory(row) for row in rows]

    async def delete(self, memory_id: str, user_id: str) -> bool:
        """Soft-delete a memory.

        Marked rather than removed so that a user correcting the system leaves
        a trace the admin inspector can show - useful when diagnosing an
        extractor that keeps re-learning something wrong.

        Args:
            memory_id: The memory to delete.
            user_id: Owner, enforced by RLS as well as by the predicate.

        Returns:
            True if a row was updated.
        """
        async with self.db.user_scope(user_id) as conn:
            cursor = await conn.execute(
                "update public.memories set status = 'deleted' where id = %s and user_id = %s",
                (memory_id, user_id),
            )
            return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# In-memory
# ---------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        Similarity in the range -1 to 1, or 0 if either vector is degenerate.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _salience(memory: StoredMemory) -> float:
    """Python mirror of the `memory_salience` SQL function.

    Kept deliberately in step with migration 0003 so tests written against the
    in-memory store describe the same ranking behaviour as production. Any
    change to one must be made to the other.

    Args:
        memory: The memory to score.

    Returns:
        A salience weight.
    """
    reinforcement = min(1.0, 0.45 + 0.55 * math.log(1 + memory.mention_count) / math.log(6))

    if memory.memory_type in (MemoryType.CONSTRAINT, MemoryType.IDENTITY):
        recency = 1.0
    elif memory.last_seen_at is None:
        recency = 1.0
    else:
        last_seen = memory.last_seen_at
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)
        age_seconds = (datetime.now(UTC) - last_seen).total_seconds()
        recency = max(0.5, math.exp(-age_seconds / (180 * 86400.0)))

    return memory.confidence * reinforcement * recency


class InMemoryMemoryStore:
    """Dictionary-backed memory storage for tests and database-free local runs.

    Mirrors `PostgresMemoryStore`'s behaviour closely enough that the same
    consolidation tests pass against both.
    """

    def __init__(self) -> None:
        """Initialise an empty store."""
        self._memories: dict[str, StoredMemory] = {}
        self._embeddings: dict[str, list[float]] = {}

    async def search(
        self,
        user_id: str,
        query_embedding: list[float],
        *,
        top_k: int = 8,
        min_similarity: float = 0.35,
        types: list[MemoryType] | None = None,
    ) -> list[RetrievedMemory]:
        """Find memories semantically similar to a query."""
        scored: list[RetrievedMemory] = []

        for memory in self._memories.values():
            if memory.user_id != user_id or memory.status != MemoryStatus.ACTIVE:
                continue
            if types and memory.memory_type not in types:
                continue

            similarity = _cosine(query_embedding, self._embeddings.get(memory.id, []))
            if similarity < min_similarity:
                continue

            salience = _salience(memory)
            scored.append(
                RetrievedMemory(
                    **memory.model_dump(),
                    similarity=similarity,
                    salience=salience,
                    score=similarity * salience,
                )
            )

        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:top_k]

    async def get_by_type(self, user_id: str, memory_type: MemoryType) -> list[StoredMemory]:
        """Fetch every active memory of one type."""
        matches = [
            memory
            for memory in self._memories.values()
            if memory.user_id == user_id
            and memory.status == MemoryStatus.ACTIVE
            and memory.memory_type == memory_type
        ]
        matches.sort(key=lambda m: (m.confidence, m.mention_count), reverse=True)
        return matches

    async def find_neighbours(
        self,
        user_id: str,
        embedding: list[float],
        *,
        subject: str | None = None,
        limit: int = 5,
    ) -> list[tuple[StoredMemory, float]]:
        """Find the closest existing memories to a candidate."""
        neighbours: list[tuple[StoredMemory, float]] = []

        for memory in self._memories.values():
            if memory.user_id != user_id or memory.status != MemoryStatus.ACTIVE:
                continue
            if subject is not None and memory.subject != subject:
                continue
            neighbours.append((memory, _cosine(embedding, self._embeddings.get(memory.id, []))))

        neighbours.sort(key=lambda item: item[1], reverse=True)
        return neighbours[:limit]

    async def insert(
        self,
        user_id: str,
        candidate: CandidateMemory,
        embedding: list[float],
        *,
        source_message_id: str | None = None,
        source_session_id: str | None = None,
        source_lang: str | None = None,
        extractor_model: str | None = None,
    ) -> str:
        """Store a new memory."""
        memory_id = str(uuid4())
        now = datetime.now(UTC)

        self._memories[memory_id] = StoredMemory(
            id=memory_id,
            user_id=user_id,
            memory_type=candidate.memory_type,
            subject=candidate.subject.value,
            content=candidate.content,
            confidence=candidate.confidence,
            mention_count=1,
            source_lang=source_lang,
            source_message_id=source_message_id,
            source_session_id=source_session_id,
            first_seen_at=now,
            last_seen_at=now,
        )
        self._embeddings[memory_id] = embedding
        return memory_id

    async def reinforce(self, memory_id: str, *, confidence: float) -> None:
        """Record that a memory was independently restated."""
        memory = self._memories.get(memory_id)
        if memory is None:
            return
        memory.mention_count += 1
        memory.confidence = min(1.0, max(memory.confidence, confidence) + 0.05)
        memory.last_seen_at = datetime.now(UTC)

    async def supersede(self, old_id: str, new_id: str) -> None:
        """Retire a memory in favour of a newer, contradictory one."""
        memory = self._memories.get(old_id)
        if memory is None:
            return
        memory.status = MemoryStatus.SUPERSEDED
        memory.superseded_by = new_id

    async def list_for_user(
        self, user_id: str, *, include_inactive: bool = False, limit: int = 200
    ) -> list[StoredMemory]:
        """List a user's memories, newest first."""
        matches = [
            memory
            for memory in self._memories.values()
            if memory.user_id == user_id
            and (include_inactive or memory.status == MemoryStatus.ACTIVE)
        ]
        matches.sort(key=lambda m: m.last_seen_at or datetime.min.replace(tzinfo=UTC),
                     reverse=True)
        return matches[:limit]

    async def delete(self, memory_id: str, user_id: str) -> bool:
        """Soft-delete a memory."""
        memory = self._memories.get(memory_id)
        if memory is None or memory.user_id != user_id:
            return False
        memory.status = MemoryStatus.DELETED
        return True
