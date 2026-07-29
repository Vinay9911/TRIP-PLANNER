"""Live web search via Tavily.

This tool covers what the other sources structurally cannot: anything
time-sensitive. Wikivoyage is a curated guide that lags real events, and
OpenStreetMap has no notion of a festival happening next weekend or a museum
closed for refurbishment. Web search fills that gap.

Tavily rather than a raw search engine because it returns extracted page
*content* rather than a list of links. An agent given ten URLs must then fetch
and parse each one - more latency, more failure modes, and a scraping problem
nobody asked for. Tavily does that server-side and returns text ready to
reason over.

The free tier is 1,000 credits a month, and a basic search costs one credit
while an advanced search costs two. That budget is small enough that the depth
choice is exposed as an argument rather than hardcoded: routine lookups stay
cheap, and the agent spends the expensive option only when a question warrants
it. Search depth is capped by policy here so a runaway loop cannot drain a
month's quota in one conversation.
"""

from __future__ import annotations

from typing import Any, Final

from app.core.config import Settings, get_settings
from app.core.errors import ConfigurationError
from app.services.http import request_json
from app.tools.base import ToolResult, resilient_tool

SEARCH_URL: Final[str] = "https://api.tavily.com/search"

# Domains that are almost always noise for travel planning: aggregator spam
# and content farms that restate the same generic top-ten list.
BLOCKED_DOMAINS: Final[list[str]] = ["pinterest.com", "quora.com"]

SEARCH_UNAVAILABLE: Final[str] = (
    "Web search is unavailable right now. Rely on the travel guide tool "
    "(search_travel_guide) and the attractions database instead, and tell the user "
    "that current event listings, prices and opening hours could not be verified. "
    "Do not retry this tool in this conversation."
)


@resilient_tool(source="tavily", unavailable_message=SEARCH_UNAVAILABLE)
async def search_web(
    query: str,
    max_results: int = 5,
    deep_search: bool = False,
    recent_only: bool = False,
    settings: Settings | None = None,
) -> ToolResult:
    """Search the live web for current information.

    Use this for anything that changes: events during the travel dates,
    temporary closures, current prices, recent traveller reports. For stable
    background on a destination, prefer `search_travel_guide`, which is free
    and returns better-structured material.

    Args:
        query: A specific, self-contained search query. Include the city and
            any date context, e.g. `"Kyoto festivals August 2026"` rather
            than `"festivals"`.
        max_results: How many results to return, 1-10.
        deep_search: Whether to run a more thorough search. Costs twice as
            much quota, so reserve it for questions a basic search has
            already failed to answer.
        recent_only: Restrict results to the past month. Use for events and
            closures, not for general background.
        settings: Settings override, for tests.

    Returns:
        A `ToolResult` whose data contains a short synthesised answer and the
        supporting results with their source URLs.
    """
    cfg = settings or get_settings()
    api_key = cfg.tavily_api_key.get_secret_value()
    if not api_key:
        raise ConfigurationError(
            "TAVILY_API_KEY is not set. Get a free key at https://app.tavily.com "
            "and add it to your .env file."
        )

    cleaned = query.strip()
    if not cleaned:
        return ToolResult.invalid(
            source="tavily",
            message="The `query` argument cannot be empty. Supply a specific search query.",
        )

    body: dict[str, Any] = {
        "query": cleaned,
        "search_depth": "advanced" if deep_search else "basic",
        # Clamped rather than trusted: the API rejects out-of-range values,
        # and a model occasionally asks for 50.
        "max_results": max(1, min(max_results, 10)),
        # Tavily's own synthesis, which saves the agent a summarisation call
        # and therefore one request against the LLM daily quota.
        "include_answer": True,
        "exclude_domains": BLOCKED_DOMAINS,
    }
    if recent_only:
        body["time_range"] = "month"

    payload = await request_json(
        "POST",
        SEARCH_URL,
        service="tavily",
        json_body=body,
        headers={"Authorization": f"Bearer {api_key}"},
    )

    results = [
        {
            "title": item.get("title"),
            "url": item.get("url"),
            # Truncated: full page extracts are long, and the agent has a
            # limited context budget shared across several tool results.
            "content": (item.get("content") or "")[:1200],
            "relevance": item.get("score"),
        }
        for item in (payload.get("results") or [])
    ]

    if not results:
        return ToolResult.ok(
            source="tavily",
            data={"query": cleaned, "answer": None, "results": []},
            message=(
                f"No web results for {cleaned!r}. Try a broader or differently-worded "
                "query, or fall back to the travel guide tool."
            ),
        )

    return ToolResult.ok(
        source="tavily",
        data={
            "query": cleaned,
            "answer": payload.get("answer"),
            "results": results,
        },
        message=(
            f"{len(results)} web results for {cleaned!r}. Treat the synthesised answer as a "
            "starting point and prefer specifics from the individual results. Cite the source "
            "URL for any claim about prices, dates or opening hours, since these change often."
        ),
    )
