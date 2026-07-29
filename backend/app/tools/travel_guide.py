"""Travel guide retrieval tool.

Exposes the multi-hop retriever to the agent as a single tool call. The model
decides *when* deep research is worthwhile; the retriever decides *how many*
hops that takes.

This is the tool to prefer for anything about a destination's character,
neighbourhoods, or what is actually worth doing there. It costs no API quota
(Wikivoyage is free) and returns better-structured material than web search,
so the tool description steers the model here first and reserves `search_web`
for time-sensitive facts the guide cannot cover.
"""

from __future__ import annotations

from typing import Final

from app.core.logging import get_logger
from app.rag.retriever import SECTIONS_FOR_INTENT
from app.tools.base import ToolResult, resilient_tool
from app.tools.context import get_tool_context

logger = get_logger(__name__)

GUIDE_UNAVAILABLE: Final[str] = (
    "The travel guide could not be reached. Use find_places for concrete venues and "
    "search_web for background instead, and tell the user your suggestions are less "
    "detailed than usual. Do not retry this tool for the same destination."
)


@resilient_tool(source="wikivoyage", unavailable_message=GUIDE_UNAVAILABLE)
async def search_travel_guide(
    destination: str,
    question: str,
    intent: str = "general",
    constraints: list[str] | None = None,
) -> ToolResult:
    """Research a destination in depth using curated travel guides.

    Prefer this over `search_web` for anything about what a place is like:
    which neighbourhoods suit which travellers, what is genuinely worth
    seeing, where to eat, which area to stay in. It runs a chained search that
    first works out the city's districts, then investigates the ones matching
    this traveller, then cross-references their stated requirements - so one
    call does the work of several.

    Use `search_web` instead for things that change: current prices, opening
    hours, events on specific dates, temporary closures.

    Args:
        destination: City to research, e.g. `"Kyoto"`.
        question: What you want to know, in full, e.g. `"quiet traditional
            neighbourhoods with good vegetarian food and temples"`. The more
            specific this is, the better the district selection.
        intent: What kind of information you need - `general`, `food`,
            `attractions`, `accommodation`, `shopping`, or `transport`. Narrows
            the search to the relevant guide sections.
        constraints: Hard requirements to cross-reference, e.g.
            `["vegetarian", "wheelchair accessible"]`. Each gets its own
            targeted search, so pass them here rather than folding them into
            `question`.

    Returns:
        A `ToolResult` whose data contains retrieved passages with their
        source citations, the districts investigated, and the retrieval trace.
    """
    context = get_tool_context()

    if context.retriever is None:
        return ToolResult.degraded(source="wikivoyage", message=GUIDE_UNAVAILABLE)

    normalised_intent = intent.strip().lower()
    if normalised_intent not in SECTIONS_FOR_INTENT:
        # Fall back rather than reject: an unrecognised intent only widens the
        # section filter, whereas failing the call loses the whole retrieval.
        logger.info("travel_guide.unknown_intent", supplied=intent, fallback="general")
        normalised_intent = "general"

    result = await context.retriever.retrieve(
        query=question,
        destination=destination,
        constraints=constraints or None,
        intent=normalised_intent,
    )

    if result.is_empty:
        return ToolResult.ok(
            source="wikivoyage",
            data={"passages": [], "hops": []},
            message=(
                f"No travel guide material was found for {destination!r} "
                f"(reason: {result.stopped_because}). Check the spelling of the city, "
                "or fall back to search_web and find_places."
            ),
        )

    passages = [
        {
            "source": chunk.citation,
            "url": chunk.url,
            "section": chunk.section,
            "content": chunk.content,
            "relevance": round(chunk.similarity, 3),
        }
        for chunk in result.chunks
    ]

    trace = [
        {
            "hop": hop.number,
            "step": hop.name,
            "query": hop.query,
            "derived_from": hop.derived_from,
            "documents": hop.documents,
            "passages_found": hop.chunks_returned,
        }
        for hop in result.hops
    ]

    covered = ", ".join(d.split("/")[-1] for d in result.districts_selected)

    return ToolResult.ok(
        source="wikivoyage",
        data={
            "destination": destination,
            "districts_available": result.districts_considered[:20],
            "districts_investigated": result.districts_selected,
            "passages": passages,
            "hops": trace,
        },
        message=(
            f"{len(passages)} passages from {len(result.hops)} chained retrieval steps, "
            f"covering {covered or 'the city article'}. "
            "Ground your recommendations in these passages and name the district for each "
            "suggestion. Content is from Wikivoyage (CC BY-SA) - if something is not "
            "covered here, say so rather than filling the gap from memory."
        ),
    )
