"""The agent graph.

Wires the nodes into the plan-execute-replan loop the assignment asks for:

    START
      |
      v
    understand ---- needs clarification? ----> respond ----> END
      |                                          ^
      | no                                       |
      v                                          |
     plan                                        |
      |                                          |
      v                                          |
    execute <----------------+                   |
      |                      |                   |
      v                      | more steps        |
    replan ------------------+                   |
      |                                          |
      | plan exhausted / replanner said finish   |
      +------------------------------------------+

The loop terminates through four independent routes, which is deliberate -
a single exit condition is a single point of hanging:

1. The plan runs out of steps.
2. The replanner decides enough evidence exists and truncates the plan.
3. The replan budget is exhausted; the run is marked partial.
4. LangGraph's own `recursion_limit` backstops all of the above.

The graph is compiled once per process and reused. Per-request state - who is
asking, which services to use - travels in the state dict and in context
variables, never in the compiled graph, so one graph safely serves concurrent
requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from app.agent.nodes.advisor import advisor_node
from app.agent.nodes.executor import executor_node
from app.agent.nodes.planner import planner_node
from app.agent.nodes.replanner import replanner_node, route_after_replan
from app.agent.nodes.responder import responder_node
from app.agent.nodes.understand import understand_node
from app.agent.state import AgentState
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.memory.service import MemoryService
from app.rag.retriever import MultiHopRetriever
from app.tools.base import ToolCallRecord

logger = get_logger(__name__)


@dataclass
class AgentDependencies:
    """Services the graph nodes need.

    Bundled into one object and closed over at build time so node signatures
    stay `(state) -> update`, which is what makes them unit-testable without
    a graph.

    Attributes:
        memory_service: Long-term memory access, or None to run without
            personalisation.
        retriever: Multi-hop retriever, used directly by the advisor node for
            its one-hop orientation. The executor's tools reach the retriever
            through the tool context instead; the advisor takes it here
            because it is a node, not a tool, and must not depend on tool
            plumbing to run.
        settings: Application settings.
    """

    memory_service: MemoryService | None = None
    retriever: MultiHopRetriever | None = None
    settings: Settings | None = None


def route_after_understand(state: AgentState) -> Literal["plan", "advise", "respond"]:
    """Route by the gear the understanding step decided on.

    Clarification skips planning entirely - one model call, not a
    plan-execute cycle that gets discarded. Advice goes to the advisor node:
    grounded options and at most two questions, at a fraction of full-plan
    cost. Everything else runs the pipeline.

    Args:
        state: Current graph state.

    Returns:
        `"respond"` to ask a clarifying question, `"advise"` for an advisory
        turn, `"plan"` to run the full pipeline.
    """
    if state.get("needs_clarification") and state.get("clarifying_question"):
        return "respond"
    if state.get("status") == "failed":
        return "respond"
    if state.get("mode") == "advise":
        return "advise"
    return "plan"


def route_after_execute(state: AgentState) -> Literal["replan", "respond"]:
    """Decide whether the loop continues after a step.

    Args:
        state: Current graph state.

    Returns:
        `"respond"` when the plan is finished, `"replan"` otherwise.
    """
    plan = state.get("plan", [])
    index = state.get("current_step_index", 0)

    if index >= len(plan):
        return "respond"
    return "replan"


def build_graph(
    dependencies: AgentDependencies,
    *,
    tool_records: list[ToolCallRecord] | None = None,
) -> StateGraph:
    """Construct the agent graph.

    Args:
        dependencies: Services the nodes need.
        tool_records: Live tool-call recorder, threaded into the executor so
            calls can be attributed to the step that made them.

    Returns:
        An uncompiled `StateGraph`.
    """
    settings = dependencies.settings or get_settings()

    async def _understand(state: AgentState) -> AgentState:
        return await understand_node(
            state, memory_service=dependencies.memory_service, settings=settings
        )

    async def _advise(state: AgentState) -> AgentState:
        return await advisor_node(state, retriever=dependencies.retriever, settings=settings)

    async def _plan(state: AgentState) -> AgentState:
        return await planner_node(state, settings=settings)

    async def _execute(state: AgentState) -> AgentState:
        return await executor_node(state, tool_records=tool_records, settings=settings)

    async def _replan(state: AgentState) -> AgentState:
        return await replanner_node(state, settings=settings)

    async def _respond(state: AgentState) -> AgentState:
        return await responder_node(state, settings=settings)

    graph: StateGraph = StateGraph(AgentState)

    graph.add_node("understand", _understand)
    graph.add_node("advise", _advise)
    graph.add_node("plan", _plan)
    graph.add_node("execute", _execute)
    graph.add_node("replan", _replan)
    graph.add_node("respond", _respond)

    graph.add_edge(START, "understand")
    graph.add_conditional_edges(
        "understand",
        route_after_understand,
        {"plan": "plan", "advise": "advise", "respond": "respond"},
    )
    # The advisor composes its own reply - routing it through the responder
    # would spend a second model call re-writing a message that is already
    # in the traveller's language.
    graph.add_edge("advise", END)
    graph.add_edge("plan", "execute")
    graph.add_conditional_edges(
        "execute", route_after_execute, {"replan": "replan", "respond": "respond"}
    )
    graph.add_conditional_edges(
        "replan", route_after_replan, {"execute": "execute", "respond": "respond"}
    )
    graph.add_edge("respond", END)

    return graph


def compile_graph(
    dependencies: AgentDependencies,
    *,
    checkpointer: Any = None,
    tool_records: list[ToolCallRecord] | None = None,
) -> Any:
    """Build and compile the agent graph.

    Args:
        dependencies: Services the nodes need.
        checkpointer: LangGraph checkpointer providing short-term memory
            across turns. Omit for a stateless run.
        tool_records: Live tool-call recorder.

    Returns:
        The compiled graph, ready to invoke.
    """
    graph = build_graph(dependencies, tool_records=tool_records)
    compiled = graph.compile(checkpointer=checkpointer)
    logger.info("agent.graph_compiled", has_checkpointer=checkpointer is not None)
    return compiled


def recursion_limit_for(settings: Settings | None = None) -> int:
    """Compute a LangGraph recursion limit from the configured budgets.

    LangGraph counts *node visits*, not loop iterations, so a limit derived
    from the step budget has to account for the execute-replan pair running
    once per step plus the fixed nodes at either end. Derived rather than
    hardcoded so that raising `AGENT_MAX_PLAN_STEPS` in configuration does not
    silently start tripping the framework's own ceiling instead of ours - a
    failure that surfaces as an opaque `GraphRecursionError` rather than the
    intended partial answer.

    Args:
        settings: Settings override, for tests.

    Returns:
        A recursion limit with headroom.
    """
    cfg = settings or get_settings()
    steps = cfg.agent_max_plan_steps + cfg.agent_max_replan_cycles
    # 2 visits per step (execute + replan), plus understand, plan, respond,
    # and a margin so our guardrails trip first.
    return steps * 2 + 6
