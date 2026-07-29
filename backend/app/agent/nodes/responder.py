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

from app.agent.state import AgentState
from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.services.llm import ModelRole, get_model, invoke_with_retry

logger = get_logger(__name__)

# Language names for the reply instruction. A BCP-47 tag alone produces less
# reliable compliance than naming the language.
LANGUAGE_NAMES: Final[dict[str, str]] = {
    "en": "English", "ja": "Japanese", "zh": "Chinese", "ko": "Korean",
    "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
    "pt": "Portuguese", "ru": "Russian", "ar": "Arabic", "hi": "Hindi",
    "bn": "Bengali", "ta": "Tamil", "te": "Telugu", "mr": "Marathi",
    "id": "Indonesian", "ms": "Malay", "th": "Thai", "vi": "Vietnamese",
    "nl": "Dutch", "pl": "Polish", "tr": "Turkish", "sv": "Swedish",
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

WRITE SOMETHING WORTH READING
- A 2-day itinerary should be organised by day, then morning / afternoon / \
  evening.
- Group things by neighbourhood so the traveller is not crossing the city \
  repeatedly. Say which district each suggestion is in.
- Include the practical bits that matter: rough timings, when to book, what \
  the weather means for the plan.
- Warm and direct. No filler, no restating the question back, no numbered \
  lists of caveats.

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
    if state.get("start_date"):
        context_lines.append(
            f"DATES: {state['start_date']} to {state.get('end_date') or state['start_date']}"
        )
    if state.get("constraints"):
        context_lines.append(
            "HARD REQUIREMENTS (non-negotiable): " + "; ".join(state["constraints"])
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

    try:
        model = get_model(
            ModelRole.EXECUTOR, settings=cfg, temperature=cfg.llm_responder_temperature
        )
        response = await invoke_with_retry(
            model,
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
            messages=[AIMessage(content="Sorry - something went wrong writing that up. "
                                        "Please try again in a moment.")],
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
        status="partial" if state.get("stopped_because") == "replan_budget_exhausted"
        else "completed",
        messages=[AIMessage(content=answer)],
    )


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

    Args:
        state: Current graph state.

    Returns:
        A partial state update with an honest failure message.
    """
    logger.warning("agent.no_findings", run_id=state.get("run_id"))

    destination = state.get("destination")
    where = f" about {destination}" if destination else ""
    message = (
        f"I ran into trouble gathering information{where} just now - the travel data "
        "sources I rely on did not respond. Could you try again in a moment? If it keeps "
        "happening, tell me what matters most for this trip and I will work from that."
    )

    return AgentState(
        final_response=message,
        status="failed",
        stopped_because="no_usable_findings",
        messages=[AIMessage(content=message)],
    )


# Specific, checkable claims: a proper noun followed by a venue-like word, or
# a price. Deliberately narrow - the aim is to catch invented specifics, not
# to police ordinary prose.
_CLAIM_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(
        r"\b([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+){0,3})\s+"
        r"(Restaurant|Cafe|Café|Temple|Shrine|Museum|Gallery|Hotel|Hostel|Market|Park|Garden)\b"
    ),
    re.compile(r"\b(?:¥|\$|€|£)\s?\d[\d,.]*\b"),
]


def _find_ungrounded_claims(answer: str, findings: str) -> list[str]:
    """Find specific claims in the answer with no support in the findings.

    A heuristic, not a proof. It looks for named venues and prices in the
    draft and checks whether the same string appears anywhere in the gathered
    evidence. Its purpose is to surface a *rate* of unsupported specifics in
    the admin dashboard - a run with several is worth a look - rather than to
    verify any individual sentence.

    Args:
        answer: The composed reply.
        findings: Everything the executor gathered.

    Returns:
        Claim fragments that did not appear in the findings.
    """
    haystack = findings.lower()
    unsupported: list[str] = []

    for pattern in _CLAIM_PATTERNS:
        for match in pattern.finditer(answer):
            claim = match.group(0).strip()
            # Check the distinctive part - the proper noun - rather than the
            # whole phrase, since the answer may reword the surrounding text.
            probe = (match.group(1) if match.groups() else claim).strip().lower()
            if len(probe) < 4:
                continue
            if probe not in haystack:
                unsupported.append(claim)

    return list(dict.fromkeys(unsupported))
