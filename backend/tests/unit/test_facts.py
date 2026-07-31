"""Tests for the structured findings the trip panel renders.

The one that matters is provenance. This project ships a mock flight and
hotel provider, so a generated price rendered in the same style as a measured
forecast is the most misleading thing the interface could show. The
`simulated` flag is what stops that, and it must be derived rather than
remembered.
"""

from __future__ import annotations

from app.services.facts import (
    collected_facts,
    record_fact,
    start_facts,
    stop_facts,
)


def test_a_generated_price_is_flagged_as_simulated() -> None:
    """Mock provider output must never look like live availability."""
    start_facts()
    record_fact(
        "search_accommodation",
        "flight-provider",
        {"source": "mock", "properties": [{"name": "Lucknow 3-star hotel"}]},
    )

    assert collected_facts()["stays"]["simulated"] is True
    stop_facts()


def test_a_real_forecast_is_not_flagged() -> None:
    """Open-Meteo is live, and labelling it simulated would be its own lie."""
    start_facts()
    record_fact("get_weather_forecast", "open-meteo", {"location": "Lucknow", "days": []})

    fact = collected_facts()["weather"]
    assert fact["simulated"] is False
    assert fact["source"] == "open-meteo"
    stop_facts()


def test_the_flag_follows_the_payload_not_a_hardcoded_list() -> None:
    """Swapping in a real provider must flip the label with no other change.

    The payload states its own source, so a provider that starts returning
    live data stops being labelled simulated without anyone remembering to
    update a constant here. Same tool, same source name, different payload.
    """
    start_facts()
    record_fact("search_flights", "flight-provider", {"source": "duffel", "offers": []})

    assert collected_facts()["flights"]["simulated"] is False
    stop_facts()


def test_only_renderable_tools_reach_a_panel() -> None:
    """A guide article is prose and belongs in the reply, not in a panel."""
    start_facts()
    record_fact("search_travel_guide", "wikivoyage", {"chunks": ["..."]})
    record_fact("search_web", "tavily", {"results": []})

    assert collected_facts() == {}
    stop_facts()


def test_a_later_call_supersedes_an_earlier_one() -> None:
    """A corrected search replaces its predecessor rather than joining it.

    Showing both would leave the traveller comparing a result against a
    question they had already refined.
    """
    start_facts()
    record_fact("search_accommodation", "mock", {"source": "mock", "budget_filter_usd": 50})
    record_fact("search_accommodation", "mock", {"source": "mock", "budget_filter_usd": 200})

    assert collected_facts()["stays"]["data"]["budget_filter_usd"] == 200
    stop_facts()


def test_recording_without_a_sheet_is_a_no_op() -> None:
    """Tools run outside a turn - in tests, in scripts - and must not fail."""
    stop_facts()
    record_fact("get_weather_forecast", "open-meteo", {"days": []})

    assert collected_facts() == {}
