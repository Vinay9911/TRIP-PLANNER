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

import asyncio
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.places import extract_places
from app.agent.state import AgentState
from app.agent.trip_state import disabled_services, missing_slots
from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.rag.retriever import MultiHopRetriever
from app.services.geocoding import geocode_centre, geocode_landmark
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

    guide_block, suggested_options = await _light_retrieval(retriever, state, destination)

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

    # What to show is taken from the answer, not from the retrieval that fed
    # it. The district list is inconsistent as a source of visuals - measured,
    # it gives Kyoto six real areas, Jaipur the single themed page "Forts and
    # palaces", and Goa nothing at all - and a theme cannot be photographed or
    # pinned, which is how a reply about Jaipur ended up with one stock photo
    # of clouds and no map. The prose named City Palace and two temples.
    shown = await extract_places(answer, destination, settings=cfg) or suggested_options

    return AgentState(
        final_response=answer,
        status="completed",
        trip_state=trip_state,
        suggested_options=shown,
        suggested_places=await _locate_options(shown, destination, settings=cfg),
        messages=[AIMessage(content=answer)],
    )


async def _locate_options(
    names: list[str], destination: str, *, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Put coordinates on the places an advisory turn is offering.

    **Why an advisory turn needs a map at all.** Photographs and pins were
    wired only to the finished itinerary, which is the rarest thing the agent
    produces - most conversations are two or three advisory turns that never
    reach a full plan. So the features existed and almost nobody ever saw
    them, and the commonest complaint about this app was that it had no
    pictures and no map. It had both; they were behind the wrong door.

    Offering "the Adirondacks, the Catskills, the Hudson Valley" as three words
    asks the traveller to already know where those are relative to each other.
    Three pins answer that instantly, and choosing between regions is exactly
    the decision this gear exists to support.

    Args:
        names: Display names the advisor offered.
        destination: The surrounding place, used to disambiguate and to
            reject a match on the wrong continent.
        settings: Settings override, for tests.

    Returns:
        One entry per name that resolved, each with `name`, `latitude` and
        `longitude`. Names that do not resolve are simply absent - the chips
        still work without a pin.
    """
    if not names or not destination:
        return []

    # Without the POI geocoder there is nothing to place these with, so return
    # before making any network call at all. This is also what keeps the test
    # suite offline: no key configured means no request, and the advisory turn
    # still answers - it just answers without pins.
    if not (settings or get_settings()).geoapify_api_key.get_secret_value():
        return []

    centre = await geocode_centre(destination, settings=settings)

    located = await asyncio.gather(
        *(geocode_landmark(name, near=destination, centre=centre) for name in names),
        return_exceptions=True,
    )

    return [
        {"name": name, "latitude": found[0], "longitude": found[1]}
        for name, found in zip(names, located, strict=True)
        if isinstance(found, tuple)
    ]


#: Capped well below the 12 sent to the prompt (`_light_retrieval`'s internal
#: slice) - a reply that offers 3-4 ways to experience a place should not
#: turn into a wall of a dozen clickable chips underneath it.
MAX_SUGGESTED_OPTIONS = 6


def _display_name(title: str) -> str:
    """Turn a Wikivoyage article title into something readable.

    Titles for sub-areas are paths - `Mumbai/Colaba and Fort`,
    `Kyoto/Higashiyama` - and passing them through verbatim leaked into a real
    reply as "explore the colonial buildings in Mumbai/Colaba and Fort". The
    slash is an artefact of how the corpus is organised, not part of the
    place's name, and it makes the agent look like it is reciting a database.

    Args:
        title: A Wikivoyage article title.

    Returns:
        The last path segment, which is the human-facing area name.
    """
    return title.split("/")[-1].strip() or title


async def _light_retrieval(
    retriever: MultiHopRetriever | None,
    state: AgentState,
    destination: str,
) -> tuple[str, list[str]]:
    """Run the one-hop orientation retrieval, degrading to nothing on failure.

    Args:
        retriever: The retriever, or None when RAG is not configured.
        state: Current graph state, for the goal text.
        destination: Where to orient.

    Returns:
        A tuple of (prompt block of guide material plus the district list,
        real district names for the frontend to render as clickable options).
        Both empty when nothing could be retrieved.
    """
    if retriever is None or not destination:
        return "", []

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
        return "", []

    record_rag_hops(len(result.hops))

    if result.is_empty and not result.districts_considered:
        return "", []

    # Deduplicated after prettifying, since "Mumbai/South" and "South" would
    # otherwise both survive as separate-looking options.
    display_names: list[str] = []
    for title in result.districts_considered:
        pretty = _display_name(title)
        if pretty not in display_names:
            display_names.append(pretty)

    parts = []
    if display_names:
        parts.append(
            "AREAS THE GUIDE LISTS FOR THIS DESTINATION (use these names exactly "
            "as written here - never with a slash or a city prefix): "
            + ", ".join(display_names[:12])
        )
    block = result.as_context_block(max_chars=3000)
    if block:
        parts.append(block)
    return "\n\n".join(parts), display_names[:MAX_SUGGESTED_OPTIONS]


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
