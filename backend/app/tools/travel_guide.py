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
from app.services.usage import record_rag_hops
from app.tools.base import ToolResult, resilient_tool
from app.tools.context import get_tool_context

logger = get_logger(__name__)

# How much retrieved text goes back to the model in one tool result.
#
# Sized against the LLM budget, not against how much was retrieved. A
# four-hop retrieval routinely returns 15 passages of up to 2,000 characters;
# handing all of that back is ~30 KB in a single tool message, which on Groq's
# free tier exceeds the per-minute token allowance and fails the step with a
# generic provider error - the retrieval having succeeded, which makes it a
# confusing failure to diagnose.
#
# The passages are already ranked, so truncating keeps the best evidence and
# discards the tail nobody would have read.
MAX_PASSAGES_RETURNED: Final[int] = 6
MAX_CHARS_PER_PASSAGE: Final[int] = 700
MAX_TOTAL_CHARS: Final[int] = 4000

GUIDE_UNAVAILABLE: Final[str] = (
    "The travel guide could not be reached. Use find_places for concrete venues and "
    "search_web for background instead, and tell the user your suggestions are less "
    "detailed than usual. Do not retry this tool for the same destination."
)


@resilient_tool(
    source="wikivoyage",
    unavailable_message=GUIDE_UNAVAILABLE,
    # A four-hop retrieval is driven by the destination, not by how the
    # question is phrased. Without this the model's rewordings each trigger
    # a fresh ~9s retrieval and another few thousand tokens.
    cache_on=("destination", "intent"),
)
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

    record_rag_hops(len(result.hops))

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

    passages: list[dict[str, object]] = []
    budget = MAX_TOTAL_CHARS

    for chunk in result.chunks[:MAX_PASSAGES_RETURNED]:
        text = chunk.content[:MAX_CHARS_PER_PASSAGE]
        if len(text) > budget:
            break
        budget -= len(text)
        passages.append(
            {
                "source": chunk.citation,
                "url": chunk.url,
                "section": chunk.section,
                "content": text,
                "relevance": round(chunk.similarity, 3),
            }
        )

    # The trace is for the admin viewer, not for the model - keep it compact so
    # it does not eat the context budget the passages need.
    trace = [
        {
            "hop": hop.number,
            "step": hop.name,
            "query": hop.query[:120],
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
            f"{len(passages)} of {len(result.chunks)} passages (the most relevant), from "
            f"{len(result.hops)} chained retrieval steps, covering "
            f"{covered or 'the city article'}. "
            "Ground your recommendations in these passages and name the district for each "
            "suggestion. Content is from Wikivoyage (CC BY-SA) - if something is not "
            "covered here, say so rather than filling the gap from memory."
        ),
    )
