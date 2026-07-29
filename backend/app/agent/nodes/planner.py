"""Planner node: decompose a goal into ordered steps.

This is the node the assignment's "Planning & Reasoning" requirement is really
about, and the one LangChain no longer provides out of the box - the prebuilt
Plan-and-Execute agent was removed in the 1.0 restructuring, and `create_agent`
replaced it with a ReAct loop that does not plan ahead.

Planning up front rather than reacting step by step buys three things here:

* **Tool budget.** A ReAct agent decides its next call after seeing the last
  result, which on a rate-limited free tier means an unpredictable number of
  model round trips. A plan bounds the work before any of it starts.
* **Inspectability.** A stored plan is something a reviewer can read and
  disagree with. "It called some tools and produced text" is not.
* **Recovery.** When a step fails, the replanner has an explicit structure to
  revise. Without a plan there is nothing to revise - only a transcript.

The prompt spends most of its length on what *not* to do, because the
characteristic failure of LLM planners is over-decomposition: eleven steps
where three would do, each costing a model call. The step ceiling is enforced
in code as well, since models routinely ignore a stated limit.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.agent.state import AgentState, PlanStep
from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.services.llm import ModelRole, structured_call
from app.tools.registry import get_tool_names

logger = get_logger(__name__)


class Plan(BaseModel):
    """An ordered plan for satisfying the traveller's goal."""

    steps: list[PlanStep] = Field(
        description=(
            "Between 2 and 5 steps. Each must be independently actionable and earn "
            "its place - if two steps would be served by one tool call, make them "
            "one step."
        )
    )
    reasoning: str = Field(
        default="", max_length=300, description="One or two sentences on the approach."
    )


PLANNER_PROMPT = """\
You plan how a travel assistant will answer a traveller's request. You do not \
answer it yourself and you do not call tools - you decide what work needs \
doing, in what order.

TOOLS THE EXECUTOR CAN USE
{tools}

HOW TO PLAN WELL
- Two to four steps for most requests. Five is the absolute maximum.
- Each step should correspond to roughly one or two tool calls. If a step \
  would need six, split it. If two steps would use the same tool with almost \
  the same arguments, merge them.
- Order matters. Research the destination before looking for specific venues, \
  because what you learn first tells you where to look next.
- The final step is always composing the answer, and it needs no tools.

WHAT NOT TO DO
- Do not create a step per tool. 'Check weather', 'find restaurants', \
  'find museums', 'find hotels' is four model round trips where one research \
  step and one logistics step would do.
- Do not plan to ask the traveller anything. That decision was already made \
  before you were called.
- Do not include steps for information you were already given. If the \
  traveller's dietary needs are listed below as known, there is no step for \
  discovering them - the executor simply uses them.
- Do not plan speculative work. A 2-day itinerary does not need a step for \
  'research day trips in case they want one'.

A short plan that answers the question beats a thorough plan that exhausts \
the request budget before finishing.\
"""


async def planner_node(state: AgentState, *, settings: Settings | None = None) -> AgentState:
    """Produce an ordered plan for the goal.

    Args:
        state: Current graph state. Requires `goal`.
        settings: Settings override, for tests.

    Returns:
        A partial state update carrying the plan.
    """
    cfg = settings or get_settings()

    context_lines = [f"GOAL: {state.get('goal', '')}"]
    if state.get("destination"):
        context_lines.append(f"DESTINATION: {state['destination']}")
    if state.get("start_date"):
        context_lines.append(
            f"DATES: {state['start_date']} to {state.get('end_date') or state['start_date']}"
        )
    if state.get("constraints"):
        context_lines.append("HARD REQUIREMENTS: " + "; ".join(state["constraints"]))
    if state.get("memory_block"):
        context_lines.append("\n" + state["memory_block"])

    try:
        plan = await structured_call(
            ModelRole.PLANNER,
            [
                SystemMessage(content=PLANNER_PROMPT.format(tools=_tool_summary())),
                HumanMessage(content="\n".join(context_lines)),
            ],
            Plan,
            purpose="create_plan",
            settings=cfg,
        )
    except ExternalServiceError as exc:
        logger.warning("agent.planning_failed", error=exc.message)
        # A single research-and-answer step is a poor plan but a working one,
        # and it beats failing the request outright.
        fallback = [
            PlanStep(
                description=f"Research and answer: {state.get('goal', 'the request')}",
                kind="research",
            ),
            PlanStep(description="Compose the final answer", kind="compose",
                     depends_on_previous=True),
        ]
        return AgentState(
            plan=fallback,
            initial_plan=fallback,
            current_step_index=0,
            errors=[{"node": "planner", "error": exc.message}],
        )

    # Enforced in code because models treat stated ceilings as advisory.
    steps = plan.steps[: cfg.agent_max_plan_steps]

    if not steps:
        steps = [PlanStep(description=state.get("goal", "Answer the request"), kind="research")]

    logger.info(
        "agent.planned",
        steps=len(steps),
        kinds=[step.kind for step in steps],
        reasoning=plan.reasoning[:120],
    )

    return AgentState(
        plan=steps,
        initial_plan=list(steps),
        current_step_index=0,
    )


def _tool_summary() -> str:
    """Render the tool list for the planner prompt.

    Only names and one-line purposes: the planner chooses *what work to do*,
    while the executor chooses *which tool*. Giving the planner full tool
    schemas invites it to write steps that name specific tools and arguments,
    which removes the executor's ability to adapt when one is unavailable.

    Returns:
        A formatted list of tool names with brief purposes.
    """
    purposes = {
        "search_travel_guide": "deep research on a destination's districts, sights and food",
        "find_places": "specific mapped venues, filterable by dietary or access needs",
        "get_weather_forecast": "forecast for a city and dates",
        "search_web": "current events, prices, closures",
        "search_flights": "flights between cities",
        "search_accommodation": "places to stay",
        "recall_user_preferences": "look up stored traveller preferences",
        "save_user_preference": "remember a durable fact about the traveller",
    }
    return "\n".join(
        f"- {name}: {purposes.get(name, 'no description')}" for name in get_tool_names()
    )
