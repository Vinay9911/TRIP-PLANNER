"""Tests for structured itineraries, follow-up offers and past-date guarding.

All three come from one live transcript. A five-day Geneva plan came back as
five paragraphs of prose; it never mentioned that the agent could also find
flights, a hotel or a restaurant despite having all three tools; and the trip
was planned for a date three weeks in the past, with the only hint a buried
line saying the forecast could not be checked.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.agent.itinerary import Itinerary, ItineraryDay, ItineraryItem
from app.agent.nodes.responder import render_itinerary_text
from app.agent.nodes.understand import _first_past_date
from app.agent.trip_state import suggest_actions

# ---------------------------------------------------------------------------
# Past dates
# ---------------------------------------------------------------------------


def test_a_past_start_date_is_detected():
    """The exact bug: '8 july 2026' asked for on 30 july 2026."""
    yesterday = (date.today() - timedelta(days=22)).isoformat()
    assert _first_past_date(yesterday, None) is not None


def test_future_dates_are_left_alone():
    future = (date.today() + timedelta(days=30)).isoformat()
    assert _first_past_date(future, None) is None


def test_today_is_not_treated_as_past():
    """Someone planning same-day travel is not making a typo."""
    assert _first_past_date(date.today().isoformat(), None) is None


def test_malformed_dates_are_not_this_check_s_problem():
    """A bad date string is the parser's business, not the calendar guard's."""
    assert _first_past_date("early October", "not a date") is None


def test_a_past_end_date_is_caught_even_when_start_is_absent():
    past = (date.today() - timedelta(days=3)).isoformat()
    assert _first_past_date(None, past) is not None


# ---------------------------------------------------------------------------
# Follow-up offers
# ---------------------------------------------------------------------------


def test_offers_the_services_that_have_not_run():
    """The Geneva failure: a full plan that never mentioned flights or hotels."""
    actions = suggest_actions(
        trip_state={"origin": "Delhi"},
        focus=None,
        tools_used={"search_travel_guide"},
        destination="Geneva",
    )
    labels = " ".join(action["label"] for action in actions)
    assert "Flights" in labels
    assert "stay" in labels.lower()


def test_never_offers_a_service_the_traveller_switched_off():
    actions = suggest_actions(
        trip_state={"origin": "Delhi"},
        focus=["attractions"],
        tools_used=set(),
        destination="Geneva",
    )
    labels = " ".join(action["label"] for action in actions).lower()
    assert "flight" not in labels
    assert "stay" not in labels


def test_never_offers_something_it_just_did():
    actions = suggest_actions(
        trip_state={"origin": "Delhi"},
        focus=None,
        tools_used={"search_flights", "search_accommodation"},
        destination="Geneva",
    )
    labels = " ".join(action["label"] for action in actions).lower()
    assert "flight" not in labels
    assert "stay" not in labels


def test_uses_the_known_origin_in_the_offer():
    actions = suggest_actions(
        trip_state={"origin": "Delhi"}, focus=["flights"], tools_used=set(), destination="Geneva"
    )
    assert actions[0]["label"] == "Flights from Delhi"
    assert "Delhi" in actions[0]["message"]


def test_no_destination_means_no_offers():
    """Offering "where to stay" before a destination exists is noise."""
    assert suggest_actions(trip_state={}, focus=None, tools_used=set(), destination=None) == []


def test_offers_are_capped():
    """A row of chips stops reading as help and starts reading as a menu."""
    actions = suggest_actions(trip_state={}, focus=None, tools_used=set(), destination="Geneva")
    assert len(actions) <= 3


def test_every_offer_carries_a_sendable_message():
    for action in suggest_actions(
        trip_state={}, focus=None, tools_used=set(), destination="Geneva"
    ):
        assert action["label"] and action["message"]


# ---------------------------------------------------------------------------
# Rendering structured days back to text
# ---------------------------------------------------------------------------


def _sample() -> Itinerary:
    return Itinerary(
        destination="Geneva",
        intro="Five days by the lake.",
        days=[
            ItineraryDay(
                day_number=1,
                title="Old Town and the lake",
                date="2027-07-08",
                summary="Start high, end by the water.",
                morning=[
                    ItineraryItem(
                        name="St. Pierre Cathedral",
                        kind="sight",
                        district="Old Town",
                        description="Climb the tower for the rooftop view.",
                        latitude=46.2011,
                        longitude=6.1478,
                    )
                ],
                evening=[ItineraryItem(name="Bains des Pâquis", kind="food", district="Pâquis")],
            )
        ],
        practical_notes=["Your hotel gives you a free transport card."],
        gaps=["I couldn't check the forecast that far ahead."],
    )


def test_rendered_text_contains_the_plan():
    """The markdown is what gets stored as the message and read back later.

    If it did not carry the plan, reopening a past conversation would show an
    empty assistant bubble next to a perfectly good itinerary.
    """
    text = render_itinerary_text(_sample())

    assert "Day 1: Old Town and the lake" in text
    assert "St. Pierre Cathedral" in text
    assert "Bains des Pâquis" in text
    assert "free transport card" in text
    assert "couldn't check the forecast" in text


def test_rendering_is_deterministic():
    """No model call, so the cards and the text can never disagree."""
    assert render_itinerary_text(_sample()) == render_itinerary_text(_sample())


def test_all_items_walks_every_slot():
    assert len(_sample().all_items()) == 2


def test_an_empty_day_renders_without_blank_slot_headings():
    itinerary = Itinerary(
        destination="Geneva",
        days=[ItineraryDay(day_number=1, title="Arrival", summary="Just settling in.")],
    )
    text = render_itinerary_text(itinerary)

    assert "Morning" not in text
    assert "Just settling in." in text
