"""Advisor node: talk about the trip before building it.

This is the gear between clarification and the full pipeline, and it exists
because of a real transcript: "i want to go to kerala" produced a complete
two-day itinerary for districts the traveller never chose, at full pipeline
cost, without ever asking how long the trip was. Technically impressive,
conversationally wrong. A human agent shown that message would talk first.

What one advisory turn does:

1. **One hop of real retrieval, not a model's impression.** The retriever is
   run with a hop budget of 1 - enough to fetch the destination's guide
   article and enumerate its actual districts or cities, so the options
   offered are grounded in the same corpus the full plan will later use.
   Cost: one embedding call and no LLM tokens, versus a dozen calls for the
   full pipeline.
2. **One model call** that presents a few distinct ways to experience the
   place, sketches an outline when enough is known, and asks for at most two
   missing details - never one the traveller already gave, never one long-term
   memory already answers.

The advisory budget lives in `trip_state.advise_rounds`: after
`MAX_ADVISE_ROUNDS` turns of this, the routing in `decide_mode` stops the
conversation-about-the-conversation and plans with stated assumptions. The
budget is enforced in routing rather than here so this node stays simple:
by the time it runs, the decision that advising is appropriate has been made.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.state import AgentState
from app.agent.trip_state import disabled_services, missing_slots
from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.rag.retriever import MultiHopRetriever
from app.services.llm import ModelRole, call_model
from app.services.usage import record_rag_hops

logger = get_logger(__name__)


ADVISOR_PROMPT = """\
You are a warm, knowledgeable travel assistant having a conversation - you \
are NOT writing an itinerary yet. The traveller has named a destination and \
you are helping them decide what kind of trip it should be.

WRITE A SHORT, FRIENDLY REPLY THAT DOES THREE THINGS

1. Offer 3-4 genuinely different ways to experience the destination, each on \
its own line with a fitting emoji, a bold label and a few concrete places or \
areas from the guide material below. Only name places that appear in that \
material - never invent one.

2. If the trip length is known, sketch a one-line outline (e.g. "Munnar 2 \
nights -> Alleppey 2 nights -> Kochi 1 night"). If it is not known, skip \
this - do not guess.

3. Finish by asking ONLY the questions listed under ASK ABOUT, phrased \
naturally in one sentence together. Never ask anything else, and never ask \
about anything under ALREADY KNOWN - re-asking something the traveller \
already told you is the worst thing you can do here.

TONE
Warm, playful and concise - a knowledgeable friend, not a brochure. A few \
emoji as visual markers (one per option line, maybe one elsewhere). No \
headers, no long paragraphs, under 220 words.

{capabilities_line}\
Mention, in one short closing line, that they can say "just plan it" any \
time and you will build the full day-by-day plan.

{language_instruction}\
"""

#: Shown once, on the first advisory turn of a conversation, so travellers
#: discover the agent's full surface without a manual. Emoji-labelled because
#: the same icons appear on the composer's service toggles - the sentence
#: teaches the UI.
CAPABILITIES_LINE = (
    "Also weave in - naturally, one line - that once the trip takes shape you "
    "can check real weather forecasts, find restaurants and attractions "
    "matching their needs, and compare flights and places to stay: "
    "so they know what to ask for.\n"
)


async def advisor_node(
    state: AgentState,
    *,
    retriever: MultiHopRetriever | None = None,
    settings: Settings | None = None,
) -> AgentState:
    """Produce one advisory turn: grounded options plus at most two questions.

    Args:
        state: Current graph state. Requires `destination` and `trip_state`.
        retriever: Multi-hop retriever, run with a one-hop budget. When absent
            the node still answers, marking its suggestions as general advice.
        settings: Settings override, for tests.

    Returns:
        A partial state update carrying the reply and the incremented
        advisory-round counter.
    """
    cfg = settings or get_settings()
    destination = state.get("destination") or ""
    trip_state = dict(state.get("trip_state") or {})

    guide_block = await _light_retrieval(retriever, state, destination)

    ask_about = missing_slots(trip_state)[:2]
    already_known = _already_known(state, trip_state)
    switched_off = disabled_services(state.get("focus"))

    context_lines = [
        f"DESTINATION: {destination}",
        f"TRAVELLER'S MESSAGE (as a goal): {state.get('goal', '')}",
    ]
    if already_known:
        context_lines.append("ALREADY KNOWN (never ask about these):\n" + already_known)
    if ask_about:
        context_lines.append(
            "ASK ABOUT (at most these, together, in one sentence):\n"
            + "\n".join(f"- {slot}" for slot in ask_about)
        )
    else:
        context_lines.append(
            "ASK ABOUT: nothing - everything needed is known. Instead, end by "
            "asking whether the outline works for them or they want changes."
        )
    if switched_off:
        context_lines.append(
            "SERVICES THE TRAVELLER SWITCHED OFF (do not offer these): " + ", ".join(switched_off)
        )
    if guide_block:
        context_lines.append(guide_block)
    else:
        context_lines.append(
            "NO GUIDE MATERIAL WAS AVAILABLE. Offer only broad, well-known "
            "aspects of the destination and say your suggestions are general "
            "advice you will verify while planning."
        )

    first_round = int(trip_state.get("advise_rounds", 0)) == 0
    trip_state["advise_rounds"] = int(trip_state.get("advise_rounds", 0)) + 1

    # The language instruction is the responder's: advise replies must switch
    # languages by exactly the same rule as final answers.
    from app.agent.nodes.responder import _language_instruction

    try:
        response = await call_model(
            ModelRole.EXECUTOR,
            [
                SystemMessage(
                    content=ADVISOR_PROMPT.format(
                        capabilities_line=CAPABILITIES_LINE if first_round else "",
                        language_instruction=_language_instruction(
                            state.get("detected_language", "en")
                        ),
                    )
                ),
                HumanMessage(content="\n\n".join(context_lines)),
            ],
            purpose="advise",
            settings=cfg,
            temperature=cfg.llm_responder_temperature,
        )
        answer = str(response.content).strip()
    except ExternalServiceError as exc:
        logger.warning("agent.advise_failed", error=exc.message)
        # A canned but honest fallback: the advisory turn is cheap by design,
        # so if even its single call failed, ask the highest-value question
        # rather than surfacing an error for a conversational turn.
        wanted = ask_about[0] if ask_about else "what matters most to you for this trip"
        answer = (
            f"{destination} is a great choice! 🌏 To point you the right way, "
            f"could you tell me {wanted}?"
        )

    logger.info(
        "agent.advised",
        destination=destination,
        round=trip_state["advise_rounds"],
        asked=len(ask_about),
        grounded=bool(guide_block),
    )

    return AgentState(
        final_response=answer,
        status="completed",
        trip_state=trip_state,
        messages=[AIMessage(content=answer)],
    )


async def _light_retrieval(
    retriever: MultiHopRetriever | None,
    state: AgentState,
    destination: str,
) -> str:
    """Run the one-hop orientation retrieval, degrading to nothing on failure.

    Args:
        retriever: The retriever, or None when RAG is not configured.
        state: Current graph state, for the goal text.
        destination: Where to orient.

    Returns:
        A prompt block of guide material plus the district list, or an empty
        string when nothing could be retrieved.
    """
    if retriever is None or not destination:
        return ""

    try:
        result = await retriever.retrieve(
            state.get("goal") or f"what kind of trips suit {destination}",
            destination,
            intent="general",
            max_hops=1,
        )
    # Deliberately broad: retrieval failure must degrade an advisory turn,
    # never fail it - the fallback prompt branch handles the empty case.
    except Exception:
        logger.warning("agent.advise_retrieval_failed", destination=destination, exc_info=True)
        return ""

    record_rag_hops(len(result.hops))

    if result.is_empty and not result.districts_considered:
        return ""

    parts = []
    if result.districts_considered:
        parts.append(
            "AREAS THE GUIDE LISTS FOR THIS DESTINATION: "
            + ", ".join(result.districts_considered[:12])
        )
    block = result.as_context_block(max_chars=3000)
    if block:
        parts.append(block)
    return "\n\n".join(parts)


def _already_known(state: AgentState, trip_state: dict[str, object]) -> str:
    """Render everything already established, so it is never asked again.

    Args:
        state: Current graph state, for constraints and memory.
        trip_state: The slot ledger.

    Returns:
        A bulleted block, possibly empty.
    """
    lines = []
    labels = (
        ("duration_days", "trip length (days)"),
        ("travel_window", "when"),
        ("start_date", "start date"),
        ("origin", "travelling from"),
        ("party", "party"),
        ("budget_tier", "budget"),
        ("priorities", "priorities"),
    )
    for key, label in labels:
        value = trip_state.get(key)
        if value:
            rendered = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
            lines.append(f"- {label}: {rendered}")
    for constraint in state.get("constraints") or []:
        lines.append(f"- requirement: {constraint}")
    if state.get("memory_block"):
        lines.append(str(state["memory_block"]))
    return "\n".join(lines)
