"""Tests for the three-gear conversation: clarify / advise / plan.

The behaviour under test came from a real transcript: "i want to go to
kerala" produced a complete itinerary for districts the traveller never
chose, without asking how long the trip was. The fix routes that message to
an advisory turn instead - and because the routing is a pure function over
extracted slots (`decide_mode`), every rule in it can be pinned here without
a model call.

Each test name states the conversational situation, not the mechanism, so a
failure reads as "the agent now interrogates a fully-specified traveller"
rather than "expected 'plan', got 'advise'".
"""

from __future__ import annotations

from app.agent.graph import route_after_understand
from app.agent.state import AgentState, initial_state
from app.agent.trip_state import (
    FOCUS_SERVICES,
    decide_mode,
    disabled_services,
    merge_trip_state,
    missing_slots,
    normalise_focus,
    resolve_scoped_service,
    scoped_clarifying_question,
)

# ---------------------------------------------------------------------------
# decide_mode: the routing rules, one scenario each
# ---------------------------------------------------------------------------


def test_destination_only_gets_advice_not_an_itinerary():
    """The Kerala case: a bare destination must not trigger the full pipeline."""
    mode = decide_mode(
        needs_clarification=False,
        destination="Kerala",
        wants_full_plan=False,
        scoped_service="none",
        trip_state={"destination": "Kerala"},
    )
    assert mode == "advise"


def test_no_destination_clarifies():
    mode = decide_mode(
        needs_clarification=False,
        destination=None,
        wants_full_plan=False,
        scoped_service="none",
        trip_state={},
    )
    assert mode == "clarify"


def test_ambiguous_request_clarifies_even_with_a_destination():
    """The understanding step's ambiguity judgement always wins."""
    mode = decide_mode(
        needs_clarification=True,
        destination="Kerala",
        wants_full_plan=False,
        scoped_service="none",
        trip_state={"destination": "Kerala"},
    )
    assert mode == "clarify"


def test_fully_specified_first_message_plans_immediately():
    """'Plan 2 days in Kyoto, March 3-4, vegetarian' - asking anyway is a form."""
    mode = decide_mode(
        needs_clarification=False,
        destination="Kyoto",
        wants_full_plan=False,
        scoped_service="none",
        trip_state={"destination": "Kyoto", "duration_days": 2, "start_date": "2026-03-03"},
    )
    assert mode == "plan"


def test_duration_plus_rough_window_is_specified_enough():
    """'5 days in early October' should plan without exact dates."""
    mode = decide_mode(
        needs_clarification=False,
        destination="Kerala",
        wants_full_plan=False,
        scoped_service="none",
        trip_state={
            "destination": "Kerala",
            "duration_days": 5,
            "travel_window": "early October",
        },
    )
    assert mode == "plan"


def test_duration_without_any_date_signal_keeps_advising():
    """'5 days, mostly backwaters' - propose an outline, ask when and from where."""
    mode = decide_mode(
        needs_clarification=False,
        destination="Kerala",
        wants_full_plan=False,
        scoped_service="none",
        trip_state={"destination": "Kerala", "duration_days": 5},
    )
    assert mode == "advise"


def test_wants_full_plan_skips_the_date_signal_requirement():
    """'Plan me 2 relaxed days in Kyoto' has a duration but no date signal.

    decide_mode itself must not additionally require a date signal once
    wants_full_plan is true - that flag alone is the override. The harder
    problem, whether the model actually SETS wants_full_plan for an
    imperative like this, is a prompt-quality question verified live
    (docs/WORKFLOW.md's worked example), not something this unit test can
    check without an LLM call - but the routing rule itself is pinned here.
    """
    mode = decide_mode(
        needs_clarification=False,
        destination="Kyoto",
        wants_full_plan=True,
        scoped_service="none",
        trip_state={"destination": "Kyoto", "duration_days": 2},
    )
    assert mode == "plan"


def test_just_plan_it_overrides_everything():
    """An explicit request is always honoured, however little is known."""
    mode = decide_mode(
        needs_clarification=False,
        destination="Kerala",
        wants_full_plan=True,
        scoped_service="none",
        trip_state={"destination": "Kerala"},
    )
    assert mode == "plan"


def test_confirmation_is_sticky_so_edits_refine_rather_than_readvise():
    """After a full plan exists, 'make day 2 lighter' must not restart advice."""
    mode = decide_mode(
        needs_clarification=False,
        destination="Kerala",
        wants_full_plan=False,
        scoped_service="none",
        trip_state={"destination": "Kerala", "outline_confirmed": True},
    )
    assert mode == "plan"


def test_a_scoped_service_request_skips_the_vibe_conversation():
    """'flights to tokyo in november, from delhi' gets flights, not lifestyle questions."""
    mode = decide_mode(
        needs_clarification=False,
        destination="Tokyo",
        wants_full_plan=False,
        scoped_service="flights",
        trip_state={"destination": "Tokyo", "origin": "Delhi"},
    )
    assert mode == "plan"


def test_a_flight_request_with_no_origin_asks_instead_of_guessing():
    """The real bug: a flight search with no departure city must ask, not guess.

    'find me flights to london' with no origin must not run a plan with a
    blank departure city - that produced a fabricated 'if you're departing
    from New York' example in a live run.
    """
    mode = decide_mode(
        needs_clarification=False,
        destination="London",
        wants_full_plan=False,
        scoped_service="flights",
        trip_state={"destination": "London"},
    )
    assert mode == "clarify"


def test_a_stay_request_needs_no_origin():
    """A hotel search must not be blocked the way a flight search is.

    Only flights require a departure city - a hotel search resolves fully
    from the destination alone.
    """
    mode = decide_mode(
        needs_clarification=False,
        destination="Kochi",
        wants_full_plan=False,
        scoped_service="stays",
        trip_state={"destination": "Kochi"},
    )
    assert mode == "plan"


def test_the_advisory_budget_ends_the_interrogation():
    """After two advisory rounds the agent plans with assumptions instead."""
    mode = decide_mode(
        needs_clarification=False,
        destination="Kerala",
        wants_full_plan=False,
        scoped_service="none",
        trip_state={"destination": "Kerala", "advise_rounds": 2},
    )
    assert mode == "plan"


# ---------------------------------------------------------------------------
# Graph routing honours the mode
# ---------------------------------------------------------------------------


def test_advise_mode_routes_to_the_advisor_node():
    state = AgentState(mode="advise", needs_clarification=False)
    assert route_after_understand(state) == "advise"


def test_clarification_still_beats_advise_routing():
    state = AgentState(mode="advise", needs_clarification=True, clarifying_question="Which city?")
    assert route_after_understand(state) == "respond"


def test_states_without_a_mode_still_plan():
    """Hand-built states (tests, degraded paths) must keep the old behaviour."""
    state = AgentState(needs_clarification=False)
    assert route_after_understand(state) == "plan"


# ---------------------------------------------------------------------------
# Trip-state merging: absence never erases
# ---------------------------------------------------------------------------


def test_follow_ups_keep_slots_they_do_not_mention():
    """'make day 2 lighter' mentions nothing; the trip must survive it."""
    established = {"destination": "Kerala", "duration_days": 5, "priorities": ["backwaters"]}
    merged = merge_trip_state(
        established,
        {"destination": None, "duration_days": None, "priorities": [], "origin": None},
    )
    assert merged == established


def test_new_information_wins_over_old():
    merged = merge_trip_state({"duration_days": 5}, {"duration_days": 7})
    assert merged["duration_days"] == 7


def test_merge_does_not_mutate_its_inputs():
    existing = {"duration_days": 5}
    merge_trip_state(existing, {"duration_days": 7})
    assert existing["duration_days"] == 5


def test_initial_state_carries_the_persisted_ledger():
    state = initial_state(
        user_id="u",
        session_id="s",
        run_id="r",
        messages=[],
        trip_state={"destination": "Kerala", "duration_days": 5},
        focus=["attractions"],
    )
    assert state["trip_state"]["duration_days"] == 5
    assert state["mode"] == "plan"
    assert state["focus"] == ["attractions"]


# ---------------------------------------------------------------------------
# Carrying a scoped ask across the turn that completes it
# ---------------------------------------------------------------------------


def test_a_bare_follow_up_carries_the_pending_service_forward():
    """'from delhi' alone must still resolve to 'flights', not 'none'."""
    resolved = resolve_scoped_service(
        "none", {"destination": "London", "pending_scoped_service": "flights"}
    )
    assert resolved == "flights"


def test_an_explicit_service_this_turn_always_wins():
    resolved = resolve_scoped_service("stays", {"pending_scoped_service": "flights"})
    assert resolved == "stays"


def test_nothing_pending_resolves_to_none():
    assert resolve_scoped_service("none", {}) == "none"


def test_scoped_fallback_question_is_never_empty_for_flights():
    assert scoped_clarifying_question("flights")


# ---------------------------------------------------------------------------
# Focus selection
# ---------------------------------------------------------------------------


def test_no_selection_means_everything_on():
    assert normalise_focus(None) == list(FOCUS_SERVICES)
    assert normalise_focus([]) == list(FOCUS_SERVICES)


def test_deselecting_everything_means_no_scoping_not_refusal():
    """A client sending only junk names almost certainly meant 'no scoping'."""
    assert normalise_focus(["bogus"]) == list(FOCUS_SERVICES)


def test_disabled_services_are_the_complement_of_the_selection():
    assert disabled_services(["flights", "attractions"]) == ["stays", "restaurants"]
    assert disabled_services(None) == []


# ---------------------------------------------------------------------------
# Question ranking
# ---------------------------------------------------------------------------


def test_questions_are_ranked_by_the_cost_of_guessing_wrong():
    """Trip length first - it changes the entire shape of the plan."""
    asked = missing_slots({"destination": "Kerala"})
    assert asked[0].startswith("how many days")


def test_a_rough_window_suppresses_the_timing_question():
    asked = missing_slots(
        {"destination": "Kerala", "travel_window": "early October", "duration_days": 5}
    )
    assert not any("when they are travelling" in slot for slot in asked)


def test_a_fully_specified_trip_has_nothing_left_to_ask():
    asked = missing_slots(
        {
            "duration_days": 5,
            "priorities": ["backwaters"],
            "start_date": "2026-10-02",
            "origin": "Delhi",
        }
    )
    assert asked == []


# ---------------------------------------------------------------------------
# The advisor node itself (no network: LLM stubbed or failing)
# ---------------------------------------------------------------------------


class _FakeRetriever:
    """Returns a canned one-hop result, standing in for Wikivoyage."""

    async def retrieve(
        self, query, destination, *, constraints=None, intent="general", max_hops=None
    ):
        from app.rag.retriever import Hop, RetrievalResult

        assert max_hops == 1, "the advisor must stay within a one-hop budget"
        result = RetrievalResult(districts_considered=["Alappuzha", "Munnar", "Kochi"])
        result.hops.append(Hop(number=1, name="orient", query=query, derived_from="user request"))
        return result


async def test_advisor_survives_an_llm_failure_with_a_question(monkeypatch, settings):
    """Even with every key exhausted, an advisory turn must stay conversational."""
    from app.agent.nodes import advisor
    from app.core.errors import ExternalServiceError

    async def failing_call_model(*args, **kwargs):
        raise ExternalServiceError("all keys cooling", service="groq")

    monkeypatch.setattr(advisor, "call_model", failing_call_model)

    state = AgentState(
        destination="Kerala",
        goal="Visit Kerala",
        trip_state={"destination": "Kerala"},
        constraints=[],
        detected_language="en",
    )
    update = await advisor.advisor_node(state, retriever=_FakeRetriever(), settings=settings)

    assert update["final_response"], "the fallback must still say something"
    assert "how many days" in update["final_response"], (
        "the fallback should ask the highest-value missing slot"
    )
    assert update["trip_state"]["advise_rounds"] == 1
    assert update["status"] == "completed"


async def test_advisor_grounds_its_options_in_the_retrieved_districts(monkeypatch, settings):
    """The prompt must carry real district names, not the model's impression."""
    from app.agent.nodes import advisor

    captured = {}

    async def capturing_call_model(role, messages, **kwargs):
        captured["prompt"] = "\n".join(str(m.content) for m in messages)

        class _Reply:
            content = "🌴 Backwaters - Alappuzha ..."

        return _Reply()

    monkeypatch.setattr(advisor, "call_model", capturing_call_model)

    state = AgentState(
        destination="Kerala",
        goal="Visit Kerala",
        trip_state={"destination": "Kerala", "duration_days": 5},
        constraints=["vegetarian"],
        detected_language="en",
    )
    update = await advisor.advisor_node(state, retriever=_FakeRetriever(), settings=settings)

    assert "Alappuzha" in captured["prompt"], "retrieved districts never reached the prompt"
    assert "vegetarian" in captured["prompt"], "known constraints must be marked as known"
    assert "trip length (days): 5" in captured["prompt"]
    assert update["final_response"].startswith("🌴")
    assert update["suggested_options"] == ["Alappuzha", "Munnar", "Kochi"], (
        "the frontend needs real, retrieved district names to render as clickable "
        "chips - not names parsed back out of the model's prose"
    )
