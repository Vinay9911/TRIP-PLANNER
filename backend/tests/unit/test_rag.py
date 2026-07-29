"""Tests for retrieval.

The central claim to verify is that retrieval genuinely *chains* - that hop 2
searches documents which were unknowable before hop 1 ran. Three searches in a
row is not multi-hop if all three queries were derivable from the original
question, so `test_hop_two_searches_documents_discovered_by_hop_one` is the
test that distinguishes this implementation from a staged one.

Everything runs against fakes: no network, no API keys.
"""

from __future__ import annotations

import pytest

from app.rag.chunking import chunk_article
from app.rag.corpus import Article, _deduplicate_districts, parse_sections
from app.rag.index import InMemoryRagIndex, ensure_indexed
from app.rag.retriever import MultiHopRetriever

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_sections_are_split_on_wiki_headings():
    extract = (
        "Kyoto was the imperial capital of Japan for over a thousand years.\n"
        "== Understand ==\nKyoto has more than a thousand temples and shrines.\n"
        "== See ==\nKiyomizu Temple is famous and sits above the city on wooden pillars.\n"
        "== Eat ==\nShojin ryori is the local Buddhist vegetarian cuisine served here.\n"
    )

    sections = dict(parse_sections(extract))

    assert "Understand" in sections
    assert "Kiyomizu" in sections["See"]
    assert "Shojin" in sections["Eat"]


def test_subsections_are_folded_into_their_parent():
    """'By train' alone is a fragment; it only means something under 'Get in'."""
    extract = (
        "== Get in ==\nSeveral options exist for reaching the city centre.\n"
        "=== By train ===\nThe Shinkansen stops at Kyoto Station every half hour.\n"
        "=== By bus ===\nHighway buses run overnight from Tokyo and are cheaper.\n"
        "== See ==\nThere are many temples worth visiting across the eastern hills.\n"
    )

    sections = dict(parse_sections(extract))

    assert "By train" not in sections
    assert "Shinkansen" in sections["Get in"]
    assert "Highway buses" in sections["Get in"]


def test_navigation_sections_are_dropped():
    extract = (
        "== See ==\nTemples and shrines are scattered throughout the eastern districts.\n"
        "== Go next ==\nOsaka is thirty minutes away by train from Kyoto Station.\n"
        "== External links ==\nOfficial tourism website for the city of Kyoto.\n"
    )

    sections = dict(parse_sections(extract))

    assert "See" in sections
    assert "Go next" not in sections
    assert "External links" not in sections


def test_redirect_variants_of_a_district_are_collapsed():
    """Wikivoyage keeps redirects as real pages, so allpages returns both."""
    titles = [
        "Paris/10th",
        "Paris/10th arrondissement",
        "Paris/11th",
        "Paris/11th arrondissement",
        "Paris/Montmartre",
    ]

    unique = _deduplicate_districts(titles)

    assert len(unique) == 3
    assert "Paris/10th arrondissement" in unique
    assert "Paris/10th" not in unique
    assert "Paris/Montmartre" in unique


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def test_chunks_carry_their_section_label():
    article = Article(
        title="Kyoto/Higashiyama",
        url="https://en.wikivoyage.org/wiki/Kyoto/Higashiyama",
        sections=[
            ("See", "Kiyomizu Temple sits above the city. " * 8),
            ("Eat", "Shojin ryori is Buddhist vegetarian cuisine. " * 8),
        ],
    )

    chunks = chunk_article(article, max_chars=2000)

    assert {chunk.section for chunk in chunks} == {"See", "Eat"}
    # Provenance is prefixed into the text so the section is embedded too -
    # Eat and Sleep sections of one district otherwise embed very close
    # together, sharing place names and tone.
    assert all(chunk.content.startswith("[Kyoto/Higashiyama - ") for chunk in chunks)


def test_oversized_sections_split_on_listing_boundaries():
    """A fixed window would cut an attraction entry in half."""
    listings = "".join(
        f"{index} Temple Number {index}, an old temple with a garden and a view. "
        f"{'Detail sentence. ' * 20}\n"
        for index in range(1, 12)
    )
    article = Article(title="Kyoto", url="", sections=[("See", listings)])

    chunks = chunk_article(article, max_chars=1200)

    assert len(chunks) > 1
    for chunk in chunks:
        body = chunk.content.split("\n", 1)[1]
        # Each piece should start at a listing boundary, not mid-entry.
        assert body.lstrip()[0].isdigit(), f"chunk starts mid-entry: {body[:60]!r}"


def test_tiny_sections_are_merged_rather_than_indexed_alone():
    article = Article(
        title="Kyoto",
        url="",
        sections=[("Drink", "A short note."), ("Drink", "Another short note.")],
    )

    chunks = chunk_article(article, max_chars=2000)

    assert len(chunks) == 1


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


async def test_indexing_is_skipped_when_already_cached(embeddings):
    index = InMemoryRagIndex()
    article = Article(
        title="Kyoto", url="", sections=[("See", "Temples abound in the eastern hills. " * 10)]
    )

    first = await ensure_indexed(article, index, embeddings)
    calls_after_first = embeddings.call_count
    second = await ensure_indexed(article, index, embeddings)

    assert first > 0
    assert second == 0, "a cached article must not be re-embedded"
    assert embeddings.call_count == calls_after_first


async def test_document_filter_that_matches_nothing_returns_nothing(embeddings):
    """Widening to the whole corpus would turn a precise hop into a blind one."""
    index = InMemoryRagIndex()
    article = Article(title="Kyoto", url="", sections=[("See", "Temples abound here. " * 10)])
    await ensure_indexed(article, index, embeddings)

    query = await embeddings.embed_query("temples")
    results = await index.search(query, document_titles=["Nonexistent/District"])

    assert results == []


# ---------------------------------------------------------------------------
# Multi-hop chaining
# ---------------------------------------------------------------------------


class StubCorpus:
    """A tiny Wikivoyage stand-in with a known district structure."""

    def __init__(self) -> None:
        """Track which article titles were fetched, to assert on chaining."""
        self.fetched: list[str] = []

    async def find_article_title(self, place: str) -> str | None:
        return "Kyoto" if "kyoto" in place.lower() else None

    async def list_districts(self, city_title: str) -> list[str]:
        return ["Kyoto/Higashiyama", "Kyoto/Central", "Kyoto/Arashiyama"]

    async def fetch_article(self, title: str) -> Article | None:
        self.fetched.append(title)
        return Article(
            title=title,
            url=f"https://en.wikivoyage.org/wiki/{title}",
            sections=[
                ("Understand", f"{title} is a district with a distinct character. " * 6),
                ("See", f"{title} has notable temples and gardens to visit. " * 6),
                ("Eat", f"{title} offers vegetarian shojin ryori restaurants. " * 6),
            ],
        )


@pytest.fixture
def stub_retriever(embeddings, settings, monkeypatch):
    """A retriever wired to stubs, with the two model calls faked."""
    from app.rag import retriever as retriever_module
    from app.rag.retriever import DistrictSelection, SufficiencyCheck

    async def fake_structured(role, messages, schema, *, purpose, settings=None, **kwargs):
        if schema is DistrictSelection:
            available = [
                line[2:].strip()
                for line in messages[-1].content.splitlines()
                if line.startswith("- ")
            ]
            return DistrictSelection(districts=available[:2], reasoning="Suits the request.")
        return SufficiencyCheck(is_sufficient=True, missing="")

    monkeypatch.setattr(retriever_module, "structured_call", fake_structured)

    corpus = StubCorpus()
    engine = MultiHopRetriever(corpus, InMemoryRagIndex(), embeddings, settings)
    return engine, corpus


async def test_hop_two_searches_documents_discovered_by_hop_one(stub_retriever):
    """The test that separates real multi-hop from three searches in a row.

    Hop 2's documents are district sub-articles whose names only became known
    when hop 1 queried the city's district list. If hop 2 could have been
    issued without hop 1, the chaining would be decorative.
    """
    engine, _ = stub_retriever

    result = await engine.retrieve(
        query="quiet temple districts", destination="Kyoto", constraints=None
    )

    hop_one = next(hop for hop in result.hops if hop.number == 1)
    hop_two = next(hop for hop in result.hops if hop.number == 2)

    assert hop_one.documents == ["Kyoto"]
    assert hop_two.documents, "hop 2 retrieved no documents"
    assert set(hop_two.documents).isdisjoint(hop_one.documents)
    assert all(title.startswith("Kyoto/") for title in hop_two.documents)
    assert "hop 1" in hop_two.derived_from


async def test_each_constraint_gets_its_own_targeted_search(stub_retriever):
    """A combined 'vegetarian AND step-free' embedding lands near neither."""
    engine, _ = stub_retriever

    result = await engine.retrieve(
        query="where to eat",
        destination="Kyoto",
        constraints=["vegetarian food", "wheelchair access"],
    )

    hop_three = next((hop for hop in result.hops if hop.number == 3), None)
    assert hop_three is not None
    assert "vegetarian food" in hop_three.query
    assert "wheelchair access" in hop_three.query
    assert "|" in hop_three.query, "constraints were merged into one search"


async def test_hop_budget_is_respected(stub_retriever, settings):
    engine, _ = stub_retriever
    settings.rag_max_hops = 2

    result = await engine.retrieve(
        query="temples", destination="Kyoto", constraints=["vegetarian"], max_hops=2
    )

    assert len(result.hops) <= 2


async def test_unknown_destination_stops_cleanly(stub_retriever):
    engine, _ = stub_retriever

    result = await engine.retrieve(query="anything", destination="Atlantis")

    assert result.is_empty
    assert result.stopped_because == "destination_not_found"


async def test_hallucinated_district_names_are_filtered_out(embeddings, settings, monkeypatch):
    """A model naming a district that does not exist must cost nothing."""
    from app.rag import retriever as retriever_module
    from app.rag.retriever import DistrictSelection, SufficiencyCheck

    async def hallucinating(role, messages, schema, *, purpose, settings=None, **kwargs):
        if schema is DistrictSelection:
            return DistrictSelection(
                districts=["Kyoto/Higashiyama", "Kyoto/Atlantis", "Kyoto/Invented"],
                reasoning="",
            )
        return SufficiencyCheck(is_sufficient=True, missing="")

    monkeypatch.setattr(retriever_module, "structured_call", hallucinating)

    corpus = StubCorpus()
    engine = MultiHopRetriever(corpus, InMemoryRagIndex(), embeddings, settings)

    result = await engine.retrieve(query="temples", destination="Kyoto")

    assert result.districts_selected == ["Kyoto/Higashiyama"]
    assert "Kyoto/Atlantis" not in corpus.fetched


async def test_retrieved_passages_carry_citations(stub_retriever):
    engine, _ = stub_retriever

    result = await engine.retrieve(query="temples", destination="Kyoto")

    assert not result.is_empty
    for chunk in result.chunks:
        assert chunk.citation
        assert " - " in chunk.citation

    block = result.as_context_block()
    assert "Source:" in block
    assert "Wikivoyage" in block
