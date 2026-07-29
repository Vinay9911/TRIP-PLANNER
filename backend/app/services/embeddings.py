"""Text embedding.

Embeddings are the mechanism behind both halves of the retrieval story in this
project: long-term memory search and RAG over travel articles. Both call this
module; nothing else talks to the embedding provider.

Three implementation details here are load-bearing rather than incidental.

**Asymmetric task types.** Gemini embeddings accept a `task_type` hint, and
using the same one for both sides of a search measurably hurts recall.
Documents are embedded as ``RETRIEVAL_DOCUMENT`` and queries as
``RETRIEVAL_QUERY``, so the model places a question near the passages that
*answer* it rather than near other questions. A stored memory is a statement
being searched for, so it is a document; the profile query built at session
start is a query.

**Manual normalisation.** `gemini-embedding-001` returns unit-length vectors
only at its native 3072 dimensions. Truncating to 768 via the model's
Matryoshka property yields vectors that are *not* normalised, and Google's
guidance is to normalise them yourself. Cosine distance happens to be
scale-invariant, so skipping this would not corrupt today's queries - but it
would quietly break the moment anyone switched an index to inner-product
distance, which is a trap worth closing at the source.

**768 dimensions.** Google's own figures put 768 at roughly a quarter of the
storage of 3072 for a 0.26% quality loss. On a 500 MB free-tier database with
an HNSW index, that trade is not close.

Like the LLM service, `GEMINI_API_KEY` accepts a comma-separated list and
calls rotate across the pool, retrying immediately on a different key when
one reports a quota failure.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections import OrderedDict
from enum import StrEnum
from typing import Any, Final

from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError, RateLimitError
from app.core.logging import get_logger
from app.services.keys import AllKeysExhausted, KeyPool, get_pool

logger = get_logger(__name__)

# The provider rejects oversized batches. Chosen conservatively: a smaller
# batch that always succeeds beats a larger one that intermittently 400s and
# forces a retry of the whole group.
MAX_BATCH_SIZE: Final[int] = 32

# Guards against a pathological input (an enormous scraped article section)
# blowing the per-request token limit. Truncation is preferable to failure -
# the leading portion of a section carries most of its retrievable meaning.
MAX_CHARS_PER_INPUT: Final[int] = 8000


class EmbeddingTask(StrEnum):
    """Which side of a similarity search the text belongs to.

    Values match the provider's `task_type` vocabulary.
    """

    DOCUMENT = "RETRIEVAL_DOCUMENT"
    QUERY = "RETRIEVAL_QUERY"
    SIMILARITY = "SEMANTIC_SIMILARITY"


def _normalise(vector: list[float]) -> list[float]:
    """Scale a vector to unit length.

    Args:
        vector: Raw embedding values.

    Returns:
        The vector scaled to L2 norm 1, or unchanged if it is all zeros
        (which would otherwise divide by zero).
    """
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


class EmbeddingService:
    """Embeds text via the Gemini API, with batching, retries and a cache.

    Attributes:
        settings: Application settings supplying the model and credential.
        dimensions: Output dimensionality, matching the `vector(n)` columns.
    """

    def __init__(self, settings: Settings | None = None, *, cache_size: int = 512) -> None:
        """Initialise the service.

        Args:
            settings: Settings override, for tests.
            cache_size: How many recent embeddings to retain in process.
        """
        self.settings = settings or get_settings()
        self.dimensions = self.settings.embedding_dimensions
        self._clients: dict[str, Any] = {}
        # Small LRU keyed by (task, text hash). Retrieval queries repeat far
        # more than you would guess - "vegetarian restaurants" recurs across
        # sessions and users - and each cache hit is one request kept off a
        # rate-limited free tier.
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._cache_size = cache_size
        self._lock = asyncio.Lock()

    # -- Client -----------------------------------------------------------

    def _get_pool(self) -> KeyPool:
        """Return the shared Gemini key pool.

        Raises:
            ConfigurationError: If no key is configured.
        """
        return get_pool("gemini", self.settings.gemini_api_key.get_secret_value())

    def _client_for(self, api_key: str) -> Any:
        """Return the client bound to one key, creating it on first use.

        Cached per key so that rotating does not rebuild an HTTP client and
        redo TLS on every call.

        Args:
            api_key: The key to bind.

        Returns:
            A Gemini client.
        """
        client = self._clients.get(api_key)
        if client is None:
            from google import genai

            client = genai.Client(api_key=api_key)
            self._clients[api_key] = client
        return client

    # -- Cache ------------------------------------------------------------

    @staticmethod
    def _cache_key(text: str, task: EmbeddingTask) -> str:
        """Build a cache key from the text and its task type.

        The task is part of the key because the same string embedded as a
        query and as a document produces different vectors.
        """
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"{task.value}:{digest}"

    def _cache_get(self, key: str) -> list[float] | None:
        """Look up a cached embedding, refreshing its recency."""
        vector = self._cache.get(key)
        if vector is not None:
            self._cache.move_to_end(key)
        return vector

    def _cache_put(self, key: str, vector: list[float]) -> None:
        """Store an embedding, evicting the least recently used if full."""
        self._cache[key] = vector
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    # -- Embedding --------------------------------------------------------

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed text that will be *stored* and later searched against.

        Args:
            texts: Passages to embed. Empty list returns an empty list.

        Returns:
            One unit-length vector per input, in the same order.

        Raises:
            ExternalServiceError: If the provider fails after all retries.
        """
        return await self._embed(texts, EmbeddingTask.DOCUMENT)

    async def embed_query(self, text: str) -> list[float]:
        """Embed text that is being used to *search*.

        Args:
            text: The query.

        Returns:
            A unit-length vector.

        Raises:
            ExternalServiceError: If the provider fails after all retries.
        """
        vectors = await self._embed([text], EmbeddingTask.QUERY)
        return vectors[0]

    async def _embed(self, texts: list[str], task: EmbeddingTask) -> list[list[float]]:
        """Embed a list of texts, serving what it can from cache.

        Args:
            texts: Inputs to embed.
            task: Which side of the search these belong to.

        Returns:
            One vector per input, in the original order.

        Raises:
            ExternalServiceError: If the provider fails after all retries.
        """
        if not texts:
            return []

        prepared = [text[:MAX_CHARS_PER_INPUT] for text in texts]
        results: list[list[float] | None] = [None] * len(prepared)
        pending: list[tuple[int, str]] = []

        async with self._lock:
            for index, text in enumerate(prepared):
                cached = self._cache_get(self._cache_key(text, task))
                if cached is not None:
                    results[index] = cached
                else:
                    pending.append((index, text))

        if pending:
            logger.debug(
                "embeddings.request",
                task=task.value,
                total=len(prepared),
                cache_hits=len(prepared) - len(pending),
            )

            for start in range(0, len(pending), MAX_BATCH_SIZE):
                batch = pending[start : start + MAX_BATCH_SIZE]
                vectors = await self._call_provider([text for _, text in batch], task)

                async with self._lock:
                    for (index, text), vector in zip(batch, vectors, strict=True):
                        results[index] = vector
                        self._cache_put(self._cache_key(text, task), vector)

        # Every slot is filled by construction. Checked explicitly rather than
        # with `assert`, which `python -O` strips: callers pair these vectors
        # with their inputs positionally, so a silently shortened list would
        # attach each embedding to the wrong text.
        if any(vector is None for vector in results):
            raise ExternalServiceError(
                "Embedding provider did not return a vector for every input.",
                service="gemini-embeddings",
                retryable=False,
                details={"requested": len(prepared)},
            )
        return [vector for vector in results if vector is not None]

    async def _call_provider(self, texts: list[str], task: EmbeddingTask) -> list[list[float]]:
        """Send one batch to the provider, rotating keys on quota failures.

        Mirrors the LLM service's policy, and for the same reason: a quota
        belongs to the key, not to the service. A rate-limited key is rested
        and the next one is tried immediately; only a non-quota failure earns a
        backoff.

        Args:
            texts: A batch no larger than `MAX_BATCH_SIZE`.
            task: Task type hint.

        Returns:
            Normalised vectors, one per input.

        Raises:
            AllKeysExhausted: If every key is cooling down.
            ExternalServiceError: If every attempt fails.
        """
        from google.genai import types

        pool = self._get_pool()
        config = types.EmbedContentConfig(
            task_type=task.value,
            output_dimensionality=self.dimensions,
        )
        attempts = pool.size + 2
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                api_key = pool.acquire()
            except AllKeysExhausted as exhausted:
                last_error = exhausted
                if attempt < attempts and exhausted.retry_after_seconds <= 20:
                    await asyncio.sleep(exhausted.retry_after_seconds + 0.5)
                    continue
                raise

            client = self._client_for(api_key)

            try:
                response = await client.aio.models.embed_content(
                    model=self.settings.embedding_model,
                    contents=list(texts),
                    config=config,
                )
            except Exception as exc:  # noqa: BLE001
                # Broad by necessity - the SDK raises its own hierarchy. The
                # message is inspected to tell a quota failure (rotate) from a
                # transport failure (back off).
                message = str(exc).lower()
                is_quota = any(
                    marker in message
                    for marker in ("429", "quota", "rate limit", "resource_exhausted")
                )

                if is_quota:
                    pool.report_rate_limited(api_key, None)
                    last_error = RateLimitError(
                        "The embedding provider rejected the request for exceeding a quota.",
                        service="gemini-embeddings",
                        details={"original": str(exc)[:300]},
                    )
                    logger.info(
                        "embeddings.key_rotated",
                        attempt=attempt,
                        keys_available=pool.available_count(),
                    )
                    continue

                pool.report_error(api_key)
                last_error = ExternalServiceError(
                    "The embedding provider could not complete the request.",
                    service="gemini-embeddings",
                    details={"original": str(exc)[:300]},
                )
                logger.warning("embeddings.attempt_failed", attempt=attempt, error=str(exc)[:200])
                if attempt >= attempts:
                    break
                await asyncio.sleep(min(2.0 ** (attempt - 1), 8.0))
                continue

            embeddings = getattr(response, "embeddings", None) or []
            if len(embeddings) != len(texts):
                raise ExternalServiceError(
                    "The embedding provider returned a different number of vectors "
                    f"than inputs ({len(embeddings)} for {len(texts)}).",
                    service="gemini-embeddings",
                    retryable=False,
                )

            pool.report_success(api_key)

            # Normalise here rather than at the call sites: see the module
            # docstring. Doing it once at the boundary means every vector in
            # the database is unit length by construction.
            return [_normalise(list(item.values or [])) for item in embeddings]

        raise last_error or ExternalServiceError(
            "Embedding retry loop exited without a result.", service="gemini-embeddings"
        )

    async def healthcheck(self) -> dict[str, object]:
        """Report whether the embedding provider is reachable and correct.

        Verifies the returned dimensionality too, because a provider that
        silently ignores `output_dimensionality` would produce vectors the
        database rejects on insert - a failure far better caught at startup
        than mid-run.

        Returns:
            A dict with an `ok` flag and diagnostic detail. Never raises.
        """
        try:
            vector = await self.embed_query("healthcheck")
        except Exception as exc:  # noqa: BLE001 - health checks never raise
            return {"ok": False, "reason": str(exc)[:200]}

        matches = len(vector) == self.dimensions
        return {
            "ok": matches,
            "dimensions": len(vector),
            "expected_dimensions": self.dimensions,
            "model": self.settings.embedding_model,
            "keys": self._get_pool().status(),
        }
