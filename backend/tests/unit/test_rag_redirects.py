"""Regression test for the bug that quietly disabled multi-hop retrieval.

Wikivoyage redirects heavily. `list_districts("Mumbai")` offers titles like
`Mumbai/Colaba and Fort`, but fetching that article returns one whose real
title is `Mumbai/South` - and three different offered titles can collapse
onto the same real article. Verified against the live API:

    requested='Mumbai/Colaba and Fort'   -> article.title='Mumbai/South'
    requested='Mumbai/Central Suburbs'   -> article.title='Mumbai/Eastern Suburbs'
    requested='Mumbai/Elephanta Island'  -> article.title='Mumbai/Elephanta'

Hop 2 indexed the *fetched* article (so under its own title) but recorded and
then searched the *requested* title. The search therefore filtered on a title
that had never been indexed and returned nothing - and because hops 3 and 4
filter on the same recorded list, they returned nothing either. The headline
"multi-hop RAG" feature silently degraded to a single hop for any destination
with redirects, while still paying for every hop's embedding round trip.

Measured before and after on the live corpus: Mumbai went from 4 retrieved
chunks (hops 2 and 4 both empty) to 13.

These tests use a fake client so the behaviour is pinned without a network
call, and assert on the thing that actually broke: that the titles hop 2
records are the ones that were indexed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from app.rag.retriever import MultiHopRetriever, RetrievalResult


@dataclass
class _FakeArticle:
    """Minimal stand-in for `corpus.Article`."""

    title: str
    sections: list[tuple[str, str]] = field(default_factory=list)
    districts: list[str] = field(default_factory=list)
    url: str = "https://en.wikivoyage.org/wiki/Test"


class _RedirectingClient:
    """Mimics Wikivoyage's redirects: several titles collapse onto one article."""

    #: requested title -> the title the fetched article actually has
    REDIRECTS: ClassVar[dict[str, str]] = {
        "Mumbai/Colaba and Fort": "Mumbai/South",
        "Mumbai/Fort, Colaba and Churchgate": "Mumbai/South",
        "Mumbai/Central Suburbs": "Mumbai/Eastern Suburbs",
    }

    def __init__(self) -> None:
        self.fetched: list[str] = []

    async def fetch_article(self, title: str) -> _FakeArticle:
        self.fetched.append(title)
        return _FakeArticle(title=self.REDIRECTS.get(title, title))


class _RecordingIndex:
    """Records what was indexed and which titles searches filter on."""

    def __init__(self) -> None:
        self.indexed_titles: list[str] = []
        self.searched_titles: list[list[str] | None] = []

    async def is_indexed(self, title: str) -> bool:
        return False

    async def index_article(self, article, chunks, vectors) -> None:
        self.indexed_titles.append(article.title)

    async def search(
        self,
        embedding,
        *,
        document_titles=None,
        sections=None,
        top_k=6,
        min_similarity=0.30,
    ):
        self.searched_titles.append(document_titles)
        return []


class _StubEmbeddings:
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]

    async def embed_query(self, text: str) -> list[float]:
        return [0.0] * 8


async def test_hop_two_searches_the_titles_it_actually_indexed(settings, monkeypatch):
    """The exact bug: recorded titles must match indexed titles, post-redirect."""
    from app.rag import retriever as retriever_module

    # `chunk_article` needs real sections to produce chunks; stub the whole
    # indexing helper so the test stays focused on title bookkeeping.
    async def fake_ensure_indexed(article, index, embeddings, *, settings=None):
        await index.index_article(article, [], [])
        return 1

    monkeypatch.setattr(retriever_module, "ensure_indexed", fake_ensure_indexed)

    client = _RedirectingClient()
    index = _RecordingIndex()
    agent = MultiHopRetriever(client, index, _StubEmbeddings(), settings)

    # Skip the model call that chooses districts; return titles that redirect.
    async def fake_select(query, city_title, available, constraints):
        return ["Mumbai/Colaba and Fort", "Mumbai/Central Suburbs"]

    monkeypatch.setattr(agent, "_select_districts", fake_select)

    result = RetrievalResult(
        districts_considered=["Mumbai/Colaba and Fort", "Mumbai/Central Suburbs"]
    )
    await agent._hop_two("food and sights", "Mumbai", None, "general", result, {})

    assert index.indexed_titles, "nothing was indexed at all"
    searched = index.searched_titles[-1] or []

    assert set(searched) == set(index.indexed_titles), (
        f"hop 2 searched {searched} but indexed {index.indexed_titles} - a search "
        "filtered on a never-indexed title returns zero chunks, which is what "
        "silently reduced multi-hop retrieval to a single hop"
    )
    assert "Mumbai/South" in searched, "the redirected-to title should be the one searched"
    assert "Mumbai/Colaba and Fort" not in searched, (
        "the pre-redirect title was never indexed and must not be searched"
    )


async def test_titles_that_redirect_together_are_deduplicated(settings, monkeypatch):
    """Two offered titles collapsing onto one article must index it once."""
    from app.rag import retriever as retriever_module

    async def fake_ensure_indexed(article, index, embeddings, *, settings=None):
        await index.index_article(article, [], [])
        return 1

    monkeypatch.setattr(retriever_module, "ensure_indexed", fake_ensure_indexed)

    client = _RedirectingClient()
    index = _RecordingIndex()
    agent = MultiHopRetriever(client, index, _StubEmbeddings(), settings)

    async def fake_select(query, city_title, available, constraints):
        # Both of these redirect to Mumbai/South.
        return ["Mumbai/Colaba and Fort", "Mumbai/Fort, Colaba and Churchgate"]

    monkeypatch.setattr(agent, "_select_districts", fake_select)

    result = RetrievalResult(districts_considered=["a", "b"])
    await agent._hop_two("food", "Mumbai", None, "general", result, {})

    assert index.indexed_titles == ["Mumbai/South"], (
        f"expected one indexing pass, got {index.indexed_titles}"
    )


async def test_later_hops_inherit_the_canonical_titles(settings, monkeypatch):
    """Hops 3 and 4 filter on `districts_selected`, so it must hold real titles."""
    from app.rag import retriever as retriever_module

    async def fake_ensure_indexed(article, index, embeddings, *, settings=None):
        await index.index_article(article, [], [])
        return 1

    monkeypatch.setattr(retriever_module, "ensure_indexed", fake_ensure_indexed)

    agent = MultiHopRetriever(_RedirectingClient(), _RecordingIndex(), _StubEmbeddings(), settings)

    async def fake_select(query, city_title, available, constraints):
        return ["Mumbai/Colaba and Fort"]

    monkeypatch.setattr(agent, "_select_districts", fake_select)

    result = RetrievalResult(districts_considered=["Mumbai/Colaba and Fort"])
    await agent._hop_two("food", "Mumbai", None, "general", result, {})

    assert result.districts_selected == ["Mumbai/South"], (
        "districts_selected carries into hops 3 and 4; holding a pre-redirect "
        "title there makes both of those hops search nothing"
    )
