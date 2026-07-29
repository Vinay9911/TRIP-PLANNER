"""Embedding and caching the retrieval corpus.

Articles are fetched on demand, chunked, embedded once, and cached with a TTL.
The index therefore warms around whatever cities people actually ask about,
rather than requiring a bulk ingest that a 500 MB free-tier database could not
hold anyway.

The cache is doing more work than it appears. Embedding one district article
costs 6-10 provider calls, and planning two days in Tokyo touches three or
four districts. Without caching, the second person to ask about Tokyo pays the
same cost as the first, and the free embedding quota is gone within a day of
demo traffic. With it, only the first request for a given article pays.

As with memory storage, there are two implementations behind one Protocol so
the retrieval logic can be tested offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.session import Database
from app.memory.store import _cosine
from app.rag.chunking import Chunk, chunk_article
from app.rag.corpus import Article
from app.services.embeddings import EmbeddingService

logger = get_logger(__name__)


@dataclass
class RetrievedChunk:
    """A chunk returned from a similarity search.

    Attributes:
        document_title: Source article title.
        section: Source section heading.
        content: The passage text.
        url: Source URL for citation.
        similarity: Cosine similarity to the query, 0-1.
    """

    document_title: str
    section: str
    content: str
    url: str
    similarity: float

    @property
    def citation(self) -> str:
        """A short human-readable source label."""
        return f"{self.document_title} - {self.section}"


class RagIndex(Protocol):
    """Storage and search interface for the retrieval corpus."""

    async def is_indexed(self, title: str) -> bool:
        """Whether an unexpired copy of this article is already indexed."""
        ...

    async def index_article(
        self, article: Article, chunks: list[Chunk], embeddings: list[list[float]]
    ) -> str:
        """Store an article and its embedded chunks, replacing any prior copy."""
        ...

    async def search(
        self,
        query_embedding: list[float],
        *,
        document_titles: list[str] | None = None,
        sections: list[str] | None = None,
        top_k: int = 6,
        min_similarity: float = 0.30,
    ) -> list[RetrievedChunk]:
        """Find chunks similar to a query, optionally narrowed by document and section."""
        ...


class PostgresRagIndex:
    """pgvector-backed corpus index.

    Attributes:
        db: Connection pool wrapper.
        settings: Application settings supplying the cache TTL.
    """

    def __init__(self, db: Database, settings: Settings | None = None) -> None:
        """Initialise the index.

        Args:
            db: Connection pool wrapper.
            settings: Settings override, for tests.
        """
        self.db = db
        self.settings = settings or get_settings()

    async def is_indexed(self, title: str) -> bool:
        """Whether an unexpired copy of this article is already indexed."""
        async with self.db.service_scope(reason="rag cache lookup") as conn:
            cursor = await conn.execute(
                """
                select 1
                  from public.documents
                 where source = 'wikivoyage' and source_id = %s and expires_at > now()
                 limit 1
                """,
                (title,),
            )
            return await cursor.fetchone() is not None

    async def index_article(
        self, article: Article, chunks: list[Chunk], embeddings: list[list[float]]
    ) -> str:
        """Store an article and its embedded chunks.

        Replaces any existing copy so a refresh after TTL expiry does not
        leave stale chunks behind alongside new ones.

        Args:
            article: The source article.
            chunks: Its chunks.
            embeddings: One embedding per chunk, in the same order.

        Returns:
            The document's id.
        """
        expires_at = datetime.now(UTC) + timedelta(days=self.settings.rag_cache_ttl_days)

        async with self.db.service_scope(reason="rag cache write") as conn:
            cursor = await conn.execute(
                """
                insert into public.documents
                    (source, source_id, title, url, lang, districts, fetched_at, expires_at)
                values ('wikivoyage', %s, %s, %s, 'en', %s, now(), %s)
                on conflict (source, source_id, lang) do update
                    set title = excluded.title,
                        url = excluded.url,
                        districts = excluded.districts,
                        fetched_at = now(),
                        expires_at = excluded.expires_at
                returning id::text
                """,
                (article.title, article.title, article.url, article.districts, expires_at),
            )
            row = await cursor.fetchone()
            document_id = (row or {})["id"]

            # Clear prior chunks before inserting: an updated article may have
            # fewer sections than before, and orphaned chunks would keep being
            # retrieved as if current.
            await conn.execute(
                "delete from public.document_chunks where document_id = %s", (document_id,)
            )

            for chunk, embedding in zip(chunks, embeddings, strict=True):
                await conn.execute(
                    """
                    insert into public.document_chunks
                        (document_id, chunk_index, section, content, token_count, embedding)
                    values (%s, %s, %s, %s, %s, %s::vector)
                    """,
                    (
                        document_id,
                        chunk.chunk_index,
                        chunk.section,
                        chunk.content,
                        len(chunk.content) // 4,
                        embedding,
                    ),
                )

        logger.info("rag.indexed", title=article.title, chunks=len(chunks))
        return str(document_id)

    async def search(
        self,
        query_embedding: list[float],
        *,
        document_titles: list[str] | None = None,
        sections: list[str] | None = None,
        top_k: int = 6,
        min_similarity: float = 0.30,
    ) -> list[RetrievedChunk]:
        """Find chunks similar to a query."""
        document_ids: list[str] | None = None

        if document_titles:
            async with self.db.service_scope(reason="rag document id lookup") as conn:
                cursor = await conn.execute(
                    """
                    select id::text from public.documents
                     where source = 'wikivoyage' and source_id = any(%s::text[])
                    """,
                    (document_titles,),
                )
                document_ids = [row["id"] for row in await cursor.fetchall()]

            # An explicit document filter that matches nothing must return
            # nothing, not silently widen to the whole corpus - that would turn
            # a precise hop-3 query into an unscoped one.
            if not document_ids:
                return []

        async with self.db.service_scope(reason="rag chunk search") as conn:
            cursor = await conn.execute(
                """
                select document_title, source_id, url, section, content, similarity
                  from public.match_document_chunks(
                           %s::vector, %s::uuid[], %s::text[], %s::int, %s::real
                       )
                """,
                (query_embedding, document_ids, sections, top_k, min_similarity),
            )
            rows = await cursor.fetchall()

        return [
            RetrievedChunk(
                document_title=row["source_id"],
                section=row["section"] or "",
                content=row["content"],
                url=row["url"] or "",
                similarity=float(row["similarity"]),
            )
            for row in rows
        ]


class InMemoryRagIndex:
    """Dictionary-backed corpus index for tests and database-free local runs."""

    def __init__(self) -> None:
        """Initialise an empty index."""
        self._documents: dict[str, Article] = {}
        self._chunks: list[tuple[Chunk, list[float]]] = []

    async def is_indexed(self, title: str) -> bool:
        """Whether this article is already indexed."""
        return title in self._documents

    async def index_article(
        self, article: Article, chunks: list[Chunk], embeddings: list[list[float]]
    ) -> str:
        """Store an article and its embedded chunks."""
        self._chunks = [entry for entry in self._chunks if entry[0].document_title != article.title]
        self._documents[article.title] = article
        self._chunks.extend(zip(chunks, embeddings, strict=True))
        return str(uuid4())

    async def search(
        self,
        query_embedding: list[float],
        *,
        document_titles: list[str] | None = None,
        sections: list[str] | None = None,
        top_k: int = 6,
        min_similarity: float = 0.30,
    ) -> list[RetrievedChunk]:
        """Find chunks similar to a query."""
        title_filter = set(document_titles) if document_titles else None
        section_filter = set(sections) if sections else None

        scored: list[RetrievedChunk] = []
        for chunk, embedding in self._chunks:
            if title_filter is not None and chunk.document_title not in title_filter:
                continue
            if section_filter is not None and chunk.section not in section_filter:
                continue

            similarity = _cosine(query_embedding, embedding)
            if similarity < min_similarity:
                continue

            scored.append(
                RetrievedChunk(
                    document_title=chunk.document_title,
                    section=chunk.section,
                    content=chunk.content,
                    url=chunk.url,
                    similarity=similarity,
                )
            )

        scored.sort(key=lambda item: item.similarity, reverse=True)
        return scored[:top_k]


async def ensure_indexed(
    article: Article,
    index: RagIndex,
    embeddings: EmbeddingService,
    *,
    settings: Settings | None = None,
) -> int:
    """Index an article if it is not already cached.

    Args:
        article: The article to index.
        index: Where to store it.
        embeddings: Embedding provider.
        settings: Settings override, for tests.

    Returns:
        The number of chunks indexed, or 0 if it was already cached or empty.
    """
    cfg = settings or get_settings()

    if await index.is_indexed(article.title):
        logger.debug("rag.cache_hit", title=article.title)
        return 0

    chunks = chunk_article(article, max_chars=cfg.rag_chunk_tokens * 4)
    if not chunks:
        logger.info("rag.no_chunks", title=article.title)
        return 0

    vectors = await embeddings.embed_documents([chunk.content for chunk in chunks])
    await index.index_article(article, chunks, vectors)
    return len(chunks)
