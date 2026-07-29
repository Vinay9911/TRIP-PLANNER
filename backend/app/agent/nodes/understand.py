"""Understanding node: work out what is being asked, and whether to ask back.

This node runs before any planning and does four things in one model call:
detects the language, resolves the request into a concrete goal with dates and
a destination, collects any constraints stated in this message, and decides
whether the request is too underspecified to plan well.

**Memory is recalled *before* this call, not after, and that ordering is the
point.** The brief asks the agent to clarify ambiguous input; it also asks it
to apply remembered preferences without the user repeating themselves. Those
two requirements collide if you implement them independently - the agent asks
"what's your budget?" to someone who established their budget three sessions
ago, which is exactly the behaviour long-term memory is supposed to eliminate.

So retrieved memories are injected into the clarification decision, and the
prompt is explicit that anything already known must not be asked about. What
remains is a question the agent genuinely could not answer for itself.

**Clarification is deliberately conservative.** An agent that interrogates
users before every request is worse than one that makes reasonable
assumptions and states them. The rule encoded below: ask only when a wrong
guess would waste the whole itinerary - no destination at all, or a stated
constraint whose meaning is genuinely unclear. Missing dates are *not* worth a
question; assume the near future, say so, and let the traveller correct it.
"""

from __future__ import annotations

from datetime import date

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.agent.state import AgentState
from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.memory.service import MemoryService
from app.services.llm import ModelRole, structured_call

logger = get_logger(__name__)


class Understanding(BaseModel):
    """Structured reading of the traveller's request."""

    goal: str = Field(
        description=(
            "The request restated as one concrete, actionable goal, in English, "
            "e.g. 'Plan a 2-day itinerary in Kyoto for a vegetarian traveller who "
            "prefers a slow pace'. Always in English regardless of the input language."
        )
    )
    destination: str | None = Field(
        default=None, description="City being asked about, or null if none was named."
    )
    start_date: str | None = Field(
        default=None,
        description=(
            "First day as YYYY-MM-DD. Resolve relative expressions such as 'next "
            "weekend' or 'in March' against today's date. Null if genuinely unstated."
        ),
    )
    end_date: str | None = Field(default=None, description="Last day as YYYY-MM-DD, or null.")
    detected_language: str = Field(
        default="en",
        max_length=12,
        description=(
            "BCP-47 tag of the language the traveller wrote in, e.g. 'en', 'ja', "
            "'es', 'fr'. This determines the language of the reply."
        ),
    )
    constraints: list[str] = Field(
        default_factory=list,
        description=(
            "Hard requirements stated in THIS message only, as short phrases, e.g. "
            "['vegetarian', 'travelling with a dog', 'under 100 USD per night']. "
            "Do not repeat requirements already listed as known."
        ),
    )
    needs_clarification: bool = Field(
        default=False,
        description=(
            "True ONLY if planning is impossible or would be wasted without an "
            "answer. Missing dates do not qualify - assume the near future instead."
        ),
    )
    clarifying_question: str | None = Field(
        default=None,
        max_length=300,
        description=(
            "One short, friendly question, asking at most two things. Written in the "
            "traveller's own language. Null when no clarification is needed."
        ),
    )


UNDERSTANDING_PROMPT = """\
You are the intake step of a travel planning agent. Read the traveller's \
message and produce a structured reading of it.

TODAY'S DATE IS {today}. Resolve every relative date against it.

WHEN TO ASK A CLARIFYING QUESTION
Ask only when planning would be impossible or wasted without an answer:
- No destination is identifiable at all, and none was mentioned earlier in \
  the conversation.
- The request is so broad it could mean several different trips \
  ("plan me a holiday" with no other signal).
- A stated requirement is genuinely ambiguous in a way that would change the \
  whole plan.

DO NOT ask about:
- Dates. Assume the near future, plan anyway, and let them correct it.
- Budget, interests, pace, or dietary needs UNLESS they are missing AND not \
  in the known-preferences list below. Prefer sensible defaults over an \
  interrogation.
- Anything already answered earlier in this conversation.
- Anything in the known-preferences list. You already know it. Asking again \
  is the single worst thing you can do here - it tells the traveller you \
  have forgotten them.

One question at a time, covering at most two things. A traveller who wanted a \
form would have filled in a form.

LANGUAGE
Detect the language of their message and record it. It sets the reply \
language. Your `goal` field stays in English regardless - it is for internal \
planning - but `clarifying_question` must be in their language.
{memory_section}\
"""


async def understand_node(
    state: AgentState,
    *,
    memory_service: MemoryService | None = None,
    settings: Settings | None = None,
) -> AgentState:
    """Interpret the request, recall relevant memory, and decide on clarifying.

    Args:
        state: Current graph state. Requires `messages` and `user_id`.
        memory_service: Long-term memory access. When absent the node still
            works, just without personalisation.
        settings: Settings override, for tests.

    Returns:
        A partial state update carrying the understanding, the memory block,
        and any clarifying question.
    """
    cfg = settings or get_settings()

    user_messages = [m for m in state["messages"] if m.type == "human"]
    if not user_messages:
        return AgentState(status="failed", stopped_because="no_user_message")

    latest = str(user_messages[-1].content)

    # --- Recall first, so the clarification decision can see it ------------
    memory_block = ""
    memory_ids: list[str] = []
    known_facts: list[str] = []
    remembered_constraints: list[str] = []

    if memory_service is not None:
        profile_query = await memory_service.build_profile_query(latest, None)
        memory_context = await memory_service.recall(state["user_id"], profile_query)
        memory_block = memory_context.as_prompt_block()
        memory_ids = memory_context.memory_ids
        remembered_constraints = [m.content for m in memory_context.constraints]
        known_facts = remembered_constraints + [m.content for m in memory_context.preferences]

    memory_section = ""
    if known_facts:
        memory_section = (
            "\n\nKNOWN PREFERENCES for this traveller, from earlier conversations. "
            "Treat every one of these as already answered - never ask about them:\n"
            + "\n".join(f"- {fact}" for fact in known_facts)
        )

    # --- Interpret ---------------------------------------------------------
    history = _recent_exchange(state)

    try:
        understanding = await structured_call(
            ModelRole.PLANNER,
            [
                SystemMessage(
                    content=UNDERSTANDING_PROMPT.format(
                        today=date.today().isoformat(), memory_section=memory_section
                    )
                ),
                HumanMessage(content=f"{history}\n\nLATEST MESSAGE:\n{latest}"),
            ],
            Understanding,
            purpose="understand_request",
            settings=cfg,
        )
    except ExternalServiceError as exc:
        logger.warning("agent.understanding_failed", error=exc.message)
        # Degrade to a permissive reading rather than failing the run: the
        # planner can still work from the raw message.
        return AgentState(
            goal=latest,
            memory_block=memory_block,
            memory_ids=memory_ids,
            detected_language="en",
            constraints=[],
            needs_clarification=False,
            errors=[{"node": "understand", "error": exc.message}],
        )

    # Constraints from memory and from this message are merged, deduplicated
    # case-insensitively while keeping the original casing for display.
    merged_constraints = _merge_constraints(remembered_constraints, understanding.constraints)

    logger.info(
        "agent.understood",
        destination=understanding.destination,
        language=understanding.detected_language,
        needs_clarification=understanding.needs_clarification,
        constraints=len(merged_constraints),
        known_facts=len(known_facts),
    )

    update = AgentState(
        goal=understanding.goal,
        destination=understanding.destination,
        start_date=understanding.start_date,
        end_date=understanding.end_date,
        detected_language=understanding.detected_language,
        constraints=merged_constraints,
        memory_block=memory_block,
        memory_ids=memory_ids,
        needs_clarification=understanding.needs_clarification,
        clarifying_question=understanding.clarifying_question,
    )

    if understanding.needs_clarification and understanding.clarifying_question:
        update["status"] = "clarifying"
        update["final_response"] = understanding.clarifying_question

    return update


def _recent_exchange(state: AgentState, limit: int = 6) -> str:
    """Render the recent conversation for the understanding prompt.

    Included so that a follow-up like "make it three days instead" can be
    resolved against what came before, and so the model does not ask about
    something already settled two turns ago.

    Args:
        state: Current graph state.
        limit: How many recent messages to include.

    Returns:
        A formatted transcript, or a placeholder when this is the first turn.
    """
    messages = [m for m in state["messages"] if m.type in ("human", "ai")][:-1]
    if not messages:
        return "CONVERSATION SO FAR: (this is the first message)"

    lines = [
        f"{'Traveller' if message.type == 'human' else 'Assistant'}: {str(message.content)[:400]}"
        for message in messages[-limit:]
    ]
    return "CONVERSATION SO FAR:\n" + "\n".join(lines)


def _merge_constraints(from_memory: list[str], from_message: list[str]) -> list[str]:
    """Combine remembered and freshly-stated constraints without duplicates.

    Args:
        from_memory: Constraint statements retrieved from long-term memory.
        from_message: Constraints stated in the current message.

    Returns:
        Deduplicated constraints, remembered ones first.
    """
    merged: list[str] = []
    seen: set[str] = set()

    for constraint in [*from_memory, *from_message]:
        key = constraint.strip().lower()
        if key and key not in seen:
            seen.add(key)
            merged.append(constraint.strip())

    return merged
