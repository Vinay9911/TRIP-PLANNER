"""Responder node: turn findings into the traveller's answer.

The last node, and the only one whose output the traveller reads. Three things
are enforced here.

**Grounding.** The responder is given the executor's findings and told to build
the answer from them. Recommending a restaurant that appeared in no retrieval
is the failure mode that makes an agent untrustworthy, because it is invisible
to the user - a hallucinated venue looks exactly like a real one until they
turn up and it is not there. `_find_ungrounded_claims` post-checks the draft
for specific claims with no support in the gathered evidence and logs them for
the admin trace.

**Honesty about gaps.** When a tool degraded or a guardrail stopped the run
early, the answer says so. A partial itinerary labelled partial is useful; one
presented as complete is misleading.

**Language.** The reply is written in whatever language the traveller wrote
in. This is where the multilingual requirement actually lands: retrieval ran
in English against an English corpus, and only the final composition switches
language. Decoupling the two means a Japanese-speaking traveller gets the full
quality of the English guide corpus rather than whatever happens to exist in
Japanese.
"""

from __future__ import annotations

import re
from typing import Final

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.itinerary import ITINERARY_INSTRUCTIONS, Itinerary
from app.agent.state import AgentState
from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.services.llm import ModelRole, call_model, structured_call

logger = get_logger(__name__)

# Language names for the reply instruction. A BCP-47 tag alone produces less
# reliable compliance than naming the language.
LANGUAGE_NAMES: Final[dict[str, str]] = {
    "en": "English",
    "ja": "Japanese",
    "zh": "Chinese",
    "ko": "Korean",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "ru": "Russian",
    "ar": "Arabic",
    "hi": "Hindi",
    "bn": "Bengali",
    "ta": "Tamil",
    "te": "Telugu",
    "mr": "Marathi",
    "id": "Indonesian",
    "ms": "Malay",
    "th": "Thai",
    "vi": "Vietnamese",
    "nl": "Dutch",
    "pl": "Polish",
    "tr": "Turkish",
    "sv": "Swedish",
}

RESPONDER_PROMPT = """\
You are a knowledgeable, warm travel assistant writing the final answer.

GROUND EVERYTHING IN THE FINDINGS
Build your answer from the research findings below. Name specific places, \
districts and practical details that appear in them. If you want to say \
something the findings do not support, either leave it out or mark it \
clearly as general advice rather than something you checked.

Never invent a venue name, address, price or opening time. A traveller who \
turns up at a restaurant you made up has been failed in a way they cannot \
detect until it is too late.

BE HONEST ABOUT GAPS
If the findings note that a data source was unavailable, say so briefly and \
move on. If flight or hotel figures are marked as simulated, state plainly \
that they are illustrative and not bookable.

When explaining a gap, speak like a person, not a system log. Never say \
"language model provider", "API", "rate limit", "tool", or name an internal \
step - and just as importantly, never wave at the reason with something \
vague like "due to the limitations" or "for technical reasons" either. If \
you cannot state the reason in plain words, drop the explanation and just \
say what to do about it. "I couldn't check your saved preferences this \
time - just mention anything that matters and I'll use it" is what a \
traveller needs to hear; a vague nod at a cause you are not naming is not \
more honest than naming none at all, it just sounds evasive.

WRITE SOMETHING WORTH READING
- A 2-day itinerary should be organised by day, then morning / afternoon / \
  evening.
- Group things by neighbourhood so the traveller is not crossing the city \
  repeatedly. Say which district each suggestion is in.
- Include the practical bits that matter: rough timings, when to book, what \
  the weather means for the plan.
- Warm, friendly and direct - a knowledgeable friend, not a brochure. No \
  filler, no restating the question back, no numbered lists of caveats.
- Use emoji as visual signposts, not decoration: one on each day heading and \
  on section markers like flights ✈️, stays 🏨, weather 🌦, food 🍽 - and \
  almost never mid-sentence. An itinerary should scan like a well-labelled \
  map, not a greetings card.

RESPECT GEOGRAPHY AND WHAT THEY SAID MATTERS MOST
For a destination made of several distinct areas rather than one walkable \
city - an island, a region, a country - do not sequence days as if distance \
were free. Moving between areas takes real time; a 5-day trip visiting four \
places that are each a multi-hour drive or a boat crossing apart is not a \
relaxed itinerary, it is a transit schedule. Pick 2-3 areas that are \
actually near each other and give each enough time to be worth the trip \
there, rather than touring the whole map.

If the traveller told you what matters most (their PRIORITIES, listed \
below), every day should serve that. A day trip to a volcano does not \
belong in an itinerary they asked to focus on coastlines, however well \
regarded the volcano is - find another coastal option, or say plainly that \
you are stepping outside their stated priority and why it is worth it \
this once, rather than including it silently.

RESPECT WHAT THEY SWITCHED OFF
If the context lists services the traveller switched off, do not include \
those sections at all - not even a mention that you skipped them.

HONOUR THEIR REQUIREMENTS
Any hard requirement listed below is not negotiable. If you could not verify \
that somewhere meets it, say so rather than quietly recommending it anyway. \
Do not recite their stored preferences back to them - just plan around them.

{language_instruction}\
"""


async def responder_node(state: AgentState, *, settings: Settings | None = None) -> AgentState:
    """Compose the final answer from the accumulated findings.

    Args:
        state: Current graph state. Requires `completed_steps`.
        settings: Settings override, for tests.

    Returns:
        A partial state update carrying `final_response` and the appended
        assistant message.
    """
    cfg = settings or get_settings()

    # A clarifying question short-circuits everything: it was already composed
    # by the understanding node, in the traveller's language.
    if state.get("needs_clarification") and state.get("final_response"):
        question = str(state["final_response"])
        return AgentState(
            final_response=question,
            status="clarifying",
            messages=[AIMessage(content=question)],
        )

    completed = state.get("completed_steps", [])
    successful = [result for result in completed if result.succeeded and result.output]

    if not successful:
        return _no_findings_response(state)

    findings = "\n\n".join(
        f"### {result.step.description}\n{result.output}" for result in successful
    )

    degraded_notes = [
        f"- {result.step.description}: {result.error}"
        for result in completed
        if not result.succeeded and result.error
    ]

    context_lines = [f"TRAVELLER'S REQUEST: {state.get('goal', '')}"]
    if state.get("destination"):
        context_lines.append(f"DESTINATION: {state['destination']}")

    scoped_service = state.get("scoped_service") or "none"
    if scoped_service != "none":
        context_lines.append(
            f"SCOPED REQUEST: answer the {scoped_service} question only - no day-by-day "
            "itinerary, no unrelated sections, no 'in the morning / afternoon / evening' "
            "structure. A short, direct answer to what was actually asked."
        )
    if state.get("start_date"):
        context_lines.append(
            f"DATES: {state['start_date']} to {state.get('end_date') or state['start_date']}"
        )

    trip_state = state.get("trip_state") or {}
    if trip_state.get("priorities"):
        # Stated explicitly rather than left to survive inside `goal`'s prose
        # - a live run showed the model naming this correctly in its opening
        # line ("Given the focus on rugged coastlines...") and then including
        # a day built around a volcano anyway. Knowing the priority and
        # holding to it for every day are different things; this line is for
        # the second one.
        context_lines.append(
            "PRIORITIES (what they said matters most - every day should serve "
            "this, see RESPECT GEOGRAPHY above): " + ", ".join(map(str, trip_state["priorities"]))
        )
    if state.get("constraints"):
        context_lines.append(
            "HARD REQUIREMENTS (non-negotiable): " + "; ".join(state["constraints"])
        )

    from app.agent.trip_state import disabled_services

    switched_off = disabled_services(state.get("focus"))
    if switched_off:
        context_lines.append(
            "SERVICES THE TRAVELLER SWITCHED OFF (omit these sections entirely): "
            + ", ".join(switched_off)
        )

    if state.get("memory_block"):
        context_lines.append(state["memory_block"])

    context_lines.append(f"\nRESEARCH FINDINGS:\n{findings}")

    if degraded_notes:
        context_lines.append(
            "\nSTEPS THAT DID NOT COMPLETE (acknowledge briefly if relevant):\n"
            + "\n".join(degraded_notes)
        )

    if state.get("stopped_because") == "replan_budget_exhausted":
        context_lines.append(
            "\nNOTE: research was cut short by an internal limit. Answer with what is "
            "here and offer to look into anything missing if they ask."
        )

    # A multi-day trip is returned as structured days rather than prose, so
    # the interface can render cards, photographs and map pins against real
    # places. Scoped asks ("find me flights") stay prose - there is nothing to
    # lay out, and forcing them through a day schema would be absurd.
    wants_itinerary = (
        scoped_service == "none"
        and int((state.get("trip_state") or {}).get("duration_days") or 0) >= 1
    )
    if wants_itinerary:
        itinerary = await _compose_itinerary(state, context_lines, cfg)
        if itinerary is not None:
            return _itinerary_response(state, itinerary)
        # Fell through: structured generation failed, so carry on and write
        # prose rather than failing a turn that has good research behind it.
        logger.info("agent.itinerary_fallback_to_prose", run_id=state.get("run_id"))

    try:
        response = await call_model(
            ModelRole.EXECUTOR,
            [
                SystemMessage(
                    content=RESPONDER_PROMPT.format(
                        language_instruction=_language_instruction(
                            state.get("detected_language", "en")
                        )
                    )
                ),
                HumanMessage(content="\n".join(context_lines)),
            ],
            purpose="compose_response",
            settings=cfg,
            temperature=cfg.llm_responder_temperature,
        )
        answer = str(response.content).strip()

    except ExternalServiceError as exc:
        logger.warning("agent.response_failed", error=exc.message)
        return AgentState(
            final_response=(
                "I gathered the research for your trip but could not write it up just now. "
                "Please try again in a moment."
            ),
            status="failed",
            errors=[{"node": "responder", "error": exc.message}],
            messages=[
                AIMessage(
                    content="Sorry - something went wrong writing that up. "
                    "Please try again in a moment."
                )
            ],
        )

    ungrounded = _find_ungrounded_claims(answer, findings)
    if ungrounded:
        # Logged rather than blocked. Rewriting the answer would cost another
        # model call on a rate-limited tier, and the check is heuristic enough
        # that false positives are likely - so it feeds the admin trace as a
        # quality signal instead of gating the response.
        logger.warning(
            "agent.possible_ungrounded_claims",
            run_id=state.get("run_id"),
            count=len(ungrounded),
            samples=ungrounded[:5],
        )

    logger.info(
        "agent.responded",
        length=len(answer),
        language=state.get("detected_language"),
        steps_used=len(successful),
    )

    return AgentState(
        final_response=answer,
        status="partial"
        if state.get("stopped_because") == "replan_budget_exhausted"
        else "completed",
        messages=[AIMessage(content=answer)],
    )


async def _compose_itinerary(
    state: AgentState, context_lines: list[str], cfg: Settings
) -> Itinerary | None:
    """Produce the plan as structured days.

    Args:
        state: Current graph state.
        context_lines: The same context the prose composer would receive.
        cfg: Application settings.

    Returns:
        The itinerary, or None if structured generation failed - in which case
        the caller falls back to prose rather than losing the turn.
    """
    try:
        itinerary = await structured_call(
            ModelRole.EXECUTOR,
            [
                SystemMessage(
                    content=RESPONDER_PROMPT.format(
                        language_instruction=_language_instruction(
                            state.get("detected_language", "en")
                        )
                    )
                    + "\n\n"
                    + ITINERARY_INSTRUCTIONS
                ),
                HumanMessage(content="\n".join(context_lines)),
            ],
            Itinerary,
            purpose="compose_itinerary",
            settings=cfg,
        )
    except ExternalServiceError as exc:
        logger.warning("agent.itinerary_failed", error=exc.message)
        return None

    if not itinerary.days:
        return None
    return itinerary


def _itinerary_response(state: AgentState, itinerary: Itinerary) -> AgentState:
    """Package a structured itinerary as a turn result.

    A plain-text rendering travels alongside it in `final_response`, because
    that is what gets stored as the conversation message and read back on a
    later turn. Without it, reopening a past conversation would show an empty
    assistant bubble.

    Args:
        state: Current graph state.
        itinerary: The composed plan.

    Returns:
        A partial state update.
    """
    text = render_itinerary_text(itinerary)

    logger.info(
        "agent.responded_structured",
        days=len(itinerary.days),
        items=len(itinerary.all_items()),
        language=state.get("detected_language"),
    )

    return AgentState(
        final_response=text,
        itinerary=itinerary.model_dump(mode="json"),
        status="partial"
        if state.get("stopped_because") == "replan_budget_exhausted"
        else "completed",
        messages=[AIMessage(content=text)],
    )


def render_itinerary_text(itinerary: Itinerary) -> str:
    """Render a structured itinerary as readable markdown.

    Used for the stored message and as the reply for any client that does not
    render the structured form. Kept deterministic - no model call - so the
    text and the cards can never disagree about what the plan is.

    Args:
        itinerary: The plan.

    Returns:
        Markdown text.
    """
    lines: list[str] = []
    if itinerary.intro:
        lines.append(itinerary.intro)

    slot_labels = (
        ("morning", "Morning"),
        ("afternoon", "Afternoon"),
        ("evening", "Evening"),
    )

    for day in itinerary.days:
        when = f" · {day.date}" if day.date else ""
        lines.append(f"\n### Day {day.day_number}: {day.title}{when}")
        if day.summary:
            lines.append(day.summary)

        for item in day.all_day:
            lines.append(f"- **{item.name}** — {item.description}".rstrip(" —"))

        for attribute, label in slot_labels:
            items = getattr(day, attribute)
            if not items:
                continue
            lines.append(f"\n**{label}**")
            for item in items:
                where = f" ({item.district})" if item.district else ""
                detail = f" — {item.description}" if item.description else ""
                lines.append(f"- **{item.name}**{where}{detail}")

    if itinerary.practical_notes:
        lines.append("\n### Good to know")
        lines.extend(f"- {note}" for note in itinerary.practical_notes)

    if itinerary.gaps:
        lines.append("")
        lines.extend(f"- {gap}" for gap in itinerary.gaps)

    return "\n".join(lines).strip()


def _language_instruction(language: str) -> str:
    """Build the reply-language instruction.

    Args:
        language: BCP-47 tag detected from the traveller's message.

    Returns:
        An instruction line for the system prompt.
    """
    code = (language or "en").split("-")[0].lower()
    if code == "en":
        return "Write your reply in English."

    name = LANGUAGE_NAMES.get(code)
    target = name or f"the language with code '{code}'"
    return (
        f"IMPORTANT: the traveller wrote in {target}. Write your ENTIRE reply in {target}, "
        "naturally and fluently - not translated English. Keep place names in their local "
        "form with a romanised version in brackets on first mention, so they can be typed "
        "into a map."
    )


def _no_findings_response(state: AgentState) -> AgentState:
    """Compose a reply for a run that gathered nothing usable.

    The message distinguishes *why*. An earlier version blamed "travel data
    sources" for every failure, which was actively misleading: the common cause
    on a free tier is the language model's own per-minute token allowance, and
    telling a user their data sources are down when the sources answered fine
    sends them looking in the wrong place. Worse, it hides the one thing they
    can act on - waiting a minute, or adding another API key.

    Args:
        state: Current graph state.

    Returns:
        A partial state update with an honest failure message.
    """
    completed = state.get("completed_steps", [])
    errors = " ".join(result.error.lower() for result in completed if result.error)

    # A daily allowance and a per-minute burst are different problems and
    # deserve different advice. Telling someone to "try again in a minute"
    # when the budget resets in hours is not a small inaccuracy - they will
    # try again, fail again, and conclude the product is broken.
    daily_exhausted = "daily token allowance" in errors
    rate_limited = "rate limit" in errors or daily_exhausted

    logger.warning(
        "agent.no_findings",
        run_id=state.get("run_id"),
        cause="rate_limit" if rate_limited else "tool_failures",
        steps_attempted=len(completed),
    )

    destination = state.get("destination")
    where = f" for {destination}" if destination else ""

    if daily_exhausted:
        message = (
            f"I've used up today's planning allowance, so I couldn't finish{where}. "
            "It resets on a rolling window over the next few hours — everything "
            "we've discussed is saved, so you can pick this up right where we "
            "left off."
        )
        stopped = "llm_daily_limit"
    elif rate_limited:
        message = (
            "I hit my request limit part-way through planning"
            f"{where}, so I could not finish. Please try again in a minute - the "
            "limit resets quickly."
        )
        stopped = "llm_rate_limited"
    else:
        message = (
            f"I ran into trouble gathering information{where} just now - the travel "
            "data sources I rely on did not respond. Could you try again in a moment? "
            "If it keeps happening, tell me what matters most for this trip and I will "
            "work from that."
        )
        stopped = "no_usable_findings"

    return AgentState(
        final_response=message,
        status="failed",
        stopped_because=stopped,
        messages=[AIMessage(content=message)],
    )


# A named venue: a run of capitalised words followed by a venue noun.
# Note the capture deliberately over-reaches - "Visit Kiyomizu Temple" yields
# "Visit Kiyomizu" - which `_name_is_supported` compensates for by testing
# progressively shorter suffixes.
#
# Both apostrophe forms are accepted because venue names arrive with either:
# some sources use the ASCII apostrophe, others the typographic one (U+2019).
# The latter is written as an escape rather than pasted literally, since the
# two are visually identical in most editors and a future reader would have
# no way to tell which is present.
_APOSTROPHES = "'" + chr(0x2019)
_VENUE_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"([A-Z][\w{_APOSTROPHES}-]+(?:\s+[A-Z][\w{_APOSTROPHES}-]+){{0,3}})\s+"
    r"(Restaurant|Cafe|Café|Temple|Shrine|Museum|Gallery|Hotel|Hostel"
    r"|Market|Park|Garden)\b"
)

# A price. No leading `\b`: a word boundary cannot exist between a space and a
# currency symbol, since neither is a word character - an easy way to write a
# pattern that silently never matches.
_PRICE_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?:[¥$€£]|USD|JPY|EUR|GBP)\s?\d[\d,.]*")


def _name_is_supported(name: str, haystack: str) -> bool:
    """Whether any meaningful part of a venue name appears in the evidence.

    The venue pattern greedily absorbs preceding capitalised words, so a
    sentence beginning "Visit Kiyomizu Temple" captures "Visit Kiyomizu".
    Testing the whole phrase would report a grounded venue as invented.

    So progressively shorter *suffixes* are tested - "Visit Kiyomizu", then
    "Kiyomizu" - and the name counts as supported if any of them appears.
    Suffixes rather than prefixes because the distinctive part of a name sits
    at its end, next to the venue noun.

    Args:
        name: The captured capitalised run.
        haystack: Lower-cased findings to search.

    Returns:
        True if the name appears to be supported by the findings.
    """
    words = name.split()

    for start in range(len(words)):
        candidate = " ".join(words[start:]).lower().strip()
        # Skip fragments too short to be distinctive; "the" matching anything
        # would make the whole check useless.
        if len(candidate) < 4:
            continue
        if candidate in haystack:
            return True

    return False


def _find_ungrounded_claims(answer: str, findings: str) -> list[str]:
    """Find specific claims in the answer with no support in the findings.

    A heuristic, not a proof. It looks for named venues and prices in the
    draft and checks whether they appear in the gathered evidence. Its purpose
    is to surface a *rate* of unsupported specifics for the admin dashboard -
    a run with several is worth a look - rather than to verify any individual
    sentence.

    Args:
        answer: The composed reply.
        findings: Everything the executor gathered.

    Returns:
        Claim fragments that did not appear in the findings.
    """
    haystack = findings.lower()
    unsupported: list[str] = []

    for match in _VENUE_PATTERN.finditer(answer):
        if not _name_is_supported(match.group(1), haystack):
            unsupported.append(match.group(0).strip())

    for match in _PRICE_PATTERN.finditer(answer):
        price = match.group(0).strip()
        # Compared with whitespace removed, since the findings may write
        # "¥ 400" where the answer writes "¥400".
        if price.replace(" ", "").lower() not in haystack.replace(" ", ""):
            unsupported.append(price)

    return list(dict.fromkeys(unsupported))
