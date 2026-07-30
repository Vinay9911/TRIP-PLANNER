"""Graph state.

The state is the contract between nodes. Each node reads what it needs and
returns a *partial* update, which LangGraph merges - so no node needs to know
what any other node writes, and a node can be unit-tested by handing it a
hand-built dict.

Two design notes worth stating.

`messages` uses LangGraph's `add_messages` reducer, so returning new messages
appends rather than replaces. Every other field is last-write-wins, which is
what you want for things like `destination` where the newest determination is
the correct one.

`completed_steps` accumulates rather than being overwritten, because the
replanner's whole job is to look at what has already been tried. A replanner
that only sees the current step cannot tell the difference between "this is
the first attempt" and "this has failed twice", and will happily loop.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

RunStatus = Literal["running", "clarifying", "completed", "partial", "failed"]

# What kind of work a plan step represents. Not used to route execution - the
# executor picks tools itself - but recorded so the admin trace can show what
# the planner intended, and used by the responder to spot a plan that never
# gathered any evidence.
StepKind = Literal["research", "logistics", "verify", "compose"]


class PlanStep(BaseModel):
    """One step of the agent's plan.

    Attributes:
        description: What to accomplish, phrased as an instruction the
            executor can act on without seeing the rest of the plan.
        kind: What category of work this is.
        depends_on_previous: Whether this step needs the previous step's
            output. Steps that do not are candidates for concurrent execution.
    """

    description: str = Field(
        description=(
            "A single, self-contained task, e.g. 'Research which Kyoto districts suit a "
            "traveller who wants temples and quiet streets'. Must be actionable on its "
            "own - the executor sees the goal and this step, not the whole plan."
        )
    )
    kind: StepKind = Field(
        default="research",
        description=(
            "research = gather information with tools. logistics = flights, hotels, "
            "transport. verify = check or cross-reference something already found. "
            "compose = assemble the final answer."
        ),
    )
    depends_on_previous: bool = Field(
        default=True,
        description="True if this step needs the previous step's result to be useful.",
    )


class StepResult(BaseModel):
    """The outcome of executing one plan step.

    Attributes:
        step: The step that was executed.
        succeeded: Whether it produced usable output.
        output: What the executor found, summarised.
        tools_used: Names of tools called during this step.
        replan_cycle: Which planning cycle introduced the step.
        latency_ms: How long execution took.
        error: Failure reason, when it failed.
    """

    step: PlanStep
    succeeded: bool = True
    output: str = ""
    tools_used: list[str] = Field(default_factory=list)
    replan_cycle: int = 0
    latency_ms: int = 0
    error: str | None = None

    def as_history_line(self) -> str:
        """Render this result for the replanner's view of what has happened."""
        status = "OK" if self.succeeded else "FAILED"
        tools = f" (tools: {', '.join(self.tools_used)})" if self.tools_used else ""
        body = self.output if self.succeeded else (self.error or "no output")
        return f"[{status}] {self.step.description}{tools}\n    -> {body[:600]}"


class AgentState(TypedDict, total=False):
    """State passed between graph nodes.

    `total=False` because nodes populate fields progressively; requiring every
    key up front would force each node to restate fields it does not touch.
    """

    # -- Conversation ------------------------------------------------------
    messages: Annotated[list[AnyMessage], add_messages]

    # -- Identity ----------------------------------------------------------
    user_id: str
    session_id: str
    run_id: str

    # -- Understanding -----------------------------------------------------
    #: The traveller's request, restated as a single actionable goal.
    goal: str
    #: Which gear this turn runs in - clarify, advise, or plan. Decided by
    #: `trip_state.decide_mode` from extracted slots, never by the model
    #: directly, so the routing is deterministic and testable.
    mode: str
    #: Per-conversation slot ledger (origin, duration, priorities, ...).
    #: Loaded from `sessions.trip_state`, merged each turn, persisted back.
    trip_state: dict[str, Any]
    #: Services the traveller has switched on in the UI. Controls which
    #: logistics tools the executor receives and what the prompts allow.
    focus: list[str]
    destination: str | None
    start_date: str | None
    end_date: str | None
    #: BCP-47 tag of the language to reply in.
    detected_language: str
    #: Hard requirements from long-term memory and this conversation, passed
    #: to retrieval and tool calls as filters rather than prompt hints.
    constraints: list[str]
    #: Rendered long-term memory, injected into planner and responder prompts.
    memory_block: str
    memory_ids: list[str]

    # -- Clarification -----------------------------------------------------
    needs_clarification: bool
    clarifying_question: str | None

    # -- Planning ----------------------------------------------------------
    plan: list[PlanStep]
    initial_plan: list[PlanStep]
    current_step_index: int
    completed_steps: Annotated[list[StepResult], operator.add]
    replan_count: int

    # -- Output ------------------------------------------------------------
    final_response: str | None
    status: RunStatus
    #: Set when a guardrail stopped the run early, so the responder can be
    #: honest about the answer being partial.
    stopped_because: str | None

    # -- Telemetry ---------------------------------------------------------
    rag_hops: int
    prompt_tokens: int
    completion_tokens: int
    errors: list[dict[str, Any]]


def initial_state(
    *,
    user_id: str,
    session_id: str,
    run_id: str,
    messages: list[AnyMessage],
    trip_state: dict[str, Any] | None = None,
    focus: list[str] | None = None,
) -> AgentState:
    """Build the starting state for a run.

    Args:
        user_id: Authenticated user.
        session_id: Conversation thread.
        run_id: Unique id for this execution.
        messages: Conversation so far, including the new user message.
        trip_state: Slot ledger persisted from earlier turns of this
            conversation, if any.
        focus: Services the traveller has enabled. None means all.

    Returns:
        A fully-initialised state.
    """
    from app.agent.trip_state import normalise_focus

    return AgentState(
        messages=messages,
        user_id=user_id,
        session_id=session_id,
        run_id=run_id,
        goal="",
        mode="plan",
        trip_state=dict(trip_state or {}),
        focus=normalise_focus(focus),
        destination=None,
        start_date=None,
        end_date=None,
        detected_language="en",
        constraints=[],
        memory_block="",
        memory_ids=[],
        needs_clarification=False,
        clarifying_question=None,
        plan=[],
        initial_plan=[],
        current_step_index=0,
        completed_steps=[],
        replan_count=0,
        final_response=None,
        status="running",
        stopped_because=None,
        rag_hops=0,
        prompt_tokens=0,
        completion_tokens=0,
        errors=[],
    )
