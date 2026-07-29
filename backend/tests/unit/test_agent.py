"""Tests for the planning loop.

The loop's correctness is mostly about *termination* and *budgets*, so that is
what these tests target. An agent that produces a slightly worse plan is a
quality problem; an agent that never stops is an outage and a bill.

Routing is written as pure functions of state precisely so it can be tested
this way - no graph, no LLM, no network.
"""

from __future__ import annotations

import pytest

from app.agent.graph import route_after_execute, route_after_understand
from app.agent.nodes.planner import planner_node
from app.agent.nodes.replanner import replanner_node, route_after_replan
from app.agent.nodes.responder import _find_ungrounded_claims, _language_instruction
from app.agent.state import AgentState, PlanStep, StepResult, initial_state
from app.core.errors import ExternalServiceError


def make_step(description: str = "Research the destination", kind: str = "research") -> PlanStep:
    return PlanStep(description=description, kind=kind)


def make_result(*, succeeded: bool = True, kind: str = "research", output: str = "found things"):
    return StepResult(
        step=make_step(kind=kind), succeeded=succeeded, output=output if succeeded else "",
        error=None if succeeded else "tool unavailable",
    )


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_clarification_skips_planning_entirely():
    """A clarifying question must not cost a full plan-execute cycle."""
    state = AgentState(needs_clarification=True, clarifying_question="Which city?")
    assert route_after_understand(state) == "respond"


def test_a_clear_request_proceeds_to_planning():
    state = AgentState(needs_clarification=False, clarifying_question=None)
    assert route_after_understand(state) == "plan"


def test_understanding_failure_still_produces_a_reply():
    state = AgentState(status="failed")
    assert route_after_understand(state) == "respond"


def test_execution_routes_to_replan_while_steps_remain():
    state = AgentState(plan=[make_step(), make_step()], current_step_index=1)
    assert route_after_execute(state) == "replan"


def test_execution_routes_to_respond_once_the_plan_is_done():
    state = AgentState(plan=[make_step()], current_step_index=1)
    assert route_after_execute(state) == "respond"


def test_replan_routing_ends_the_loop_when_the_plan_was_truncated():
    """Truncating the plan is how the replanner says 'finish'."""
    state = AgentState(plan=[], current_step_index=2)
    assert route_after_replan(state) == "respond"


def test_replan_routing_continues_when_steps_remain():
    state = AgentState(plan=[make_step(), make_step(), make_step()], current_step_index=1)
    assert route_after_replan(state) == "execute"


# ---------------------------------------------------------------------------
# Budgets and termination
# ---------------------------------------------------------------------------


async def test_replan_budget_exhaustion_ends_the_run_as_partial(settings):
    """A deterministic failure would otherwise be replanned forever."""
    settings.agent_max_replan_cycles = 2

    state = AgentState(
        plan=[make_step(), make_step(), make_step()],
        current_step_index=1,
        completed_steps=[make_result(succeeded=False)],
        replan_count=2,
    )

    update = await replanner_node(state, settings=settings)

    assert update["status"] == "partial"
    assert update["stopped_because"] == "replan_budget_exhausted"
    assert update["plan"] == state["plan"][:1]
    assert route_after_replan({**state, **update}) == "respond"


async def test_replanner_skips_its_model_call_when_the_last_step_succeeded(
    settings, monkeypatch
):
    """Asking 'should I continue?' after a success costs a request for an obvious answer."""
    from app.agent.nodes import replanner

    called = False

    async def should_not_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("replanner made an unnecessary model call")

    monkeypatch.setattr(replanner, "structured_call", should_not_run)

    state = AgentState(
        plan=[make_step(), make_step(), make_step()],
        current_step_index=1,
        completed_steps=[make_result(succeeded=True)],
        replan_count=0,
    )

    await replanner_node(state, settings=settings)
    assert called is False


async def test_replanner_failure_falls_back_to_continuing(settings, monkeypatch):
    """Losing the supervisor should degrade to 'no supervisor', not to a dead run."""
    from app.agent.nodes import replanner

    async def exploding(*args, **kwargs):
        raise ExternalServiceError("provider down", service="groq")

    monkeypatch.setattr(replanner, "structured_call", exploding)

    state = AgentState(
        plan=[make_step(), make_step()],
        current_step_index=1,
        # A failed verify step forces the model path rather than the cheap exit.
        completed_steps=[make_result(succeeded=False, kind="verify")],
        replan_count=0,
    )

    update = await replanner_node(state, settings=settings)

    assert update["status"] == "running"
    assert update.get("errors")


async def test_planner_enforces_the_step_ceiling_in_code(settings, monkeypatch):
    """Models treat a stated limit as advisory, so it is also enforced here."""
    from app.agent.nodes import planner
    from app.agent.nodes.planner import Plan

    settings.agent_max_plan_steps = 3

    async def over_eager(*args, **kwargs):
        return Plan(steps=[make_step(f"step {i}") for i in range(11)])

    monkeypatch.setattr(planner, "structured_call", over_eager)

    update = await planner_node(AgentState(goal="Plan Kyoto"), settings=settings)

    assert len(update["plan"]) == 3


async def test_planner_failure_produces_a_workable_fallback_plan(settings, monkeypatch):
    from app.agent.nodes import planner

    async def exploding(*args, **kwargs):
        raise ExternalServiceError("provider down", service="groq")

    monkeypatch.setattr(planner, "structured_call", exploding)

    update = await planner_node(AgentState(goal="Plan Kyoto"), settings=settings)

    assert len(update["plan"]) >= 1
    assert update["plan"][-1].kind == "compose"
    assert update.get("errors")


async def test_planner_records_the_initial_plan_separately(settings, monkeypatch):
    """The trace needs the original plan to show that replanning changed it."""
    from app.agent.nodes import planner
    from app.agent.nodes.planner import Plan

    async def fixed(*args, **kwargs):
        return Plan(steps=[make_step("a"), make_step("b")])

    monkeypatch.setattr(planner, "structured_call", fixed)

    update = await planner_node(AgentState(goal="Plan Kyoto"), settings=settings)

    assert [step.description for step in update["initial_plan"]] == ["a", "b"]
    assert update["initial_plan"] is not update["plan"]


def test_recursion_limit_scales_with_configured_budgets():
    """A raised step ceiling must not start tripping LangGraph's guard instead of ours."""
    from app.agent.graph import recursion_limit_for
    from app.core.config import Settings

    small = Settings(_env_file=None, agent_max_plan_steps=3, agent_max_replan_cycles=1)
    large = Settings(_env_file=None, agent_max_plan_steps=12, agent_max_replan_cycles=5)

    assert recursion_limit_for(large) > recursion_limit_for(small)
    assert recursion_limit_for(large) >= (12 + 5) * 2


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def test_initial_state_is_fully_populated():
    state = initial_state(user_id="u", session_id="s", run_id="r", messages=[])

    for key in ("goal", "plan", "completed_steps", "replan_count", "constraints", "status"):
        assert key in state

    assert state["status"] == "running"
    assert state["replan_count"] == 0


def test_step_results_render_failures_visibly_for_the_replanner():
    ok = make_result(succeeded=True, output="Found three temples")
    failed = make_result(succeeded=False)

    assert "[OK]" in ok.as_history_line()
    assert "[FAILED]" in failed.as_history_line()
    assert "tool unavailable" in failed.as_history_line()


# ---------------------------------------------------------------------------
# Responder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected_fragment"),
    [
        ("en", "English"),
        ("ja", "Japanese"),
        ("es", "Spanish"),
        ("ja-JP", "Japanese"),
        ("xx", "'xx'"),
    ],
)
def test_reply_language_instruction_names_the_language(code, expected_fragment):
    assert expected_fragment in _language_instruction(code)


def test_non_english_instruction_asks_for_romanised_place_names():
    """A place name only in local script cannot be typed into a map."""
    instruction = _language_instruction("ja")
    assert "romanis" in instruction.lower()


def test_grounding_check_flags_a_venue_absent_from_the_findings():
    findings = "Kiyomizu Temple is in Higashiyama. Nishiki Market sells local produce."
    answer = "Visit Kiyomizu Temple, then dine at the Fictional Sakura Restaurant."

    ungrounded = _find_ungrounded_claims(answer, findings)

    assert any("Sakura" in claim for claim in ungrounded)
    assert not any("Kiyomizu" in claim for claim in ungrounded)


def test_grounding_check_passes_a_fully_supported_answer():
    findings = "Kiyomizu Temple is a major sight. Yasaka Shrine is nearby."
    answer = "Start at Kiyomizu Temple, then walk to Yasaka Shrine."

    assert _find_ungrounded_claims(answer, findings) == []


def test_grounding_check_flags_invented_prices():
    findings = "Entry details were not available."
    answer = "Entry costs ¥400 per person."

    assert _find_ungrounded_claims(answer, findings) != []
