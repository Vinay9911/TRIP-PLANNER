"""Regression tests for the per-step tool-call budget.

These pin down a measured failure, not a hypothetical one. Run
372b30e6 - a 10-day nature trip to New York - made **43** `search_accommodation`
calls inside a single step, ran 194s against a 90s timeout and cost 156,870
tokens, because `recursion_limit` bounds LangGraph super-steps and a model
calling tools in parallel puts many calls inside one super-step.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import BaseTool, StructuredTool

from app.agent.nodes.executor import _budgeted_tools

BUDGET = 4


def _counting_tool(calls: list[dict[str, Any]]) -> BaseTool:
    """Build a tool that records every invocation.

    Args:
        calls: List the tool appends its arguments to.

    Returns:
        A tool named `search_accommodation`, matching the failing trace.
    """

    async def run(city: str, check_in: str) -> str:
        calls.append({"city": city, "check_in": check_in})
        return f"3 stays in {city}"

    return StructuredTool.from_function(
        coroutine=run,
        name="search_accommodation",
        description="Find stays.",
    )


@pytest.mark.asyncio
async def test_budget_caps_distinct_calls() -> None:
    """The 43-call runaway must stop at the configured budget."""
    calls: list[dict[str, Any]] = []
    (tool,) = _budgeted_tools([_counting_tool(calls)], budget=BUDGET)

    for index in range(43):
        await tool.ainvoke({"city": f"City {index}", "check_in": "2026-08-08"})

    assert len(calls) == BUDGET


@pytest.mark.asyncio
async def test_over_budget_returns_an_instruction_not_an_error() -> None:
    """A refusal must tell the model what to do instead.

    A bare error string is what makes models retry the same call - the
    behaviour this whole guard exists to stop.
    """
    calls: list[dict[str, Any]] = []
    (tool,) = _budgeted_tools([_counting_tool(calls)], budget=1)

    await tool.ainvoke({"city": "Lake Placid", "check_in": "2026-08-08"})
    refusal = await tool.ainvoke({"city": "Woodstock", "check_in": "2026-08-08"})

    assert "do not call any more tools" in refusal.lower()
    assert "write your findings now" in refusal.lower()


@pytest.mark.asyncio
async def test_identical_calls_are_served_from_the_memo() -> None:
    """One city with identical dates recurred eight times in the trace.

    A repeat costs nothing and must not consume budget: the question was
    already answered, so charging for it would spend the allowance on nothing
    and could starve a genuinely new lookup.
    """
    calls: list[dict[str, Any]] = []
    (tool,) = _budgeted_tools([_counting_tool(calls)], budget=BUDGET)

    first = await tool.ainvoke({"city": "Lake Placid, NY", "check_in": "2026-08-08"})
    assert first == "3 stays in Lake Placid, NY"

    for _ in range(7):
        result = await tool.ainvoke({"city": "Lake Placid, NY", "check_in": "2026-08-08"})
        # The cached payload comes back, so the model still has something to
        # write findings from, carrying a note telling it not to ask again.
        assert "3 stays in Lake Placid, NY" in result
        assert "do not repeat" in result.lower()

    assert len(calls) == 1

    # The budget must be untouched but for the one real call, so three
    # genuinely different lookups still succeed.
    for city in ("Woodstock", "Beacon", "Hunter"):
        await tool.ainvoke({"city": city, "check_in": "2026-08-08"})
    assert len(calls) == BUDGET


@pytest.mark.asyncio
async def test_budget_is_shared_across_the_whole_toolbox() -> None:
    """A step's budget covers the step, not each tool separately.

    Otherwise a four-tool step silently gets four times the allowance, which
    is how a limit that reads as strict becomes one that is not.
    """
    first: list[dict[str, Any]] = []
    second: list[dict[str, Any]] = []
    tool_a, tool_b = _budgeted_tools([_counting_tool(first), _counting_tool(second)], budget=BUDGET)

    # Distinct names, so the memo cannot merge them.
    object.__setattr__(tool_b, "name", "find_places")

    for index in range(10):
        await tool_a.ainvoke({"city": f"A{index}", "check_in": "2026-08-08"})
        await tool_b.ainvoke({"city": f"B{index}", "check_in": "2026-08-08"})

    assert len(first) + len(second) == BUDGET
