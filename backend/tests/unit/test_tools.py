"""Tests for the tool layer.

Two claims in this project's documentation are only worth as much as the tests
behind them:

1. "A failing tool degrades rather than failing the run."
2. "There is no keyword routing - the model chooses tools."

The first is tested directly, by making upstream APIs fail in each way that
matters and asserting the tool still returns a usable result. The second
cannot be tested by calling an LLM (slow, costs quota, non-deterministic), so
it is tested structurally: the tool schemas the model sees are asserted to be
complete and free of parameters it must not control, and the codebase is
checked for the conditional-dispatch pattern that would constitute hardcoded
routing.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.core.errors import ExternalServiceError, ToolExecutionError
from app.tools.base import ToolResult, ToolStatus, resilient_tool
from app.tools.places import _deduplicate, _resolve_categories, _resolve_conditions
from app.tools.registry import ALL_TOOLS, get_tool_names, get_tools
from app.tools.travel_logistics import search_accommodation, search_flights
from app.tools.weather import get_weather_forecast

# ---------------------------------------------------------------------------
# Degradation contract
# ---------------------------------------------------------------------------


async def test_upstream_failure_degrades_instead_of_raising(tool_records):
    @resilient_tool(source="test-api", unavailable_message="Use something else instead.")
    async def failing_tool() -> ToolResult:
        raise ExternalServiceError("upstream exploded", service="test-api")

    result = await failing_tool()

    assert result.status is ToolStatus.DEGRADED
    assert result.message == "Use something else instead."
    assert len(tool_records) == 1
    assert tool_records[0].error_code == "external_service_error"


async def test_unexpected_bug_in_a_tool_also_degrades(tool_records):
    """A defect in our own code must not take down a run five tools could answer."""

    @resilient_tool(source="test-api", unavailable_message="Fallback guidance.")
    async def buggy_tool() -> ToolResult:
        raise ZeroDivisionError("oops")

    result = await buggy_tool()

    assert result.status is ToolStatus.DEGRADED
    assert tool_records[0].error_code == "unexpected_error"


async def test_bad_arguments_are_reported_as_invalid_not_degraded(tool_records):
    """The model must be able to tell 'you called me wrong' from 'I am down'."""

    @resilient_tool(source="test-api", unavailable_message="unavailable")
    async def picky_tool() -> ToolResult:
        raise ToolExecutionError("`when` must be YYYY-MM-DD.", tool_name="picky_tool")

    result = await picky_tool()

    assert result.status is ToolStatus.INVALID
    assert "YYYY-MM-DD" in result.message


async def test_degraded_message_tells_the_model_not_to_retry():
    """Without an explicit instruction, models loop on the failing call.

    Accepts either phrasing of the same instruction - "do not retry this tool"
    or "do not call this tool again" - since what matters is that the message
    forbids repetition, not which words it uses.
    """
    from app.tools.memory_tools import MEMORY_UNAVAILABLE
    from app.tools.places import PLACES_UNAVAILABLE
    from app.tools.travel_guide import GUIDE_UNAVAILABLE
    from app.tools.travel_logistics import FLIGHTS_UNAVAILABLE, STAYS_UNAVAILABLE
    from app.tools.weather import WEATHER_UNAVAILABLE
    from app.tools.web_search import SEARCH_UNAVAILABLE

    forbidding_phrases = ("do not retry", "do not call this tool again")

    for message in (
        WEATHER_UNAVAILABLE,
        PLACES_UNAVAILABLE,
        SEARCH_UNAVAILABLE,
        GUIDE_UNAVAILABLE,
        FLIGHTS_UNAVAILABLE,
        STAYS_UNAVAILABLE,
        MEMORY_UNAVAILABLE,
    ):
        lowered = message.lower()
        assert any(phrase in lowered for phrase in forbidding_phrases), (
            f"degraded message lacks a do-not-repeat instruction: {message[:80]}"
        )
        # It must also say what to do instead, not merely what went wrong.
        assert len(message) > 100, f"degraded message is too terse to act on: {message}"


async def test_every_tool_call_is_recorded_with_latency(tool_records):
    @resilient_tool(source="test-api", unavailable_message="x")
    async def fine_tool(city: str) -> ToolResult:
        return ToolResult.ok(source="test-api", data={"city": city})

    await fine_tool(city="Kyoto")

    assert len(tool_records) == 1
    record = tool_records[0]
    assert record.tool_name == "fine_tool"
    assert record.arguments == {"city": "Kyoto"}
    assert record.latency_ms >= 0


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------


@respx.mock
async def test_weather_degrades_when_the_api_times_out(settings, tool_records):
    respx.get(url__startswith=settings.open_meteo_geocoding_url).mock(
        side_effect=httpx.ConnectTimeout("timed out")
    )

    result = await get_weather_forecast("Kyoto", settings=settings)

    assert result.status is ToolStatus.DEGRADED
    assert "weather" in result.message.lower()


@respx.mock
async def test_weather_reports_an_unresolvable_city_as_invalid(settings, tool_records):
    respx.get(url__startswith=settings.open_meteo_geocoding_url).mock(
        return_value=httpx.Response(200, json={})
    )

    result = await get_weather_forecast("Atlantis", settings=settings)

    assert result.status is ToolStatus.INVALID
    assert "Atlantis" in result.message


@respx.mock
async def test_weather_translates_wmo_codes_into_words(settings, tool_records):
    respx.get(url__startswith=settings.open_meteo_geocoding_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "Kyoto",
                        "country": "Japan",
                        "latitude": 35.0,
                        "longitude": 135.76,
                        "timezone": "Asia/Tokyo",
                    }
                ]
            },
        )
    )
    respx.get(url__startswith=settings.open_meteo_base_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "daily": {
                    "time": ["2026-08-01", "2026-08-02"],
                    "weather_code": [61, 0],
                    "temperature_2m_max": [28.0, 31.0],
                    "temperature_2m_min": [21.0, 23.0],
                    "precipitation_sum": [5.2, 0.0],
                    "precipitation_probability_max": [80, 5],
                    "wind_speed_10m_max": [12.0, 8.0],
                    "sunrise": ["2026-08-01T05:12", "2026-08-02T05:13"],
                    "sunset": ["2026-08-01T18:55", "2026-08-02T18:54"],
                }
            },
        )
    )

    from datetime import date, timedelta

    start = (date.today() + timedelta(days=3)).isoformat()
    end = (date.today() + timedelta(days=4)).isoformat()
    result = await get_weather_forecast("Kyoto", start, end, settings=settings)

    assert result.status is ToolStatus.OK
    days = result.data["days"]
    assert days[0]["conditions"] == "slight rain"
    assert days[1]["conditions"] == "clear sky"


@respx.mock
async def test_weather_beyond_the_horizon_is_labelled_not_a_forecast(settings, tool_records):
    """Presenting climate normals as a forecast would be a quiet lie."""
    respx.get(url__startswith=settings.open_meteo_geocoding_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "Kyoto",
                        "country": "Japan",
                        "latitude": 35.0,
                        "longitude": 135.76,
                        "timezone": "Asia/Tokyo",
                    }
                ]
            },
        )
    )
    respx.get(url__startswith="https://archive-api.open-meteo.com").mock(
        return_value=httpx.Response(
            200,
            json={
                "daily": {
                    "temperature_2m_max": [30.0],
                    "temperature_2m_min": [22.0],
                    "precipitation_sum": [3.0],
                }
            },
        )
    )

    from datetime import date, timedelta

    far = (date.today() + timedelta(days=90)).isoformat()
    result = await get_weather_forecast("Kyoto", far, far, settings=settings)

    assert result.data["forecast_type"] == "climate_normals"
    assert "NOT a forecast" in result.message


# ---------------------------------------------------------------------------
# Places
# ---------------------------------------------------------------------------


def test_category_aliases_translate_to_provider_vocabulary():
    resolved, unknown = _resolve_categories(["restaurants", "museums"])
    assert "catering.restaurant" in resolved
    assert "entertainment.museum" in resolved
    assert unknown == []


def test_unknown_categories_are_reported_not_silently_dropped():
    _, unknown = _resolve_categories(["restaurants", "teleportation_pads"])
    assert unknown == ["teleportation_pads"]


def test_constraint_aliases_map_to_provider_conditions():
    resolved, unknown = _resolve_conditions(["vegetarian", "wheelchair_accessible"])
    assert "vegetarian" in resolved
    assert "wheelchair" in resolved
    assert unknown == []


def test_duplicate_osm_venues_are_collapsed():
    """OSM stores many venues as both a node and a polygon a few metres apart."""
    features = [
        {"properties": {"name": "Kiyomizu-dera", "lat": 34.9949, "lon": 135.7850}},
        {"properties": {"name": "kiyomizu-dera", "lat": 34.99491, "lon": 135.78501}},
        {"properties": {"name": "Yasaka Shrine", "lat": 35.0036, "lon": 135.7785}},
        {"properties": {"name": "", "lat": 35.0, "lon": 135.0}},
    ]

    unique = _deduplicate(features)

    assert len(unique) == 2
    assert {f["properties"]["name"] for f in unique} == {"Kiyomizu-dera", "Yasaka Shrine"}


# ---------------------------------------------------------------------------
# Flights and accommodation
# ---------------------------------------------------------------------------


async def test_flight_results_are_labelled_as_simulated(settings, tool_records):
    """Presenting mock fares as bookable is the failure this guards against."""
    result = await search_flights("Singapore", "Tokyo", "2026-09-15", settings=settings)

    assert result.status is ToolStatus.OK
    assert "SIMULATED" in result.data["data_disclaimer"]
    assert "SIMULATED" in result.message


async def test_flight_prices_scale_with_distance(settings, tool_records):
    short = await search_flights("Delhi", "Goa", "2026-09-15", settings=settings)
    long_haul = await search_flights("London", "Sydney", "2026-09-15", settings=settings)

    cheapest_short = short.data["offers"][0]["price_total_usd"]
    cheapest_long = long_haul.data["offers"][0]["price_total_usd"]

    assert cheapest_long > cheapest_short * 2
    assert long_haul.data["route"]["distance_km"] > short.data["route"]["distance_km"]


async def test_flight_results_are_deterministic(settings, tool_records):
    """Reproducibility matters for demos and for asserting on values in tests."""
    first = await search_flights("Singapore", "Tokyo", "2026-09-15", settings=settings)
    second = await search_flights("Singapore", "Tokyo", "2026-09-15", settings=settings)

    assert first.data["offers"] == second.data["offers"]


async def test_malformed_date_is_reported_as_invalid(settings, tool_records):
    result = await search_flights("Singapore", "Tokyo", "15/09/2026", settings=settings)

    assert result.status is ToolStatus.INVALID
    assert "YYYY-MM-DD" in result.message


async def test_reversed_dates_are_rejected(settings, tool_records):
    result = await search_accommodation("Tokyo", "2026-09-17", "2026-09-15", settings=settings)

    assert result.status is ToolStatus.INVALID


async def test_accommodation_budget_filter_is_applied(settings, tool_records):
    unfiltered = await search_accommodation("Tokyo", "2026-09-15", "2026-09-17", settings=settings)
    filtered = await search_accommodation(
        "Tokyo", "2026-09-15", "2026-09-17", max_price_per_night=90, settings=settings
    )

    assert len(filtered.data["properties"]) < len(unfiltered.data["properties"])
    assert all(prop["price_per_night_usd"] <= 90 for prop in filtered.data["properties"])


# ---------------------------------------------------------------------------
# Tool selection is the model's, not the code's
# ---------------------------------------------------------------------------


def test_all_tools_are_registered():
    names = get_tool_names()
    expected = {
        "search_travel_guide",
        "find_places",
        "get_weather_forecast",
        "search_web",
        "search_flights",
        "search_accommodation",
        "recall_user_preferences",
        "save_user_preference",
    }
    assert expected.issubset(set(names))
    assert len(names) >= 3, "the brief requires at least three tools"


def test_every_tool_has_a_description_the_model_can_act_on():
    for tool in ALL_TOOLS:
        assert tool.description, f"{tool.name} has no description"
        assert len(tool.description) > 80, (
            f"{tool.name}'s description is too thin to drive tool selection"
        )


def test_tool_schemas_never_expose_identity_or_injected_services():
    """A model-suppliable user_id would be a data-isolation hole."""
    forbidden = {"user_id", "settings", "session_id", "memory_service", "retriever", "db"}

    for tool in ALL_TOOLS:
        parameters = set(tool.args_schema.model_json_schema().get("properties", {}))
        leaked = parameters & forbidden
        assert not leaked, f"{tool.name} exposes {leaked} to the model"


def test_memory_tools_can_be_withheld_for_opted_out_users():
    """Removing the tools beats instructing the model not to use them."""
    names = {tool.name for tool in get_tools(include_memory=False)}

    assert "recall_user_preferences" not in names
    assert "save_user_preference" not in names
    assert "search_travel_guide" in names


def test_switching_off_a_service_removes_its_tool():
    """A composer toggle is a guarantee, not a suggestion: the tool is gone."""
    names = {tool.name for tool in get_tools(focus=["attractions", "restaurants"])}

    assert "search_flights" not in names
    assert "search_accommodation" not in names
    assert "search_travel_guide" in names
    assert "find_places" in names


def test_no_focus_means_the_full_toolbox():
    assert {t.name for t in get_tools(focus=None)} == {t.name for t in get_tools()}


def test_shared_tools_survive_attraction_and_restaurant_toggles():
    """Attraction/restaurant toggles must not amputate shared tools.

    Those services share find_places with everything else, so their scoping
    is enforced in prompts instead of by tool removal.
    """
    names = {tool.name for tool in get_tools(focus=["flights", "stays"])}

    assert "find_places" in names
    assert "search_travel_guide" in names
    assert "search_flights" in names


def test_no_hardcoded_keyword_routing_exists():
    """Structural check for the pattern the brief explicitly forbids.

    Scans the agent and tool packages for conditionals that branch on a
    keyword appearing in a message and then dispatch to a specific tool -
    e.g. `if "weather" in message: call_weather()`. Tool choice must be the
    model's alone.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    pattern = re.compile(
        r"if\s+[\"'](?:weather|flight|hotel|restaurant|attraction)[\"']\s+in\s+"
        r"\w*(?:message|query|text|prompt|user_input)",
        re.IGNORECASE,
    )

    offenders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]

    assert not offenders, f"hardcoded keyword routing found in: {offenders}"


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=lambda t: t.name)
def test_tool_arguments_are_fully_typed(tool):
    """An untyped parameter becomes an untyped JSON-schema field the model guesses at."""
    schema = tool.args_schema.model_json_schema()
    for name, spec in schema.get("properties", {}).items():
        assert "type" in spec or "anyOf" in spec or "$ref" in spec, (
            f"{tool.name}.{name} has no type in its schema"
        )
