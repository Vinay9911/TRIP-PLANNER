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
from app.core.errors import ExternalServiceError, RateLimitError
from app.core.logging import get_logger
from app.tools.base import ToolCallRecord
from app.tools.registry import get_tools_for_step

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
- search_flights already supports a round trip in ONE call via its optional \
  return_date argument. When both directions are needed, call it once with \
  return_date set - never call it twice, once per direction. That wastes a \
  call for no benefit and the tool already does both legs.
- get_weather_forecast, find_places and search_accommodation all need a \
  place that resolves to ONE point on a map. A whole region, country or \
  island - "Bali", "Kerala", "Provence" - is not that: it silently resolves \
  to whichever same-named village the geocoder happens to rank first \
  anywhere in the world, which is not the place the traveller means. Use a \
  specific city or town instead - one from your own research when you have \
  one ("Uluwatu" or "Ubud", not "Bali"). If a tool reports the place could \
  not be found, that is a strong sign the name you gave it was too broad; \
  retry once with something more specific rather than the same name again.
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
        output = await _run_step_agent(prompt, cfg, focus=state.get("focus"), kind=step.kind)
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


def _seconds_until_a_key_frees(pool: object) -> float:
    """How long until the pool has a usable key again.

    Args:
        pool: The key pool to inspect.

    Returns:
        Seconds until the soonest key becomes available, or 0 if one already is.
    """
    status = pool.status()  # type: ignore[attr-defined]
    if status["available_keys"]:
        return 0.0
    return min(
        (float(entry["cooldown_remaining_s"]) for entry in status["keys"]),
        default=0.0,
    )


async def _run_step_agent(
    prompt: str, cfg: Settings, *, focus: list[str] | None = None, kind: str = "research"
) -> str:
    """Run one step through a `create_agent` instance, rotating keys on 429.

    `create_agent` owns its internal tool-calling loop and holds whichever key
    the model was constructed with, so rotation cannot happen *inside* a call.
    Instead a rate limit fails the whole step and it is retried with a fresh
    model, which draws the next key from the pool.

    Args:
        prompt: The step instruction.
        cfg: Application settings.
        focus: Services the traveller enabled; a switched-off service's tool
            is withheld from the model entirely.
        kind: The plan step's kind, which narrows the toolbox to what that
            kind of work can use - see `get_tools_for_step`.

    Returns:
        The executor's textual findings.

    Raises:
        RateLimitError: If every key was exhausted.
        ExternalServiceError: If the step failed for another reason.
        TimeoutError: If the step exceeded its deadline.
    """
    from langchain.agents import create_agent

    from app.services.llm import (
        ModelRole,
        UsageRecordingCallback,
        get_llm_pool,
        get_model,
        models_for_role,
    )

    pool = get_llm_pool(cfg)

    # Groq's daily token budget is per model, so an exhausted bucket is not
    # the end of the road: the same key can still serve a different model.
    # This step is by far the heaviest consumer in the system, and it sits on
    # one of the smallest daily allowances, so the fallback matters most here.
    candidates = models_for_role(ModelRole.EXECUTOR, cfg)
    model_index = 0

    # One attempt per key on each candidate model, plus a little slack.
    attempts = max(pool.size, 1) * len(candidates) + 1

    for attempt in range(1, attempts + 1):
        # Every key cooling down. A per-minute limit clears in seconds, so a
        # short wait usually restores one - much better than failing a step
        # the rest of the plan depends on. A key that has spent its *daily*
        # allowance is parked for far longer than this wait (see
        # `DAILY_LIMIT_COOLDOWN_SECONDS`), so this will not sit spinning on an
        # exhausted pool: the wait is capped and the attempt then fails
        # honestly.
        if pool.available_count() == 0:
            wait = min(_seconds_until_a_key_frees(pool), 25.0)
            if wait > 0 and attempt < attempts:
                logger.info("agent.step_waiting_for_key", seconds=round(wait, 1))
                await asyncio.sleep(wait + 0.5)

        agent = create_agent(
            model=get_model(ModelRole.EXECUTOR, settings=cfg, model_name=candidates[model_index]),
            tools=get_tools_for_step(kind, focus=focus),
            system_prompt=EXECUTOR_PROMPT,
        )

        try:
            # A per-step deadline, so one pathologically slow tool chain cannot
            # consume the whole request. The remaining steps still run.
            response = await asyncio.wait_for(
                agent.ainvoke(
                    {"messages": [HumanMessage(content=prompt)]},
                    {
                        # Bounds the ReAct loop inside the step, independently
                        # of the outer plan budget. Two graph steps per tool
                        # round trip.
                        "recursion_limit": cfg.agent_max_tool_calls_per_step * 2 + 2,
                        # The only level at which LangChain propagates a
                        # handler down into the model calls `create_agent`
                        # makes internally. Without it the executor - the
                        # single largest token consumer here - reports zero.
                        "callbacks": [UsageRecordingCallback()],
                    },
                ),
                timeout=cfg.agent_step_timeout_seconds,
            )
        except Exception as exc:
            # Normalised so a quota failure can be told from anything else.
            from app.services.llm import _classify_provider_error

            if isinstance(exc, TimeoutError):
                raise
            error = _classify_provider_error(exc)
            if isinstance(error, RateLimitError) and attempt < attempts:
                # A daily exhaustion belongs to the model, not the key, so
                # every key would hit the same wall. Step to the next model
                # instead; a per-minute limit still just rotates keys.
                if (error.details or {}).get("scope") == "per_day" and model_index + 1 < len(
                    candidates
                ):
                    model_index += 1
                    logger.info("agent.step_model_fallback", now_using=candidates[model_index])
                else:
                    logger.info("agent.step_key_rotated", attempt=attempt, of=attempts)
                continue
            raise error from exc

        return _extract_output(response)

    raise RateLimitError(
        "Every API key was rate limited while executing this step.", service="groq"
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

    trip_state = state.get("trip_state") or {}
    if state.get("start_date"):
        sections.append(
            f"DATES: {state['start_date']} to {state.get('end_date') or state['start_date']}"
        )
    elif trip_state.get("travel_window"):
        # Without this, a model holding only the traveller's phrase passes it
        # verbatim into date-taking tools - a live run showed 'early October'
        # submitted as `start_date` eight times. Giving it today's date and an
        # explicit instruction makes the conversion its job, once.
        from datetime import date

        sections.append(
            f"WHEN: {trip_state['travel_window']} (no exact dates given - today is "
            f"{date.today().isoformat()}; resolve this to concrete YYYY-MM-DD dates "
            "yourself before calling any tool that takes dates)"
        )
    if trip_state.get("origin"):
        sections.append(f"TRAVELLING FROM: {trip_state['origin']}")
    if trip_state.get("duration_days"):
        sections.append(f"TRIP LENGTH: {trip_state['duration_days']} days")
    if state.get("constraints"):
        sections.append(
            "HARD REQUIREMENTS (pass these to tools as filters): " + "; ".join(state["constraints"])
        )

    # Attractions/restaurants share find_places with everything else, so
    # switching them off cannot remove a tool - it becomes an instruction
    # here instead. Flights/stays are additionally enforced by tool removal.
    from app.agent.trip_state import disabled_services

    switched_off = disabled_services(state.get("focus"))
    if switched_off:
        sections.append(
            "SERVICES THE TRAVELLER SWITCHED OFF (do not research, search for, "
            "or recommend these): " + ", ".join(switched_off)
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
