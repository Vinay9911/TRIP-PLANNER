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
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.agent.state import AgentState
from app.agent.trip_state import (
    decide_mode,
    merge_trip_state,
    resolve_scoped_service,
    scoped_clarifying_question,
)
from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.memory.service import MemoryService
from app.services.llm import ModelRole, structured_call

logger = get_logger(__name__)


class Understanding(BaseModel):
    """Structured reading of the traveller's request.

    Beyond the goal and language, this extracts the *slots* that drive the
    three-gear conversation (see `app/agent/trip_state.py`): the model's job
    here is pure extraction - "does this message state a duration?" - while
    the decision of whether to clarify, advise or plan is made in code from
    what was extracted. Keeping judgement out of the model keeps the routing
    deterministic and testable.
    """

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
    origin: str | None = Field(
        default=None,
        description=(
            "City the traveller will travel FROM, if stated in this message or "
            "listed among the known preferences (their home city). Null otherwise."
        ),
    )
    duration_days: int | None = Field(
        default=None,
        ge=1,
        le=90,
        description="Trip length in days when stated ('5 days', 'a week' = 7). Null otherwise.",
    )
    travel_window: str | None = Field(
        default=None,
        max_length=60,
        description=(
            "Rough timing in the traveller's words when exact dates are absent, "
            "e.g. 'early October', 'next summer'. Null if dates were resolved or "
            "nothing was said."
        ),
    )
    party: str | None = Field(
        default=None,
        max_length=60,
        description="Who is travelling when stated, e.g. 'couple', 'family with two kids'.",
    )
    budget_tier: str | None = Field(
        default=None,
        description="One of 'budget', 'mid-range', 'luxury' when stated or clearly implied.",
    )
    priorities: list[str] = Field(
        default_factory=list,
        description=(
            "What matters most for this trip, stated in THIS message, as short "
            "phrases, e.g. ['backwaters', 'food', 'quiet places']."
        ),
    )
    wants_full_plan: bool = Field(
        default=False,
        description=(
            "True when the traveller's own words are a command to build the "
            "trip, not just an expression of interest in a place. Two patterns "
            "both count: an explicit shortcut ('just plan it', 'surprise me') "
            "AND an imperative planning request that already gives at least a "
            "duration ('Plan me 2 days in Kyoto', 'Build me a 5-day Kerala "
            "itinerary') - the verb 'plan/build/make me' plus a length is "
            "already a command, even with no exact dates. False for a bare "
            "destination mention ('I want to go to Kerala', 'thinking about "
            "Vietnam') - that is interest, not a command, however enthusiastic."
        ),
    )
    scoped_service: str = Field(
        default="none",
        description=(
            "Set when the message asks for ONE specific service rather than trip "
            "planning: 'flights' ('flights to Tokyo in November'), 'stays' "
            "('find me a hotel in Kochi'), 'restaurants', 'attractions', or "
            "'weather'. Use 'none' when they are planning or discussing a trip."
        ),
    )
    asks_for_nothing: bool = Field(
        default=False,
        description=(
            "True when the message makes no request at all - a greeting "
            "('hi', 'hello buddy'), thanks, an acknowledgement ('ok', 'nice'), "
            "or a remark about the conversation itself. False whenever the "
            "message asks for, changes, or supplies anything about the trip, "
            "however briefly ('make day 2 lighter', 'from Delhi', '5 days')."
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
        max_length=400,
        description=(
            "One short, warm question, asking at most two things, written in the "
            "traveller's own language. Offer two or three concrete example options "
            "to pick from rather than an open-ended blank, and one or two fitting "
            "emoji are welcome. Null when no clarification is needed."
        ),
    )


UNDERSTANDING_PROMPT = """\
You are the intake step of a travel planning agent. Read the traveller's \
message and produce a structured reading of it.

TODAY'S DATE IS {today}. Resolve every relative date against it.

SLOT EXTRACTION
Fill origin, duration_days, travel_window, party, budget_tier and priorities \
from what THIS message states, plus anything in the known-preferences list \
that applies (their home city fills origin, for example). Extract only - do \
not guess a slot the traveller has not given you. Slots already listed under \
KNOWN TRIP DETAILS are settled; leave them null here unless this message \
changes them.

NARROWING THE DESTINATION IS A CHANGE, NOT A REPETITION
When the known destination is a country or region and this message names a \
city, area or district inside it, set `destination` to that narrower place. \
"Let's do Mumbai" after discussing India means the destination is now Mumbai; \
leaving it as India would plan the wrong trip. The same applies to picking one \
of several options you offered ("Rugged Coastlines", "the backwaters") - if it \
identifies a specific place, extract it.

CRITICAL: A country or a broad region (e.g., "USA", "United States", "Europe", "Kerala") \
IS a valid destination. If the traveller names a country, extract it into the \
`destination` field. Do NOT leave `destination` null just because it is broad. \
Only leave `destination` null when this message genuinely does not indicate \
where they are going.

Set wants_full_plan only for an explicit request or a clear acceptance of a \
proposed outline.

CRITICAL: Set `scoped_service` to one of ('flights', 'stays', 'restaurants', \
'attractions', 'weather') if the user asks for ONE specific thing (e.g. \
"recommend places to eat" -> "restaurants"). You MUST do this EVEN IF they \
are already in the middle of planning a full trip. If they ask for restaurants, \
set scoped_service to 'restaurants', do NOT leave it as 'none'. Use 'none' \
ONLY when they are discussing the trip as a whole.

A flight search needs a departure city - it is meaningless without one. If \
scoped_service is 'flights' and no origin is known (neither in this message \
nor under KNOWN TRIP DETAILS below), set needs_clarification=true and ask \
for their departure city specifically. Do NOT invent an illustrative example \
city ("if you were departing from X...") - a traveller who never mentioned \
that city finds it confusing, not helpful. Just ask.

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
{trip_section}{memory_section}\
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

    trip_state = dict(state.get("trip_state") or {})
    trip_section = _trip_section(trip_state)

    # --- Interpret ---------------------------------------------------------
    history = _recent_exchange(state)

    try:
        understanding = await structured_call(
            ModelRole.PLANNER,
            [
                SystemMessage(
                    content=UNDERSTANDING_PROMPT.format(
                        today=date.today().isoformat(),
                        trip_section=trip_section,
                        memory_section=memory_section,
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
            mode="plan",
            trip_state=trip_state,
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

    # Fold this turn's extracted slots into the conversation's ledger. The
    # merge never lets absence erase: a follow-up that mentions no duration
    # keeps the duration established two turns ago.
    trip_state = merge_trip_state(
        trip_state,
        {
            "destination": understanding.destination,
            "origin": understanding.origin,
            "duration_days": understanding.duration_days,
            "travel_window": understanding.travel_window,
            "start_date": understanding.start_date,
            "end_date": understanding.end_date,
            "party": understanding.party,
            "budget_tier": understanding.budget_tier,
            "priorities": understanding.priorities,
        },
    )

    # A destination that survives from an earlier turn still counts: "5 days,
    # mostly backwaters" names no destination but continues the Kerala thread.
    effective_destination = understanding.destination or trip_state.get("destination")

    # A date that has already passed is almost always a typo for next year,
    # and planning it wastes the whole run. This was a real failure: a request
    # for "8 july 2026" made on 30 July 2026 produced a complete five-day
    # itinerary for a trip three weeks in the past, and the only hint was a
    # buried line saying the forecast could not be checked. The weather tool
    # had reported the problem accurately; nothing acted on it. Checked here,
    # in code, because it is a fact about the calendar rather than a judgement.
    past_date = _first_past_date(understanding.start_date, understanding.end_date)
    if past_date is not None:
        readable = past_date.strftime("%-d %B %Y") if _supports_dash_d() else past_date.isoformat()
        return AgentState(
            goal=understanding.goal,
            mode="clarify",
            trip_state=trip_state,
            destination=effective_destination,
            detected_language=understanding.detected_language,
            constraints=merged_constraints,
            memory_block=memory_block,
            memory_ids=memory_ids,
            needs_clarification=True,
            clarifying_question=(
                f"Just to check — {readable} has already passed. "
                "Did you mean next year, or different dates? 📅"
            ),
            status="clarifying",
            final_response=(
                f"Just to check — {readable} has already passed. "
                "Did you mean next year, or different dates? 📅"
            ),
        )

    # A message that asks for nothing gets an answer, not a pipeline.
    #
    # Once a traveller confirms an outline, `outline_confirmed` sticks so that
    # follow-up edits refine the trip rather than restarting the conversation.
    # The cost of that was every later message planning, including ones that
    # were not requests: typing "heloo buddy" into a Lucknow conversation
    # produced a complete two-day itinerary. Absurd to read, and it spent tens
    # of thousands of tokens on a greeting.
    #
    # Answered here, in code, with no second model call - the reply is a fact
    # about the conversation's own state, and asking a model to write it would
    # cost more than the turn is worth.
    if understanding.asks_for_nothing and effective_destination:
        greeting = _acknowledge(effective_destination, trip_state)
        return AgentState(
            goal=understanding.goal,
            mode="clarify",
            trip_state=trip_state,
            destination=effective_destination,
            detected_language=understanding.detected_language,
            constraints=merged_constraints,
            memory_block=memory_block,
            memory_ids=memory_ids,
            needs_clarification=True,
            clarifying_question=greeting,
            status="clarifying",
            final_response=greeting,
        )

    # "find me flights to london" -> "from delhi" is one request spread over
    # two turns. The second turn doesn't repeat "flights", so without this
    # the scoped ask would be lost and the follow-up would fall through to
    # the advise gear - asking vibe questions about a destination the
    # traveller only ever wanted a flight to.
    effective_scoped_service = resolve_scoped_service(understanding.scoped_service, trip_state)

    mode = decide_mode(
        needs_clarification=understanding.needs_clarification,
        destination=effective_destination,
        wants_full_plan=understanding.wants_full_plan,
        scoped_service=effective_scoped_service,
        trip_state=trip_state,
        focus=state.get("focus"),
    )

    # Carry the scoped ask forward only while it is still unresolved; once it
    # is satisfied (or superseded by a genuine full-trip decision) the flag
    # must not linger, or every later turn would keep re-scoping to it.
    if mode == "clarify" and effective_scoped_service != "none":
        trip_state["pending_scoped_service"] = effective_scoped_service
    else:
        trip_state.pop("pending_scoped_service", None)

    # Confirmation is sticky: once the traveller asks for the full plan, every
    # later turn is a refinement, not a fresh round of advice. Deliberately
    # NOT set for a scoped decision (flights, a stay) - that is a one-off ask,
    # not a decision to build a whole trip, and marking it confirmed would
    # trap every later turn in full-plan mode regardless of what is asked.
    # This was a real bug: "find me flights to london" -> "from delhi" used
    # to turn a flights-only question into an unwanted full day-by-day
    # itinerary with hotel suggestions, because the first turn's scoped
    # decision got recorded as if the whole trip had been confirmed.
    if (
        mode == "plan"
        and not understanding.needs_clarification
        and effective_scoped_service == "none"
    ):
        trip_state["outline_confirmed"] = True

    # `mode` is the single source of truth for routing (see
    # `route_after_understand`), so a "clarify" decision must leave here with
    # a guaranteed question, even when the model itself did not flag one -
    # the code-level scoped-slot rule above can decide "clarify" on its own.
    needs_clarification = understanding.needs_clarification
    clarifying_question = understanding.clarifying_question
    if mode == "clarify" and not clarifying_question:
        needs_clarification = True
        clarifying_question = (
            scoped_clarifying_question(effective_scoped_service)
            if effective_scoped_service != "none"
            else "Which city or country did you have in mind?"
        )
    elif mode == "clarify":
        needs_clarification = True

    logger.info(
        "agent.understood",
        destination=effective_destination,
        mode=mode,
        language=understanding.detected_language,
        needs_clarification=needs_clarification,
        scoped_service=effective_scoped_service,
        constraints=len(merged_constraints),
        known_facts=len(known_facts),
    )

    update = AgentState(
        goal=understanding.goal,
        mode=mode,
        trip_state=trip_state,
        scoped_service=effective_scoped_service,
        destination=effective_destination,
        start_date=understanding.start_date or trip_state.get("start_date"),
        end_date=understanding.end_date or trip_state.get("end_date"),
        detected_language=understanding.detected_language,
        constraints=merged_constraints,
        memory_block=memory_block,
        memory_ids=memory_ids,
        needs_clarification=needs_clarification,
        clarifying_question=clarifying_question,
    )

    if mode == "clarify":
        update["status"] = "clarifying"
        update["final_response"] = clarifying_question

    return update


def _supports_dash_d() -> bool:
    """Whether this platform's strftime accepts the `%-d` (no zero pad) flag.

    glibc does; the Windows C runtime does not and raises. Checked once rather
    than assumed, so date phrasing does not crash the intake step on Windows.
    """
    try:
        date.today().strftime("%-d")
    except ValueError:
        return False
    return True


def _first_past_date(*values: str | None) -> date | None:
    """The earliest of these ISO dates that has already passed.

    Args:
        *values: Date strings, any of which may be None or malformed.

    Returns:
        The date, or None when all are in the future, absent or unparseable.
        Unparseable is deliberately treated as "no problem here" - a malformed
        date is the date parser's business, not this check's.
    """
    today = date.today()
    for value in values:
        if not value:
            continue
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            continue
        if parsed < today:
            return parsed
    return None


def _trip_section(trip_state: dict[str, object]) -> str:
    """Render the already-settled slots for the understanding prompt.

    Shown so the model neither re-extracts nor re-asks what a previous turn
    established - the same never-ask-twice rule the memory section enforces,
    applied to this conversation's own slots.

    Args:
        trip_state: The persisted slot ledger.

    Returns:
        A prompt section, or an empty string on a fresh conversation.
    """
    labels = (
        ("destination", "destination"),
        ("origin", "travelling from"),
        ("duration_days", "trip length in days"),
        ("travel_window", "when"),
        ("start_date", "start date"),
        ("end_date", "end date"),
        ("party", "party"),
        ("budget_tier", "budget"),
        ("priorities", "priorities"),
    )
    lines = []
    for key, label in labels:
        value = trip_state.get(key)
        if value:
            rendered = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
            lines.append(f"- {label}: {rendered}")

    if not lines:
        return ""
    return (
        "\n\nKNOWN TRIP DETAILS, settled earlier in this conversation. Do not "
        "ask about these and leave their slots null unless this message "
        "changes them:\n" + "\n".join(lines)
    )


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


def _acknowledge(destination: str, trip_state: dict[str, Any]) -> str:
    """Reply to a message that asked for nothing.

    Says where the conversation has got to and offers the obvious next step,
    so a greeting is answered like a greeting instead of triggering a plan.

    Composed from the slot ledger rather than by a model: the content is
    entirely a fact about state this function already has, and spending a call
    to phrase it would cost more than the turn is worth.

    Args:
        destination: Where the trip is, known by this point.
        trip_state: The conversation's slot ledger.

    Returns:
        One friendly sentence, plus a concrete offer.
    """
    duration = trip_state.get("duration_days")
    window = trip_state.get("travel_window") or trip_state.get("start_date")

    where = f"We're on **{destination}**"
    if duration and window:
        where += f" — {duration} days, {window}"
    elif duration:
        where += f" — {duration} days"

    if trip_state.get("outline_confirmed"):
        offer = "Want me to change anything, or shall I look at flights, stays or food? ✈️"
    elif duration and window:
        offer = 'Say "just plan it" and I\'ll build the day-by-day plan. 🗺️'
    else:
        offer = "Tell me how long you have and roughly when, and I'll plan it properly. 📅"

    return f"Hey! 👋 {where}.\n\n{offer}"
