"""The toolbox handed to the model.

This module is where tool *implementations* become tool *schemas*. Each entry
is a thin wrapper that exists for one reason: to control exactly what the
model sees.

The implementations take parameters the model must never supply - `settings`
for dependency injection, and identity, which comes from the request context
rather than from the model (see `app/tools/context.py`). Exposing those would
be worse than untidy: every extra parameter is another thing the model can get
wrong, and a model-suppliable `user_id` is a data-isolation hole.

So the wrappers restate only the arguments that represent a genuine decision.
Their docstrings become the tool descriptions the model reads, which makes
them prompt engineering rather than documentation - they say when to prefer
one tool over another, because that guidance is what produces sensible tool
selection without any hardcoded routing.

Nothing in this system inspects a user message for keywords to decide which
tool to run. The model chooses, every time, from these descriptions alone.

**Every call into the underlying implementation uses keyword arguments,
never positional ones.** This was a real bug, not a style choice: the tools
are wrapped by `resilient_tool` (see `app/tools/base.py`), which records each
call's arguments for the admin trace and, for expensive tools, computes a
cache key from named arguments. Both read `**kwargs` only. A positional call
arrives as `*args`, so the trace silently recorded `{}` for every single tool
call, and result caching silently never activated - discovered by pulling a
real run's trace and finding every `arguments` column empty.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool

from app.core.logging import get_logger
from app.tools import memory_tools, places, travel_guide, travel_logistics, weather, web_search

logger = get_logger(__name__)


def _serialise(result: Any) -> dict[str, Any]:
    """Convert a `ToolResult` into what the model receives.

    Internal bookkeeping is dropped, and `status` and `message` are kept
    because a degraded result's message is the instruction that tells the
    model how to proceed without this data.

    Args:
        result: The `ToolResult` returned by a tool implementation.

    Returns:
        A JSON-serialisable dict.
    """
    payload = result.model_dump(mode="json")
    return {
        "status": payload["status"],
        "source": payload["source"],
        "data": payload["data"],
        "note": payload["message"],
    }


# ---------------------------------------------------------------------------
# Wrappers
# ---------------------------------------------------------------------------
#
# **Docstrings here are billed; comments are free.** Every docstring below is
# serialised into the tool schema and re-sent to the model on *every* call of
# the executor's ReAct loop. Measured against the live API, the earlier prose
# versions came to 15,032 characters - roughly 3,750 tokens per call, and
# about 45,000 tokens across one full plan. Groq's free tier allows 100,000
# tokens per day, so tool descriptions alone were consuming close to half a
# day's budget per itinerary.
#
# So these docstrings say only what the model needs in order to pick the right
# tool and fill its arguments. The reasoning a human reader wants lives in
# comments like this one, which cost nothing at inference time.


@tool
async def get_weather_forecast(
    location: str, start_date: str | None = None, end_date: str | None = None
) -> dict[str, Any]:
    """Weather forecast for a city and date range.

    Use when weather changes a recommendation. Beyond ~16 days you get
    seasonal averages, labelled as such - never present those as a forecast.

    Args:
        location: One city or town. Not a region or country.
        start_date: YYYY-MM-DD. Defaults to today.
        end_date: YYYY-MM-DD. Defaults to the day after start.
    """
    return _serialise(
        await weather.get_weather_forecast(
            location=location, start_date=start_date, end_date=end_date
        )
    )


@tool
async def find_places(
    location: str,
    categories: list[str],
    conditions: list[str] | None = None,
    radius_metres: int = 5000,
    limit: int = 12,
) -> dict[str, Any]:
    """Real mapped venues with addresses and coordinates.

    Use for specific places to put in an itinerary. Pass `conditions` for any
    dietary or access requirement - it filters at the source, which is far
    more reliable than filtering results yourself.

    Args:
        location: City or district, e.g. "Higashiyama, Kyoto".
        categories: attractions, sights, museums, culture, parks, nature,
            restaurants, cafes, bars, food, nightlife, shopping, markets,
            hotels, religious, viewpoints, beaches.
        conditions: vegetarian, vegan, halal, kosher, gluten_free,
            wheelchair_accessible, dog_friendly, free_entry, internet.
        radius_metres: Search radius.
        limit: Max results.
    """
    return _serialise(
        await places.find_places(
            location=location,
            categories=categories,
            conditions=conditions,
            radius_metres=radius_metres,
            limit=limit,
        )
    )


@tool
async def search_travel_guide(
    destination: str,
    question: str,
    intent: str = "general",
    constraints: list[str] | None = None,
) -> dict[str, Any]:
    """Deep research on a destination from curated travel guides.

    First choice for what a place is like: districts, sights, food, where to
    stay. Free and multi-hop. Use `search_web` for anything time-sensitive.

    Args:
        destination: City or region to research.
        question: What you want to know, specifically.
        intent: general, food, attractions, accommodation, shopping, transport.
        constraints: Hard requirements, e.g. ["vegetarian"].
    """
    return _serialise(
        await travel_guide.search_travel_guide(
            destination=destination,
            question=question,
            intent=intent,
            constraints=constraints,
        )
    )


@tool
async def search_web(
    query: str, max_results: int = 5, deep_search: bool = False, recent_only: bool = False
) -> dict[str, Any]:
    """Live web search for time-sensitive facts.

    Use for events on given dates, closures, current prices. For stable
    background use search_travel_guide - free and better structured.

    Args:
        query: Self-contained query including city and dates.
        max_results: 1-10.
        deep_search: Slower and costlier; only after a basic search failed.
        recent_only: Past month only. Good for events, wrong for background.
    """
    return _serialise(
        await web_search.search_web(
            query=query,
            max_results=max_results,
            deep_search=deep_search,
            recent_only=recent_only,
        )
    )


@tool
async def search_flights(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: str | None = None,
    cabin: str = "economy",
    adults: int = 1,
) -> dict[str, Any]:
    """Flights between two cities. Simulated data, not bookable.

    One call covers a round trip - pass return_date rather than calling twice.

    Args:
        origin: Departure city.
        destination: Arrival city.
        departure_date: YYYY-MM-DD.
        return_date: YYYY-MM-DD for a round trip.
        cabin: economy, premium_economy, business, first.
        adults: 1-9.
    """
    return _serialise(
        await travel_logistics.search_flights(
            origin=origin,
            destination=destination,
            departure_date=departure_date,
            return_date=return_date,
            cabin=cabin,
            adults=adults,
        )
    )


@tool
async def search_accommodation(
    city: str,
    check_in: str,
    check_out: str,
    guests: int = 2,
    max_price_per_night: float | None = None,
) -> dict[str, Any]:
    """Places to stay in a city. Simulated data, not bookable.

    Pass max_price_per_night whenever a budget is known, including a
    remembered one, so results respect it rather than being filtered after.

    Args:
        city: City name.
        check_in: YYYY-MM-DD.
        check_out: YYYY-MM-DD.
        guests: 1-8.
        max_price_per_night: Budget ceiling in USD.
    """
    return _serialise(
        await travel_logistics.search_accommodation(
            city=city,
            check_in=check_in,
            check_out=check_out,
            guests=guests,
            max_price_per_night=max_price_per_night,
        )
    )


@tool
async def recall_user_preferences(query: str, limit: int = 6) -> dict[str, Any]:
    """Look up stored facts about this traveller.

    Relevant ones are loaded automatically before you start, so use this only
    for something the opening request did not cover.

    Args:
        query: The kind of facts wanted, e.g. "dietary restrictions".
        limit: 1-15.
    """
    return _serialise(await memory_tools.recall_user_preferences(query=query, limit=limit))


@tool
async def save_user_preference(
    fact: str, category: str = "preference", subject: str = "other"
) -> dict[str, Any]:
    """Remember a durable fact about the traveller.

    Only for things still true on a different trip. Never trip-specific
    details like these dates or this hotel.

    Args:
        fact: One atomic fact, third person, no personal identifiers.
        category: constraint, preference, identity, experience.
        subject: diet, allergy, accessibility, budget, pace, accommodation,
            transport, interests, climate, companions, pets, home_base,
            languages, visited, avoid, other.
    """
    return _serialise(
        await memory_tools.save_user_preference(fact=fact, category=category, subject=subject)
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ALL_TOOLS: list[BaseTool] = [
    search_travel_guide,
    find_places,
    get_weather_forecast,
    search_web,
    search_flights,
    search_accommodation,
    recall_user_preferences,
    save_user_preference,
]


#: Which tool each composer service toggle removes when switched off. Only
#: `flights` and `stays` appear here because they map 1:1 to a tool;
#: `attractions` and `restaurants` share `find_places` and
#: `search_travel_guide` with everything else, so switching those off is
#: enforced through prompt instructions rather than by amputating a tool the
#: remaining services still need.
_TOOL_FOR_SERVICE = {
    "flights": "search_flights",
    "stays": "search_accommodation",
}


def get_tools(*, include_memory: bool = True, focus: list[str] | None = None) -> list[BaseTool]:
    """Return the tools available to the agent.

    Args:
        include_memory: Whether to include the memory tools. Disabled for
            users who have opted out of long-term memory - removing the tools
            entirely is a stronger guarantee than instructing the model not to
            use them.
        focus: Services the traveller has enabled in the UI. None means all.
            A switched-off service's tool is removed for the same reason the
            memory tools are: absence is a guarantee, an instruction is a
            request.

    Returns:
        The tool list to pass to the executor.
    """
    excluded: set[str] = set()

    if not include_memory:
        excluded.update({"recall_user_preferences", "save_user_preference"})

    if focus is not None:
        enabled = {service.strip().lower() for service in focus}
        excluded.update(
            tool_name for service, tool_name in _TOOL_FOR_SERVICE.items() if service not in enabled
        )

    if not excluded:
        return list(ALL_TOOLS)
    return [tool_ for tool_ in ALL_TOOLS if tool_.name not in excluded]


def get_tool_names() -> list[str]:
    """Return the names of all registered tools, for prompts and tests."""
    return [tool_.name for tool_ in ALL_TOOLS]


#: Which tools each kind of plan step can actually use.
#:
#: Every tool handed to the executor costs its full schema on *every* call of
#: that step's ReAct loop, whether or not it is used. A research step has no
#: business booking a flight, and a logistics step does not need to save a
#: memory - so sending those schemas is paying, repeatedly, for options that
#: were never going to be taken.
#:
#: Narrowing by step kind roughly halves the per-call payload. It is not a
#: constraint on the model's judgement in any meaningful sense: the planner
#: already decided what kind of work this step is, and this only removes tools
#: that would be wrong for that kind of work. Tool *choice within the step* is
#: still entirely the model's.
_TOOLS_FOR_STEP_KIND: dict[str, tuple[str, ...]] = {
    "research": (
        "search_travel_guide",
        "find_places",
        "search_web",
        "recall_user_preferences",
    ),
    "logistics": (
        "search_flights",
        "search_accommodation",
        "get_weather_forecast",
        "find_places",
    ),
    "verify": (
        "search_web",
        "get_weather_forecast",
        "search_travel_guide",
    ),
    # Composition needs no tools at all: by then the evidence is gathered, and
    # the responder writes the answer.
    "compose": (),
}


def get_tools_for_step(
    kind: str, *, include_memory: bool = True, focus: list[str] | None = None
) -> list[BaseTool]:
    """Return the tools appropriate to one kind of plan step.

    Args:
        kind: The step's `StepKind` - research, logistics, verify or compose.
        include_memory: Whether memory tools may be included.
        focus: Services the traveller has enabled; a switched-off service's
            tool is removed exactly as in `get_tools`.

    Returns:
        The tool list for this step. An unrecognised kind falls back to the
        full toolbox rather than to nothing, so a new step kind degrades to
        "more expensive" instead of "unable to act".
    """
    allowed = _TOOLS_FOR_STEP_KIND.get(kind)
    available = get_tools(include_memory=include_memory, focus=focus)

    if allowed is None:
        return available
    return [tool_ for tool_ in available if tool_.name in allowed]
