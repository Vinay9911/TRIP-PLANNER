"""Executor node: carry out one plan step using tools.

This node is where `create_agent` is used, and the split is deliberate.

LangChain 1.x removed the prebuilt Plan-and-Execute agent, replacing it with
`create_agent` - a maintained, well-tested ReAct harness that decides which
tool to call, calls it, reads the result, and decides whether to call another.
That inner loop is a solved problem, and hand-rolling it would mean
reimplementing tool-call parsing, error recovery and message threading for no
benefit.

What `create_agent` does *not* do is plan ahead. So this project keeps the
planning loop explicit in the graph and delegates each individual step to a
`create_agent` instance. The honest answer to "why didn't you just use
`create_agent`?" is that it *is* used - for the part it is good at.

**The executor sees one step, not the plan.** It is given the overall goal for
context and the current step as its instruction. Handing it the full plan
reliably causes it to run ahead and attempt later steps, which defeats the
point of planning and makes the replanner's view of progress wrong.

Tool selection is entirely the model's. Nothing here inspects the step text
for keywords and pre-selects tools.
"""

from __future__ import annotations

import asyncio
import time

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.state import AgentState, PlanStep, StepResult
from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.tools.base import ToolCallRecord
from app.tools.registry import get_tools

logger = get_logger(__name__)


EXECUTOR_PROMPT = """\
You are the execution step of a travel planning agent. You have been given \
ONE task from a larger plan. Complete exactly that task and nothing else.

Use whichever tools you need. Call several if the task genuinely requires it, \
and none if you already have what the task asks for.

RULES
- Do that task only. Later steps in the plan are not yours; another execution \
  pass will handle them.
- Prefer search_travel_guide over search_web for understanding a destination. \
  It is free and better structured. Use search_web for things that change: \
  prices, opening hours, events on specific dates.
- When the traveller has hard requirements, pass them to tools as filters \
  rather than filtering results yourself afterwards. find_places accepts \
  conditions like vegetarian and wheelchair_accessible; search_travel_guide \
  accepts constraints.
- If a tool reports itself unavailable, read its note and follow it. Do not \
  retry a tool that told you not to, and never invent data a tool failed to \
  return - say it is unavailable.
- Report what you actually found: specific names, places and figures. Your \
  output is the input to the next step, so a vague summary loses the work.

Do not write the final answer to the traveller. Report findings.\
"""


async def executor_node(
    state: AgentState,
    *,
    tool_records: list[ToolCallRecord] | None = None,
    settings: Settings | None = None,
) -> AgentState:
    """Execute the current plan step.

    Args:
        state: Current graph state. Requires `plan` and `current_step_index`.
        tool_records: The live tool-call recorder, used to attribute calls to
            this step by snapshotting its length before and after.
        settings: Settings override, for tests.

    Returns:
        A partial state update appending one `StepResult` and advancing the
        step index.
    """
    cfg = settings or get_settings()

    plan = state.get("plan", [])
    index = state.get("current_step_index", 0)

    if index >= len(plan):
        return AgentState(current_step_index=index)

    step = plan[index]
    started = time.perf_counter()
    calls_before = len(tool_records) if tool_records is not None else 0

    prompt = _build_step_prompt(state, step)

    try:
        from langchain.agents import create_agent

        from app.services.llm import ModelRole, get_model

        agent = create_agent(
            model=get_model(ModelRole.EXECUTOR, settings=cfg),
            tools=get_tools(),
            system_prompt=EXECUTOR_PROMPT,
        )

        # A per-step deadline, so one pathologically slow tool chain cannot
        # consume the whole request. The remaining steps still run.
        response = await asyncio.wait_for(
            agent.ainvoke(
                {"messages": [HumanMessage(content=prompt)]},
                # Bounds the ReAct loop inside the step, independently of the
                # outer plan budget. Two graph steps per tool round trip.
                {"recursion_limit": cfg.agent_max_tool_calls_per_step * 2 + 4},
            ),
            timeout=cfg.agent_step_timeout_seconds,
        )
        output = _extract_output(response)
        succeeded, error = True, None

    except TimeoutError:
        logger.warning(
            "agent.step_timeout", step=step.description[:80], timeout=cfg.agent_step_timeout_seconds
        )
        output = ""
        succeeded = False
        error = f"Step timed out after {cfg.agent_step_timeout_seconds}s."

    except ExternalServiceError as exc:
        logger.warning("agent.step_failed", step=step.description[:80], error=exc.message)
        output = ""
        succeeded = False
        error = exc.message

    except Exception as exc:
        # An unexpected failure in one step must not end the run - the
        # replanner may be able to route around it. Logged with a traceback
        # because, unlike the branches above, this indicates a defect.
        logger.exception("agent.step_unexpected_error", step=step.description[:80])
        output = ""
        succeeded = False
        error = f"Unexpected error: {str(exc)[:200]}"

    latency_ms = int((time.perf_counter() - started) * 1000)
    tools_used = (
        [record.tool_name for record in tool_records[calls_before:]]
        if tool_records is not None
        else []
    )

    logger.info(
        "agent.step_completed",
        step_index=index,
        kind=step.kind,
        succeeded=succeeded,
        tools_used=tools_used,
        latency_ms=latency_ms,
    )

    return AgentState(
        completed_steps=[
            StepResult(
                step=step,
                succeeded=succeeded,
                output=output,
                tools_used=tools_used,
                replan_cycle=state.get("replan_count", 0),
                latency_ms=latency_ms,
                error=error,
            )
        ],
        current_step_index=index + 1,
    )


def _build_step_prompt(state: AgentState, step: PlanStep) -> str:
    """Assemble the instruction for one step.

    Carries the goal, trip parameters, hard requirements and prior findings -
    but not the rest of the plan, for the reason given in the module docstring.

    Args:
        state: Current graph state.
        step: The step to execute.

    Returns:
        The prompt text.
    """
    sections = [f"OVERALL GOAL (for context): {state.get('goal', '')}"]

    if state.get("destination"):
        sections.append(f"DESTINATION: {state['destination']}")
    if state.get("start_date"):
        sections.append(
            f"DATES: {state['start_date']} to {state.get('end_date') or state['start_date']}"
        )
    if state.get("constraints"):
        sections.append(
            "HARD REQUIREMENTS (pass these to tools as filters): " + "; ".join(state["constraints"])
        )
    if state.get("memory_block"):
        sections.append(state["memory_block"])

    completed = state.get("completed_steps", [])
    if completed:
        # Only successful findings, and only the recent ones. Failed steps
        # are the replanner's concern; repeating them here invites the
        # executor to retry work that has already been abandoned.
        findings = [result for result in completed if result.succeeded][-3:]
        if findings:
            sections.append(
                "WHAT EARLIER STEPS FOUND:\n"
                + "\n\n".join(f"- {result.output[:800]}" for result in findings)
            )

    sections.append(f"\nYOUR TASK NOW:\n{step.description}")
    return "\n\n".join(sections)


def _extract_output(response: object) -> str:
    """Pull the executor's textual findings out of an agent response.

    Args:
        response: The result of `agent.ainvoke`.

    Returns:
        The last assistant message's text, or an empty string.
    """
    messages = (response or {}).get("messages", []) if isinstance(response, dict) else []

    for message in reversed(messages):
        if isinstance(message, AIMessage) and message.content:
            content = message.content
            if isinstance(content, list):
                # Some providers return content as a list of typed blocks.
                parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                return "\n".join(part for part in parts if part).strip()
            return str(content).strip()

    return ""
