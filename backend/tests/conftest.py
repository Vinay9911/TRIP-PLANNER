"""Shared test fixtures.

The guiding constraint: **the unit suite must run with no network, no
database, and no API keys.** A test suite that needs live credentials is one
that stops being run, and the logic most worth testing here - memory
consolidation, tool degradation, plan validation - has nothing to do with
whether Groq is reachable.

So the two external dependencies are faked at their narrowest interfaces:

* `FakeEmbeddingService` produces deterministic vectors from text, with the
  property that similar text yields similar vectors. That is enough to
  exercise the deduplicate / arbitrate / insert bands for real, rather than
  mocking out the very comparison the code exists to make.
* `InMemoryMemoryStore` (from the application) stands in for Postgres and
  mirrors the SQL ranking formula.

Tests that genuinely need a live service are marked `integration` and are
deselected by default.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator

import pytest

from app.core.config import Settings
from app.memory.store import InMemoryMemoryStore
from app.tools.base import start_recording, stop_recording
from app.tools.context import ToolContext, reset_tool_context, set_tool_context


@pytest.fixture
def settings() -> Settings:
    """Settings with test-friendly values and no real credentials.

    `_env_file=None` stops Pydantic reading a developer's real `.env`, which
    would otherwise make test results depend on whose machine they run on.
    """
    return Settings(
        _env_file=None,
        environment="development",
        log_level="WARNING",
        groq_api_key="test-groq-key",
        gemini_api_key="test-gemini-key",
        tavily_api_key="test-tavily-key",
        geoapify_api_key="test-geoapify-key",
        embedding_dimensions=128,
        memory_dedupe_threshold=0.90,
        memory_conflict_threshold=0.75,
        memory_min_confidence=0.6,
    )


class FakeEmbeddingService:
    """Deterministic embeddings that preserve lexical similarity.

    Real embeddings cannot be used offline, but a purely random fake would
    make every similarity ~0 and the consolidation tests vacuous - they would
    pass whatever the thresholds were.

    This builds a bag-of-words vector: each token is hashed to a set of
    dimensions and accumulated, then the vector is normalised. Two sentences
    sharing most of their words land close together; unrelated sentences land
    far apart. That is the only property the consolidation logic depends on.
    """

    def __init__(self, dimensions: int = 64) -> None:
        self.dimensions = dimensions
        self.call_count = 0

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = [token for token in text.lower().split() if len(token) > 2]

        for token in tokens:
            # Strip common suffixes so 'vegetarian' and 'vegetarians' collide.
            stem = token.rstrip(".,!?;:'\"").removesuffix("s")
            digest = hashlib.sha256(stem.encode()).digest()
            for offset in range(3):
                index = digest[offset] % self.dimensions
                vector[index] += 1.0

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return [1.0 / math.sqrt(self.dimensions)] * self.dimensions
        return [value / norm for value in vector]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.call_count += 1
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.call_count += 1
        return self._vector(text)


@pytest.fixture
def embeddings() -> FakeEmbeddingService:
    """A deterministic offline embedding service."""
    return FakeEmbeddingService(dimensions=128)


@pytest.fixture
def memory_store() -> InMemoryMemoryStore:
    """An empty in-process memory store."""
    return InMemoryMemoryStore()


@pytest.fixture
def user_id() -> str:
    """A stable test user id."""
    return "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def tool_context(user_id: str) -> Iterator[ToolContext]:
    """Bind a tool context for the duration of a test."""
    context = ToolContext(user_id=user_id, session_id="22222222-2222-4222-8222-222222222222")
    token = set_tool_context(context)
    try:
        yield context
    finally:
        reset_tool_context(token)


@pytest.fixture
def tool_records() -> Iterator[list]:
    """Capture tool-call records emitted during a test."""
    records = start_recording()
    try:
        yield records
    finally:
        stop_recording()
