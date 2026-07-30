"""Trip state: the slot-filling ledger behind the conversational planner.

The agent runs in one of three gears, and this module owns the decision:

* **clarify** - no destination at all, or the request is genuinely ambiguous.
  One friendly question, no planning.
* **advise** - a destination but not yet a trip. Present grounded options,
  sketch an outline, ask for at most two missing slots. This is the gear for
  "i want to go to kerala" - the message that used to trigger a full
  itinerary nobody asked for.
* **plan** - run the full plan-execute pipeline. Reached when the trip is
  specified, when the traveller confirms an outline or says "just plan it",
  or when they asked for one specific service (flights, a hotel) that the
  planner can satisfy with a short plan.

The decision is pure code over extracted slots, not a model judgement call.
The model's job is extraction ("does this message state a duration?"); the
routing rule itself must be deterministic, testable, and explainable in one
sentence each - an interviewer will ask exactly when the agent chooses to
interrogate versus act, and "the model felt like it" is not an answer.

Trip state persists per conversation in `sessions.trip_state` (JSONB, see
migration 0009). It is trip-scoped, not durable: "5 days, early October"
belongs to this trip only. Durable facts still flow through long-term memory.

The known slot keys:

``destination``      city or region being discussed
``origin``           where they travel from (also saved to memory as home_base)
``duration_days``    trip length
``travel_window``    free-text timing ("early October") when exact dates absent
``start_date`` / ``end_date``   resolved YYYY-MM-DD dates
``party``            who is going ("couple", "family with kids")
``budget_tier``      budget / mid-range / luxury
``priorities``       what matters ("backwaters", "food")
``focus``            services switched on in the UI (see FOCUS_SERVICES)
``outline_confirmed``  the traveller accepted an outline or asked to plan
``advise_rounds``    how many advisory turns have already happened
"""

from __future__ import annotations

from typing import Any, Literal

AgentMode = Literal["clarify", "advise", "plan"]

#: Services the traveller can switch on and off in the composer. `flights`
#: and `stays` map 1:1 to tools and are removed from the executor's toolbox
#: when off; `attractions` and `restaurants` share `find_places` /
#: `search_travel_guide`, so they are enforced through prompts instead.
FOCUS_SERVICES: tuple[str, ...] = ("flights", "stays", "attractions", "restaurants")

#: How many advisory turns before the agent stops asking and plans with
#: stated assumptions. Two rounds is one options turn plus one outline turn -
#: a third consecutive round of questions is an interrogation, which is the
#: exact behaviour the advise gear exists to avoid inflicting.
MAX_ADVISE_ROUNDS = 2


def normalise_focus(focus: list[str] | None) -> list[str]:
    """Canonicalise a focus selection from the client.

    Args:
        focus: Service names the traveller has enabled, or None for all.

    Returns:
        A list containing only known service names, in canonical order.
        None or an empty selection means everything is on - a client that
        deselects every service almost certainly meant "no special scoping",
        not "refuse to help".
    """
    if not focus:
        return list(FOCUS_SERVICES)
    enabled = {service.strip().lower() for service in focus}
    selected = [service for service in FOCUS_SERVICES if service in enabled]
    return selected or list(FOCUS_SERVICES)


def disabled_services(focus: list[str] | None) -> list[str]:
    """The services the traveller switched off, for prompt injection.

    Args:
        focus: The enabled services.

    Returns:
        Canonically-ordered names of services not in the selection.
    """
    enabled = set(normalise_focus(focus))
    return [service for service in FOCUS_SERVICES if service not in enabled]


def merge_trip_state(existing: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """Fold newly-extracted slots into the persisted trip state.

    Later information wins, but absence never erases: a follow-up like
    "make day 2 lighter" mentions no duration, and forgetting the duration
    because of it would make every refinement destroy the trip.

    Args:
        existing: The trip state loaded from the session.
        updates: Slots extracted from the current message. None values and
            empty strings/lists are treated as "not mentioned", not as
            deletions.

    Returns:
        The merged state. Never mutates its inputs.
    """
    merged = dict(existing)
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, list) and not value:
            continue
        merged[key] = value
    return merged


def decide_mode(
    *,
    needs_clarification: bool,
    destination: str | None,
    wants_full_plan: bool,
    scoped_service: str | None,
    trip_state: dict[str, Any],
) -> AgentMode:
    """Choose the gear for this turn.

    The rules, in the order they apply, each one sentence:

    1. No usable destination or a genuinely ambiguous request -> clarify.
    2. A request for one specific service (flights, a stay, weather) -> plan,
       because the planner will produce a short scoped plan and asking vibe
       questions about a flight search would be absurd.
    3. The traveller confirmed an outline or said "just plan it" -> plan,
       and that confirmation sticks for the rest of the conversation so
       follow-up edits ("make day 2 lighter") refine rather than re-advise.
    4. The trip is already specified (duration plus some date signal) -> plan;
       asking questions whose answers are on the table is a form, not a
       conversation.
    5. The advisory budget is spent -> plan with stated assumptions rather
       than a third round of questions.
    6. Otherwise -> advise.

    Args:
        needs_clarification: The understanding step judged the request too
            ambiguous to act on.
        destination: Destination resolved so far, if any.
        wants_full_plan: The traveller explicitly asked for the full plan or
            accepted a proposed outline this turn.
        scoped_service: Set when the message asks for one specific service
            rather than trip planning ("flights to tokyo in november").
        trip_state: The merged trip state for this conversation.

    Returns:
        The gear to run this turn in.
    """
    if needs_clarification or not destination:
        return "clarify"
    if scoped_service and scoped_service != "none":
        return "plan"
    if wants_full_plan or trip_state.get("outline_confirmed"):
        return "plan"

    has_duration = bool(trip_state.get("duration_days"))
    has_date_signal = bool(trip_state.get("start_date") or trip_state.get("travel_window"))
    if has_duration and has_date_signal:
        return "plan"

    if int(trip_state.get("advise_rounds", 0)) >= MAX_ADVISE_ROUNDS:
        return "plan"

    return "advise"


def missing_slots(trip_state: dict[str, Any]) -> list[str]:
    """Which slots are still worth asking about, most important first.

    Ordered by how much a wrong guess would cost: trip length changes the
    entire shape, priorities pick the districts, timing changes weather and
    seasonal advice, origin unlocks flights. Capped by the caller at two -
    this function only supplies the ranking.

    Args:
        trip_state: The merged trip state.

    Returns:
        Slot names not yet filled, in asking order.
    """
    order = (
        ("duration_days", "how many days the trip is"),
        ("priorities", "what matters most to them (nature, food, culture, beaches)"),
        ("travel_window", "roughly when they are travelling"),
        ("origin", "which city they will travel from"),
    )
    missing = []
    for key, description in order:
        if key == "travel_window":
            if trip_state.get("travel_window") or trip_state.get("start_date"):
                continue
        elif trip_state.get(key):
            continue
        missing.append(description)
    return missing
