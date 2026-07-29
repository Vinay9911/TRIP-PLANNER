"""Replanner node: decide whether to continue, revise, or answer.

This is the node that makes the loop a loop. After each step it looks at
everything done so far and picks one of three outcomes:

    CONTINUE  The plan is still sound. Run the next step.
    REVISE    Something changed - a step failed, a tool was unavailable, or a
              result made a later step pointless. Replace the *remaining*
              steps and carry on.
    FINISH    Enough evidence exists to answer, even if steps remain.

FINISH deserves the most emphasis, because it is the outcome that saves the
most work and the one a naive implementation never reaches. Plans are written
before any evidence exists, so they routinely over-specify: a planner allocates
a separate step for weather that the research step already covered. Letting the
replanner cut the plan short when the question is already answered is what
stops the agent grinding through steps whose output nobody will read.

**Revision replaces only the unexecuted tail.** Completed steps are history -
rewriting them would corrupt the trace and could cause work to be repeated.

**The replan budget is a hard ceiling.** Two failure modes make this
non-negotiable: a step that fails deterministically will fail identically
after any amount of replanning, and a model asked "is this good enough?" can
always find something else to check. When the budget is exhausted the run is
marked partial and answered with whatever was gathered - a partial answer that
arrives beats a perfect one that never does.
"""

from __future__ import annotations

from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.agent.state import AgentState, PlanStep
from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.services.llm import ModelRole, structured_call

logger = get_logger(__name__)

ReplanDecision = Literal["continue", "revise", "finish"]


class Replan(BaseModel):
    """The replanner's verdict on how to proceed."""

    decision: ReplanDecision = Field(
        description=(
            "continue = the remaining plan is still right, run the next step. "
            "revise = replace the remaining steps because something changed. "
            "finish = enough has been gathered to answer now, stop planning."
        )
    )
    revised_steps: list[PlanStep] = Field(
        default_factory=list,
        description=(
            "Only when decision is 'revise'. The new REMAINING steps - do not "
            "repeat steps already completed. Keep it to three or fewer."
        ),
    )
    reasoning: str = Field(
        default="", max_length=300, description="One or two sentences justifying the decision."
    )


REPLANNER_PROMPT = """\
You supervise a travel planning agent partway through its plan. Given what \
has been done and what remains, decide what happens next.

CHOOSE finish WHEN
- The findings so far can answer the traveller's goal properly. Remaining \
  steps that would only add polish are not worth the wait.
- Every remaining step depends on something that failed and will not recover.

CHOOSE revise WHEN
- A step failed but there is a different route to the same information - a \
  different tool, or a different query.
- A finding changed the picture: the destination turned out to be somewhere \
  a later step assumed wrongly, or a constraint makes a planned step \
  pointless.
- The traveller's requirements are not yet addressed and no remaining step \
  addresses them.

CHOOSE continue WHEN
- The plan is working and the next step is still the right next thing.

BE BIASED TOWARDS finish. Plans are written before any evidence exists, so \
they routinely include steps that turn out to be unnecessary. Every extra \
step costs the traveller time. If you have specific, usable material that \
answers the goal, stop.

DO NOT revise merely because an answer could be more thorough. Revise only \
when something is actually missing or actually broken.\
"""


async def replanner_node(state: AgentState, *, settings: Settings | None = None) -> AgentState:
    """Decide whether to continue, revise the plan, or finish.

    Args:
        state: Current graph state. Requires `plan` and `completed_steps`.
        settings: Settings override, for tests.

    Returns:
        A partial state update carrying the decision's effects.
    """
    cfg = settings or get_settings()

    plan = state.get("plan", [])
    index = state.get("current_step_index", 0)
    completed = state.get("completed_steps", [])
    replan_count = state.get("replan_count", 0)

    # --- Cheap exits, before spending a model call -------------------------

    if index >= len(plan):
        logger.info("agent.plan_exhausted", steps_completed=len(completed))
        return AgentState(status="running", stopped_because=None)

    if replan_count >= cfg.agent_max_replan_cycles:
        # Budget spent. Skip to composing an answer from what exists.
        logger.info("agent.replan_budget_exhausted", replan_count=replan_count)
        return AgentState(
            plan=plan[:index],
            status="partial",
            stopped_because="replan_budget_exhausted",
        )

    # The step that just ran is the last element of `completed_steps`. If it
    # succeeded and steps remain, continuing is nearly always right, and
    # asking a model to confirm that costs a request per step for no benefit.
    if completed and completed[-1].succeeded and index < len(plan):
        remaining = len(plan) - index
        if remaining == 1 and plan[index].kind == "compose":
            # The only thing left is composition, which the responder does.
            return AgentState(plan=plan[:index], stopped_because=None)
        if remaining > 0 and completed[-1].step.kind != "verify":
            logger.debug("agent.replan_skipped", reason="last_step_succeeded")
            return AgentState(status="running")

    # --- Ask ---------------------------------------------------------------

    history = "\n\n".join(result.as_history_line() for result in completed)
    remaining_steps = "\n".join(
        f"{position + 1}. [{step.kind}] {step.description}"
        for position, step in enumerate(plan[index:])
    )

    try:
        replan = await structured_call(
            ModelRole.PLANNER,
            [
                SystemMessage(content=REPLANNER_PROMPT),
                HumanMessage(
                    content=(
                        f"GOAL: {state.get('goal', '')}\n\n"
                        f"COMPLETED SO FAR:\n{history or '(nothing yet)'}\n\n"
                        f"REMAINING PLAN:\n{remaining_steps or '(nothing left)'}"
                    )
                ),
            ],
            Replan,
            purpose="replan",
            settings=cfg,
        )
    except ExternalServiceError as exc:
        logger.warning("agent.replan_failed", error=exc.message)
        # Carrying on with the existing plan is the safe default: it is what
        # would have happened without a replanner at all.
        return AgentState(status="running", errors=[{"node": "replanner", "error": exc.message}])

    logger.info(
        "agent.replan_decision",
        decision=replan.decision,
        replan_count=replan_count,
        reasoning=replan.reasoning[:120],
    )

    if replan.decision == "finish":
        # Truncating the plan at the current index is what ends the loop:
        # the router sees no remaining steps and moves to the responder.
        return AgentState(plan=plan[:index], stopped_because="replanner_finished")

    if replan.decision == "revise" and replan.revised_steps:
        # Splice: completed steps stay, the tail is replaced.
        revised = plan[:index] + replan.revised_steps[: cfg.agent_max_plan_steps - index]
        logger.info("agent.plan_revised", new_steps=len(replan.revised_steps))
        return AgentState(plan=revised, replan_count=replan_count + 1, status="running")

    return AgentState(status="running")


def route_after_replan(state: AgentState) -> Literal["execute", "respond"]:
    """Decide the next node after replanning.

    Kept as a pure function of state so the loop's exit conditions are
    testable without running the graph.

    Args:
        state: Current graph state.

    Returns:
        `"execute"` to run another step, `"respond"` to compose the answer.
    """
    plan = state.get("plan", [])
    index = state.get("current_step_index", 0)

    if index >= len(plan):
        return "respond"
    return "execute"
