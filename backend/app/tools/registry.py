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


@tool
async def get_weather_forecast(
    location: str, start_date: str | None = None, end_date: str | None = None
) -> dict[str, Any]:
    """Get the weather forecast for a city over a date range.

    Call this whenever weather could change a recommendation - deciding
    between indoor and outdoor activities, advising what to pack, or warning
    about a rainy day. Forecasts are reliable up to about 16 days ahead;
    beyond that you will get typical seasonal conditions clearly labelled as
    such, which you must not present as a forecast.

    Args:
        location: City name, e.g. "Kyoto".
        start_date: First day as YYYY-MM-DD. Defaults to today.
        end_date: Last day as YYYY-MM-DD. Defaults to the day after start.
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
    """Find real, mapped places near a location, filtered by attributes.

    Returns actual venues with addresses and coordinates from OpenStreetMap.
    Use this when you need specific places to put in an itinerary, rather
    than general description of an area.

    Always pass `conditions` when the traveller has a dietary, accessibility
    or pet requirement - it filters at the source, which is far more reliable
    than filtering the results yourself afterwards.

    Args:
        location: City, district or neighbourhood, e.g. "Higashiyama, Kyoto".
        categories: What to look for: attractions, sights, museums, culture,
            parks, nature, restaurants, cafes, bars, food, nightlife,
            shopping, markets, hotels, hostels, religious, viewpoints,
            beaches.
        conditions: Hard filters: vegetarian, vegan, halal, kosher,
            gluten_free, wheelchair_accessible, dog_friendly, free_entry,
            internet.
        radius_metres: Search radius from the location centre.
        limit: Maximum places to return.
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
    """Research a destination in depth using curated travel guides.

    Your first choice for understanding a place: which neighbourhoods suit
    which travellers, what is genuinely worth seeing, where to eat, which
    area to stay in. Runs a chained search that works out the city's
    districts, investigates the ones matching this traveller, then
    cross-references their requirements - so one call does the work of
    several, and it costs no API quota.

    Use `search_web` instead for anything time-sensitive: current prices,
    opening hours, events on particular dates, temporary closures.

    Args:
        destination: City to research, e.g. "Kyoto".
        question: What you want to know, stated fully and specifically. The
            more specific, the better the neighbourhoods chosen.
        intent: general, food, attractions, accommodation, shopping, or
            transport.
        constraints: Hard requirements to cross-reference, e.g.
            ["vegetarian"]. Pass them here rather than inside `question`.
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
    """Search the live web for current, time-sensitive information.

    Use for things that change and that a travel guide cannot know: festivals
    and events during the traveller's dates, temporary closures, current
    ticket prices, recent traveller reports. For stable background about a
    destination, use `search_travel_guide` first - it is free and better
    structured.

    Args:
        query: A specific, self-contained query including the city and any
            date context, e.g. "Kyoto festivals August 2026".
        max_results: How many results to return, 1-10.
        deep_search: Run a more thorough search. Costs more quota - reserve it
            for when a basic search has already failed.
        recent_only: Restrict to the past month. Good for events and
            closures, wrong for general background.
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
    """Search for flights between two cities.

    Use when the traveller asks about getting somewhere, wants a transport
    budget, or is comparing dates. Not needed for planning what to do once
    they have arrived.

    Args:
        origin: Departure city, e.g. "Singapore".
        destination: Arrival city, e.g. "Tokyo".
        departure_date: Outbound date as YYYY-MM-DD.
        return_date: Optional inbound date as YYYY-MM-DD.
        cabin: economy, premium_economy, business, or first.
        adults: Number of adult passengers, 1-9.
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
    """Search for places to stay in a city.

    Use when the traveller asks where to stay or wants an accommodation
    budget. Always pass `max_price_per_night` when a budget is known -
    including one you recalled from their stored preferences - so results
    respect it rather than being filtered afterwards.

    Args:
        city: City name, e.g. "Kyoto".
        check_in: Arrival date as YYYY-MM-DD.
        check_out: Departure date as YYYY-MM-DD.
        guests: Number of guests, 1-8.
        max_price_per_night: Nightly budget ceiling in USD.
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
    """Look up what is already known about this traveller from past conversations.

    Their relevant preferences are loaded automatically before you start, so
    reach for this only when the conversation moves to something the opening
    request did not cover - checking dietary needs before recommending
    restaurants, or budget before suggesting hotels.

    Args:
        query: What you want to know, described as the kind of facts you are
            after, e.g. "dietary restrictions and food preferences".
        limit: Maximum facts to return, 1-15.
    """
    return _serialise(await memory_tools.recall_user_preferences(query=query, limit=limit))


@tool
async def save_user_preference(
    fact: str, category: str = "preference", subject: str = "other"
) -> dict[str, Any]:
    """Remember a durable fact about the traveller for future conversations.

    Use when they state something that will still matter on a completely
    different trip - a dietary requirement, budget level, travel style - and
    especially when they correct something you had wrong.

    Do not use it for trip-specific details like these dates, this hotel or
    this destination. Those are useless next time and clutter their profile.

    Args:
        fact: One atomic fact, third person and self-contained, e.g.
            "Traveller is vegetarian and does not eat fish." Never include
            names, contact details or payment information.
        category: constraint (a hard requirement), preference (a soft
            leaning), identity (a stable attribute), or experience (somewhere
            they have been).
        subject: diet, allergy, accessibility, budget, pace, accommodation,
            transport, interests, climate, companions, pets, home_base,
            languages, visited, avoid, or other.
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
