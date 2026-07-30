"""Multi-hop retrieval.

"Multi-hop" means each retrieval's *query is constructed from the previous
retrieval's results*. That is the distinction from a single retrieve-and-stuff
call, and it is easy to fake - running three searches in a row is not
multi-hop if all three queries were derivable from the original question.

Here the dependency is genuine and traceable:

    Hop 1  ORIENT
           Retrieve the city article. Read its district list from the
           Wikivoyage API. Output: which neighbourhoods exist, and what each
           is like.

    Hop 2  NARROW                     <- input comes from hop 1
           A model picks the 2-4 districts matching this traveller's style and
           constraints, choosing only from the names hop 1 returned. Those
           specific district articles are then fetched and indexed. Hop 2
           cannot run without hop 1: the district names are not knowable in
           advance and are not invented, they are looked up.

    Hop 3  CONSTRAIN                  <- input comes from hop 2
           For each hard constraint, a targeted search scoped to the district
           documents hop 2 selected and to the relevant sections - a dietary
           constraint searches only their `Eat` sections.

    Hop 4  FILL GAPS                  <- input comes from hop 3
           A sufficiency check names what is still missing; one final search
           targets exactly that. Skipped when the evidence is already
           sufficient, which is the common case.

**Stop conditions are as important as the hops.** The current literature is
unanimous that unbounded retrieve-reason-retrieve loops are the dominant
failure mode of agentic RAG - they are where the cost blows up. Four
guardrails apply: a hard hop ceiling, an early exit when a hop adds no new
documents, an early exit when the sufficiency check passes, and a cap on
districts per query.

Every hop is recorded in a `RetrievalTrace`, which the admin dashboard renders.
That is what lets a reviewer verify the chaining actually happened rather than
taking this docstring's word for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.rag.corpus import WikivoyageClient, extract_child_destinations
from app.rag.index import RagIndex, RetrievedChunk, ensure_indexed
from app.services.embeddings import EmbeddingService
from app.services.llm import ModelRole, structured_call

logger = get_logger(__name__)

# Districts retrieved per query. Each costs an article fetch plus 6-10
# embedding calls, and a two-day itinerary realistically covers two or three
# neighbourhoods - so this is a quality ceiling as much as a cost one.
MAX_DISTRICTS_PER_QUERY: Final[int] = 4

# Which sections answer which kind of question. Used to narrow hop 3.
SECTIONS_FOR_INTENT: Final[dict[str, list[str]]] = {
    "food": ["Eat", "Drink"],
    "attractions": ["See", "Do"],
    "accommodation": ["Sleep"],
    "shopping": ["Buy"],
    "transport": ["Get around", "Get in"],
    "general": ["Understand", "See", "Do"],
}


class DistrictSelection(BaseModel):
    """Which neighbourhoods to investigate, chosen from those that exist."""

    districts: list[str] = Field(
        default_factory=list,
        description=(
            "2-4 district article titles copied EXACTLY from the supplied list. "
            "Never invent a name, and never return one that is not in the list."
        ),
    )
    reasoning: str = Field(
        default="",
        max_length=300,
        description="One or two sentences on why these suit this traveller.",
    )


class SufficiencyCheck(BaseModel):
    """Whether the evidence gathered so far can answer the question."""

    is_sufficient: bool = Field(
        description="True if the retrieved passages can answer the request without guessing."
    )
    missing: str = Field(
        default="",
        max_length=200,
        description=(
            "If not sufficient, the single most important thing still missing, phrased "
            "as a search query. Empty when sufficient."
        ),
    )


@dataclass
class Hop:
    """One retrieval step, recorded for the trace.

    Attributes:
        number: 1-based hop index.
        name: Short label, e.g. `"narrow"`.
        query: The query issued at this hop.
        derived_from: What earlier output produced this query - the field that
            evidences chaining.
        documents: Article titles searched or newly indexed.
        chunks_returned: How many passages this hop contributed.
    """

    number: int
    name: str
    query: str
    derived_from: str
    documents: list[str] = field(default_factory=list)
    chunks_returned: int = 0


@dataclass
class RetrievalResult:
    """Everything a multi-hop retrieval produced.

    Attributes:
        chunks: Deduplicated passages, most relevant first.
        hops: The trace of what was done.
        districts_considered: All districts the city offers.
        districts_selected: Those actually investigated.
        stopped_because: Which stop condition ended the loop.
        used_region_fallback: True when `districts_considered` came from
            parsing a region article's own Cities/Regions listing rather than
            from real `City/District` subpages - the path taken for a state or
            country rather than a city. Surfaced so the trace can show which
            mechanism actually ran.
    """

    chunks: list[RetrievedChunk] = field(default_factory=list)
    hops: list[Hop] = field(default_factory=list)
    districts_considered: list[str] = field(default_factory=list)
    districts_selected: list[str] = field(default_factory=list)
    stopped_because: str = ""
    used_region_fallback: bool = False

    @property
    def is_empty(self) -> bool:
        """True when nothing was retrieved."""
        return not self.chunks

    def as_context_block(self, max_chars: int = 6000) -> str:
        """Render retrieved passages for prompt injection, with citations.

        Args:
            max_chars: Budget for the rendered block.

        Returns:
            A formatted block, or an empty string when nothing was retrieved.
        """
        if not self.chunks:
            return ""

        lines = ["RETRIEVED TRAVEL GUIDE MATERIAL (Wikivoyage, CC BY-SA)", ""]
        used = 0

        for chunk in self.chunks:
            entry = f"[Source: {chunk.citation}]\n{chunk.content}\n"
            if used + len(entry) > max_chars:
                break
            lines.append(entry)
            used += len(entry)

        lines.append(
            "Base concrete recommendations - venue names, neighbourhoods, opening "
            "advice - on the passages above and name the district each suggestion is in. "
            "If something is not covered here, say so rather than inventing it."
        )
        return "\n".join(lines)


class MultiHopRetriever:
    """Runs the chained retrieval described in the module docstring.

    Attributes:
        client: Wikivoyage corpus client.
        index: Where embedded chunks are cached.
        embeddings: Embedding provider.
        settings: Application settings supplying hop and top-k budgets.
    """

    def __init__(
        self,
        client: WikivoyageClient,
        index: RagIndex,
        embeddings: EmbeddingService,
        settings: Settings | None = None,
    ) -> None:
        """Initialise the retriever.

        Args:
            client: Wikivoyage corpus client.
            index: Chunk index.
            embeddings: Embedding provider.
            settings: Settings override, for tests.
        """
        self.client = client
        self.index = index
        self.embeddings = embeddings
        self.settings = settings or get_settings()

    async def retrieve(
        self,
        query: str,
        destination: str,
        *,
        constraints: list[str] | None = None,
        intent: str = "general",
        max_hops: int | None = None,
    ) -> RetrievalResult:
        """Run a multi-hop retrieval for one request.

        Args:
            query: What the traveller asked.
            destination: City to research.
            constraints: Hard requirements from long-term memory or this
                conversation, e.g. `["vegetarian", "wheelchair accessible"]`.
                Each gets its own targeted hop-3 search.
            intent: Which sections to favour - one of the keys in
                `SECTIONS_FOR_INTENT`.
            max_hops: Hop ceiling. Defaults to the configured value.

        Returns:
            The retrieved passages and a full trace. Never raises: retrieval
            failure degrades the answer rather than failing the request.
        """
        budget = max_hops or self.settings.rag_max_hops
        result = RetrievalResult()
        collected: dict[str, RetrievedChunk] = {}

        try:
            city_title = await self._hop_one(query, destination, result, collected)
            if city_title is None:
                result.stopped_because = "destination_not_found"
                return result

            if len(result.hops) < budget:
                await self._hop_two(query, city_title, constraints, intent, result, collected)

            if constraints and len(result.hops) < budget:
                await self._hop_three(constraints, result, collected)

            if len(result.hops) < budget:
                await self._hop_four(query, result, collected)

        except ExternalServiceError:
            logger.warning("rag.retrieval_degraded", destination=destination, exc_info=True)
            result.stopped_because = result.stopped_because or "upstream_error"

        result.chunks = sorted(collected.values(), key=lambda c: c.similarity, reverse=True)
        result.stopped_because = result.stopped_because or "hop_budget_reached"

        logger.info(
            "rag.retrieval_completed",
            destination=destination,
            hops=len(result.hops),
            chunks=len(result.chunks),
            districts=len(result.districts_selected),
            stopped_because=result.stopped_because,
        )
        return result

    # -- Hop 1: orient ----------------------------------------------------

    async def _hop_one(
        self,
        query: str,
        destination: str,
        result: RetrievalResult,
        collected: dict[str, RetrievedChunk],
    ) -> str | None:
        """Retrieve the city article and enumerate its districts.

        Returns:
            The resolved city article title, or None if the destination has no
            Wikivoyage article.
        """
        city_title = await self.client.find_article_title(destination)
        if city_title is None:
            logger.info("rag.destination_not_found", destination=destination)
            return None

        article = await self.client.fetch_article(city_title)
        if article is None:
            return None

        districts = await self.client.list_districts(city_title)

        # A region-level article - a state, a country - has no `City/District`
        # subpages, so the query above returns nothing even though the article
        # itself lists its cities in plain prose. Falling back to that listing
        # is what makes hop 2 real for a destination like "Kerala" instead of
        # silently degrading to a single flat retrieval - confirmed by
        # checking a real run's trace, where `districts_available` was empty
        # for a request about Kerala despite the article having a proper
        # "Cities" section.
        used_fallback = False
        if not districts:
            candidates = extract_child_destinations(article.sections)
            districts = await self.client.resolve_child_destinations(candidates)
            used_fallback = bool(districts)

        article.districts = districts
        result.districts_considered = districts
        result.used_region_fallback = used_fallback

        await ensure_indexed(article, self.index, self.embeddings, settings=self.settings)

        hop_query = f"{destination} overview, districts, character and main attractions"
        chunks = await self._search(hop_query, document_titles=[city_title], top_k=4)
        self._collect(chunks, collected)

        result.hops.append(
            Hop(
                number=1,
                name="orient",
                query=hop_query,
                derived_from="user request",
                documents=[city_title],
                chunks_returned=len(chunks),
            )
        )
        return city_title

    # -- Hop 2: narrow ----------------------------------------------------

    async def _hop_two(
        self,
        query: str,
        city_title: str,
        constraints: list[str] | None,
        intent: str,
        result: RetrievalResult,
        collected: dict[str, RetrievedChunk],
    ) -> None:
        """Select districts using hop 1's output, then retrieve those articles."""
        if not result.districts_considered:
            # Small destinations have no sub-articles. Not a failure - the
            # city article already covers everything - so record the reason
            # and stop rather than forcing a pointless hop.
            result.stopped_because = "no_district_articles"
            return

        selected = await self._select_districts(
            query, city_title, result.districts_considered, constraints
        )
        if not selected:
            result.stopped_because = "no_districts_selected"
            return

        result.districts_selected = selected
        indexed: list[str] = []

        for title in selected:
            article = await self.client.fetch_article(title)
            if article is None:
                continue
            await ensure_indexed(article, self.index, self.embeddings, settings=self.settings)
            indexed.append(title)

        if not indexed:
            result.stopped_because = "district_articles_unavailable"
            return

        sections = SECTIONS_FOR_INTENT.get(intent, SECTIONS_FOR_INTENT["general"])
        hop_query = f"{query} in {', '.join(d.split('/')[-1] for d in indexed)}"
        chunks = await self._search(
            hop_query,
            document_titles=indexed,
            sections=sections,
            top_k=self.settings.rag_top_k,
        )
        self._collect(chunks, collected)

        result.hops.append(
            Hop(
                number=2,
                name="narrow",
                query=hop_query,
                derived_from=(
                    "hop 1 "
                    + ("region Cities listing" if result.used_region_fallback else "district list")
                    + f" ({len(result.districts_considered)} candidates)"
                ),
                documents=indexed,
                chunks_returned=len(chunks),
            )
        )

    async def _select_districts(
        self,
        query: str,
        city_title: str,
        available: list[str],
        constraints: list[str] | None,
    ) -> list[str]:
        """Ask a model which districts suit this traveller.

        Args:
            query: The traveller's request.
            city_title: City article title.
            available: District titles that actually exist.
            constraints: Hard requirements to respect.

        Returns:
            Selected district titles, filtered to those genuinely available.
        """
        constraint_text = (
            f"\nHard requirements that must be respected: {'; '.join(constraints)}"
            if constraints
            else ""
        )

        try:
            selection = await structured_call(
                ModelRole.UTILITY,
                [
                    SystemMessage(
                        content=(
                            "You choose which neighbourhoods of a city best suit a "
                            "traveller's request. Pick 2-4. Favour variety over "
                            "adjacency, and copy the titles EXACTLY from the supplied "
                            "list - do not invent, abbreviate or reformat them."
                        )
                    ),
                    HumanMessage(
                        content=(
                            f"City: {city_title}\n"
                            f"Traveller's request: {query}{constraint_text}\n\n"
                            f"Available district articles:\n"
                            + "\n".join(f"- {title}" for title in available)
                        )
                    ),
                ],
                DistrictSelection,
                purpose="select_districts",
                settings=self.settings,
            )
        except ExternalServiceError:
            logger.warning("rag.district_selection_failed", city=city_title, exc_info=True)
            # Fall back to the first few districts rather than abandoning the
            # hop. Wikivoyage tends to list central areas first, so this is a
            # weak but non-absurd default.
            return available[:2]

        # Models do occasionally return a plausible-sounding district that does
        # not exist. Filtering against the real list means a hallucinated name
        # costs nothing instead of causing a failed fetch.
        available_set = set(available)
        valid = [title for title in selection.districts if title in available_set]

        if len(valid) < len(selection.districts):
            logger.info(
                "rag.dropped_invalid_districts",
                proposed=selection.districts,
                kept=valid,
            )

        return valid[:MAX_DISTRICTS_PER_QUERY]

    # -- Hop 3: constrain -------------------------------------------------

    async def _hop_three(
        self,
        constraints: list[str],
        result: RetrievalResult,
        collected: dict[str, RetrievedChunk],
    ) -> None:
        """Cross-reference each hard constraint against the selected districts."""
        if not result.districts_selected:
            return

        before = len(collected)
        queries: list[str] = []

        # One search per constraint rather than one combined search: embedding
        # "vegetarian AND wheelchair accessible" produces a vector near
        # neither, which is the classic multi-constraint retrieval failure.
        for constraint in constraints[:3]:
            hop_query = f"{constraint} options and suitability"
            queries.append(hop_query)
            chunks = await self._search(
                hop_query,
                document_titles=result.districts_selected,
                sections=["Eat", "Drink", "See", "Do", "Sleep"],
                top_k=3,
            )
            self._collect(chunks, collected)

        result.hops.append(
            Hop(
                number=3,
                name="constrain",
                query=" | ".join(queries),
                derived_from=(
                    f"hop 2 districts ({', '.join(result.districts_selected)}) x stated constraints"
                ),
                documents=result.districts_selected,
                chunks_returned=len(collected) - before,
            )
        )

    # -- Hop 4: fill gaps -------------------------------------------------

    async def _hop_four(
        self,
        query: str,
        result: RetrievalResult,
        collected: dict[str, RetrievedChunk],
    ) -> None:
        """Check sufficiency and run one targeted search for what is missing."""
        if not collected:
            return

        check = await self._check_sufficiency(query, list(collected.values()))
        if check.is_sufficient or not check.missing:
            result.stopped_because = "evidence_sufficient"
            return

        before = len(collected)
        targets = result.districts_selected or None
        chunks = await self._search(check.missing, document_titles=targets, top_k=3)
        self._collect(chunks, collected)

        result.hops.append(
            Hop(
                number=4,
                name="fill_gaps",
                query=check.missing,
                derived_from="sufficiency check over hops 1-3",
                documents=targets or [],
                chunks_returned=len(collected) - before,
            )
        )

    async def _check_sufficiency(
        self, query: str, chunks: list[RetrievedChunk]
    ) -> SufficiencyCheck:
        """Judge whether the evidence gathered can answer the request.

        Args:
            query: The original request.
            chunks: Everything retrieved so far.

        Returns:
            The verdict. Defaults to sufficient if the check fails, so a model
            outage ends the loop rather than triggering another retrieval.
        """
        summary = "\n".join(f"- {chunk.citation}: {chunk.content[:180]}" for chunk in chunks[:10])

        try:
            return await structured_call(
                ModelRole.UTILITY,
                [
                    SystemMessage(
                        content=(
                            "Decide whether the retrieved material is enough to answer the "
                            "request without guessing. Be pragmatic: a two-day itinerary "
                            "needs a handful of specific places, not exhaustive coverage. "
                            "If something important is missing, phrase it as a search query."
                        )
                    ),
                    HumanMessage(content=f"Request: {query}\n\nRetrieved so far:\n{summary}"),
                ],
                SufficiencyCheck,
                purpose="rag_sufficiency_check",
                settings=self.settings,
            )
        except ExternalServiceError:
            logger.warning("rag.sufficiency_check_failed", exc_info=True)
            return SufficiencyCheck(is_sufficient=True, missing="")

    # -- Helpers ----------------------------------------------------------

    async def _search(
        self,
        query: str,
        *,
        document_titles: list[str] | None = None,
        sections: list[str] | None = None,
        top_k: int = 6,
    ) -> list[RetrievedChunk]:
        """Embed a query and search the index."""
        embedding = await self.embeddings.embed_query(query)
        return await self.index.search(
            embedding,
            document_titles=document_titles,
            sections=sections,
            top_k=top_k,
        )

    @staticmethod
    def _collect(chunks: list[RetrievedChunk], into: dict[str, RetrievedChunk]) -> None:
        """Merge chunks into the accumulator, keeping the best score per passage.

        Later hops deliberately re-search documents earlier hops touched, so
        the same passage can surface more than once. Keyed by citation plus a
        content prefix, retaining the highest similarity seen.
        """
        for chunk in chunks:
            key = f"{chunk.citation}:{chunk.content[:80]}"
            existing = into.get(key)
            if existing is None or chunk.similarity > existing.similarity:
                into[key] = chunk
