"""Tests for running independent plan steps concurrently.

`PlanStep.depends_on_previous` existed from the first version and nothing ever
read it, so every plan ran strictly one step at a time - five sequential round
trips for a five-step plan, even where researching attractions, checking the
weather and finding stays needed nothing from one another.

Two things are pinned here: which steps are allowed to run together, and that
running them together does not corrupt the trace. The second matters as much
as the first - the admin trace attributes tool calls to steps, and concurrent
steps interleaving would silently file "checked the weather" under whichever
step happened to be measured around it.
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent.nodes import executor
from app.agent.nodes.executor import _batch_from, executor_node
from app.agent.state import AgentState, PlanStep
from app.tools.base import ToolCallRecord, ToolStatus, start_recording, stop_recording


def _step(description: str, *, kind: str = "research", independent: bool = False) -> PlanStep:
    """Build a plan step.

    Args:
        description: Step text.
        kind: Step kind.
        independent: True to mark it as not needing the previous step.

    Returns:
        The step.
    """
    return PlanStep(description=description, kind=kind, depends_on_previous=not independent)


def test_a_dependent_step_runs_alone() -> None:
    """The default is sequential, because the default is `depends_on_previous`."""
    plan = [_step("Research Kyoto"), _step("Pick districts from that research")]

    assert _batch_from(plan, 0, limit=3) == plan[:1]


def test_independent_steps_run_together() -> None:
    """The case this exists for: three lookups that need nothing from each other."""
    plan = [
        _step("Research Kyoto districts"),
        _step("Check the weather for the dates", independent=True),
        _step("Find places to stay", kind="logistics", independent=True),
    ]

    assert len(_batch_from(plan, 0, limit=3)) == 3


def test_the_first_dependent_step_ends_the_batch() -> None:
    """A batch stops at the first step that needs what came before it.

    Not "collect every independent step in the plan" - a later independent
    step may still sit *after* a dependent one, and hoisting it past that
    would reorder the plan rather than parallelise it.
    """
    plan = [
        _step("Research Kyoto"),
        _step("Check the weather", independent=True),
        _step("Choose districts from the research"),
        _step("Find restaurants", independent=True),
    ]

    batch = _batch_from(plan, 0, limit=8)

    assert [s.description for s in batch] == ["Research Kyoto", "Check the weather"]


def test_compose_never_joins_a_batch() -> None:
    """Composing needs every finding, so it cannot run beside one.

    Guarded explicitly rather than trusted to the model's flag: a planner that
    marked the compose step independent would produce an answer written from
    findings that had not arrived yet, which reads as a plausible but hollow
    itinerary rather than an obvious failure.
    """
    plan = [
        _step("Research Kyoto"),
        _step("Compose the final answer", kind="compose", independent=True),
    ]

    assert len(_batch_from(plan, 0, limit=3)) == 1


def test_the_batch_respects_its_limit() -> None:
    """Concurrency is capped; an eight-step plan must not open eight loops."""
    plan = [_step("Research")] + [_step(f"Independent {i}", independent=True) for i in range(7)]

    assert len(_batch_from(plan, 0, limit=3)) == 3


@pytest.mark.asyncio
async def test_concurrent_steps_keep_their_own_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each step's trace must list the tools *it* called, not its siblings'.

    The slow step finishes last but its record still belongs to it. Before the
    per-step recorder this was computed by slicing a shared list between two
    lengths, which cannot survive interleaving.
    """
    records = start_recording()

    async def fake_step_agent(prompt, cfg, *, focus=None, kind="research"):
        from app.tools.base import _recorder

        # "weather" is slow and finishes after "guide", so completion order and
        # plan order genuinely differ here.
        slow = "weather" in prompt
        await asyncio.sleep(0.02 if slow else 0.0)
        name = "get_weather_forecast" if slow else "search_travel_guide"
        own = _recorder.get()
        if own is not None:
            own.append(
                ToolCallRecord(tool_name=name, status=ToolStatus.OK, source="test", latency_ms=1)
            )
        return f"findings from {name}"

    monkeypatch.setattr(executor, "_run_step_agent", fake_step_agent)

    state = AgentState(
        plan=[
            _step("research the guide"),
            _step("check the weather", independent=True),
        ],
        current_step_index=0,
        goal="Plan Kyoto",
    )

    update = await executor_node(state, tool_records=records, settings=None)

    completed = update["completed_steps"]
    assert len(completed) == 2, "both steps should have run"
    assert update["current_step_index"] == 2, "the index must advance past the whole batch"

    assert completed[0].tools_used == ["search_travel_guide"]
    assert completed[1].tools_used == ["get_weather_forecast"]

    # Merged in plan order, not completion order - the trace is read by a
    # human looking for a given step, so the slow step stays second.
    assert [r.tool_name for r in records] == [
        "search_travel_guide",
        "get_weather_forecast",
    ]
    stop_recording()


@pytest.mark.asyncio
async def test_one_failing_step_does_not_take_down_its_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch is not all-or-nothing.

    Steps run concurrently, so an exception in one would propagate out of the
    gather and discard the other's completed work - paying for it and throwing
    it away. The replanner can route around a single failed step; it cannot
    recover work that was never returned.
    """
    start_recording()

    async def flaky(prompt, cfg, *, focus=None, kind="research"):
        if "weather" in prompt:
            raise RuntimeError("provider exploded")
        return "guide findings"

    monkeypatch.setattr(executor, "_run_step_agent", flaky)

    state = AgentState(
        plan=[_step("research the guide"), _step("check the weather", independent=True)],
        current_step_index=0,
        goal="Plan Kyoto",
    )

    update = await executor_node(state, tool_records=[], settings=None)

    completed = update["completed_steps"]
    assert completed[0].succeeded is True
    assert completed[0].output == "guide findings"
    assert completed[1].succeeded is False
    assert "provider exploded" in (completed[1].error or "")
    stop_recording()
