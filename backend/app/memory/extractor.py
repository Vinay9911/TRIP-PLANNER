"""Deciding what is worth remembering.

This module answers the hardest question in the memory design: *what should be
stored?* Storing everything is a chat log with extra steps; storing too little
means the traveller repeats themselves every session. The answer here has
three layers, ordered cheapest-first so the expensive one runs rarely.

**Layer 1 - a free heuristic gate.**

Most conversational turns contain nothing durable. "What's the weather like?"
and "sounds good, thanks" have no facts worth carrying into a future trip.
Running a language model over every such message would burn a rate-limited
daily quota on producing empty lists. So a cheap lexical pre-check runs first
and skips the model entirely unless the message shows some sign of stating a
preference, constraint or personal fact. This is a *filter on cost, not on
correctness*: it is tuned to over-admit, because a false positive costs one
cheap model call while a false negative loses a fact permanently.

**Layer 2 - schema-constrained extraction.**

Messages that pass the gate go to the small utility model with a strict
Pydantic schema. The model cannot return prose, cannot invent a category, and
cannot use a subject slot outside the closed vocabulary. Most of the policy is
expressed as schema constraints rather than prompt text, because a schema is
enforced and a prompt is a suggestion.

**Layer 3 - an explicit rejection policy.**

The prompt enumerates what must *not* be stored, with examples. In testing,
this is what separates a memory system from a noise generator - without
explicit negative rules, models reliably store "user is going to Tokyo in
March" (trip-specific, useless next time) and "user seems friendly"
(unfalsifiable, unusable).

Everything here runs *after* the HTTP response has been sent, as a background
task. Memory extraction must never sit between a user and their itinerary.
"""

from __future__ import annotations

import re
from typing import Final

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.memory.schemas import CandidateMemory, ExtractionResult
from app.services.llm import ModelRole, structured_call

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Layer 1: the heuristic gate
# ---------------------------------------------------------------------------

# Signals that a message might contain a durable personal fact. Tuned to
# over-admit: a false positive costs one cheap model call, a false negative
# loses information for good.
_SIGNAL_PATTERNS: Final[list[re.Pattern[str]]] = [
    # First-person statements of state, preference or capability.
    re.compile(
        r"\b(i|we|my|our|me|us)\b.{0,40}\b("
        r"am|are|was|were|have|has|had|like|love|prefer|enjoy|hate|dislike|"
        r"avoid|need|want|can't|cannot|don't|do not|never|always|usually|"
        r"allergic|vegetarian|vegan|budget|afford)\b",
        re.IGNORECASE,
    ),
    # Explicit dietary, accessibility and travel-party vocabulary, which can
    # appear without a first-person pronoun ("no pork, please").
    re.compile(
        r"\b(vegetarian|vegan|halal|kosher|gluten|allergy|allergic|lactose|"
        r"wheelchair|accessible|mobility|stroller|pram|"
        r"pet|dog|cat|kids|children|toddler|infant|"
        r"budget|cheap|luxury|backpack|splurge|shoestring|mid-range)\b",
        re.IGNORECASE,
    ),
    # Travel history and verdicts.
    re.compile(
        r"\b(visited|been to|went to|last time|previously|last year|"
        r"loved|hated|too crowded|too touristy|disappointing)\b",
        re.IGNORECASE,
    ),
    # Pace and style.
    re.compile(
        r"\b(slow|relaxed|packed|busy|early riser|night owl|"
        r"off the beaten|hidden gem|touristy|authentic)\b",
        re.IGNORECASE,
    ),
]

# Non-Latin scripts defeat the English regexes above. Rather than maintain a
# lexicon per language, any message containing CJK, Arabic, Hebrew, Thai,
# Devanagari or Cyrillic characters is admitted to the model, which is
# multilingual. Cost is bounded because these messages are a small minority.
_NON_LATIN = re.compile(
    r"[Ѐ-ӿ֐-׿؀-ۿऀ-ॿ"
    r"฀-๿぀-ヿ㐀-䶿一-鿿가-힯]"
)

# Below this length a Latin-script message is almost always an
# acknowledgement ("ok", "thanks", "sounds good").
_MIN_LENGTH: Final[int] = 12

# A much lower floor for scripts that pack far more meaning per character.
# "私はベジタリアンです" is a complete, durable statement - "I am vegetarian" -
# in ten characters. Applying the Latin threshold to it would silently switch
# off memory extraction for exactly the users the multilingual support is for,
# and the failure would be invisible: no error, just a profile that never
# fills up.
_MIN_LENGTH_NON_LATIN: Final[int] = 4


def looks_worth_extracting(message: str) -> bool:
    """Cheap pre-check for whether a message may contain a durable fact.

    Deliberately permissive. Its only job is to skip the obvious nothing-burgers
    ("ok", "thanks", "what's the weather?") before spending a model call.

    Args:
        message: The user's message text.

    Returns:
        True if extraction should run.

    Example:
        >>> looks_worth_extracting("thanks!")
        False
        >>> looks_worth_extracting("I'm vegetarian and travelling with my dog")
        True
    """
    text = message.strip()

    # Script detection comes before the length gate, because the correct
    # length threshold depends on the script.
    if _NON_LATIN.search(text):
        # The keyword patterns below are English-only, so there is nothing
        # cheaper to test against. Admit anything of plausible length and let
        # the multilingual model decide.
        return len(text) >= _MIN_LENGTH_NON_LATIN

    if len(text) < _MIN_LENGTH:
        return False

    return any(pattern.search(text) for pattern in _SIGNAL_PATTERNS)


# ---------------------------------------------------------------------------
# Layer 3: the rejection policy
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM_PROMPT: Final[str] = """\
You extract durable facts about a traveller from a conversation, so that a \
travel planning assistant can personalise FUTURE, UNRELATED trips without \
asking the same questions again.

THE TEST FOR EVERY CANDIDATE FACT
Ask yourself: "If this person books a completely different trip, to a \
different country, in two years' time - would knowing this still help?"
If no, do not store it. This single test resolves most cases.

STORE
- Dietary needs and restrictions: vegetarian, vegan, halal, allergies.
- Accessibility and mobility needs, including travelling with a pushchair.
- Budget level as a durable trait: 'travels on a tight budget', 'happy to \
  spend on good hotels'.
- Travel style and pace: prefers slow itineraries, dislikes crowded tourist \
  sites, likes hiking, always wants a museum day.
- Who they travel with: partner, two young children, a dog.
- Stable personal attributes: home city, languages spoken.
- Travel history and verdicts: 'visited Rome in 2024 and found it too hot'.

DO NOT STORE
- Trip-specific details: dates, this destination, this hotel, this flight. \
  'Going to Tokyo in March' is useless once March has passed.
- Transient states: tired today, in a hurry, currently deciding.
- Questions the user asked. A question is not a statement of preference. \
  'Are there vegan restaurants?' does NOT establish that they are vegan.
- Anything you inferred without support. If they asked about a museum, that \
  does not make them a museum lover.
- Personal identifying information: names, emails, phone numbers, addresses, \
  passport or payment details. Never store these, even if volunteered.
- Unfalsifiable impressions: 'seems friendly', 'is indecisive'.
- Facts about anyone other than the traveller and their travel companions.

HOW TO WRITE THE CONTENT
- One atomic fact per entry. Split 'vegetarian and hates crowds' into two.
- Third person, English, self-contained: 'Traveller is vegetarian.' Write in \
  English even when the conversation is in another language, and record the \
  original language in the detected_language field. This keeps memories \
  retrievable across languages.
- No hedging. If you are unsure, lower the confidence instead of writing \
  'might be vegetarian'.

CONFIDENCE
- 0.9-1.0: stated explicitly and unambiguously.
- 0.6-0.8: clearly implied, e.g. 'I'll skip the steakhouse, I don't eat meat'.
- Below 0.6: do not return it at all.

Returning an empty list is the correct and common answer. A wrong memory is \
worse than a missing one, because it will silently distort every future trip.\
"""


async def extract_memories(
    user_message: str,
    *,
    assistant_reply: str | None = None,
    settings: Settings | None = None,
) -> ExtractionResult:
    """Extract durable facts about the traveller from one conversational turn.

    Args:
        user_message: What the user said. The primary source - facts are
            attributed to the user, not to the assistant.
        assistant_reply: The assistant's response, supplied only as context so
            the model can resolve references like "yes, that one". Facts are
            never extracted from the assistant's own suggestions, which would
            otherwise let the system invent preferences and then believe them.
        settings: Settings override, for tests.

    Returns:
        An `ExtractionResult`. Empty when nothing durable was found, which is
        the common case.

    Raises:
        ExternalServiceError: If the model provider fails. Callers run this in
            the background and should log rather than propagate.
    """
    cfg = settings or get_settings()

    if not looks_worth_extracting(user_message):
        logger.debug("memory.extraction_skipped", reason="heuristic_gate")
        return ExtractionResult(memories=[], detected_language="en")

    context = f"USER SAID:\n{user_message}"
    if assistant_reply:
        # Truncated: the reply is context for disambiguation only, and a long
        # itinerary would dominate the prompt and invite the model to extract
        # facts from the assistant's own suggestions.
        context += (
            "\n\nASSISTANT REPLIED (context only - do NOT extract facts from this):\n"
            f"{assistant_reply[:600]}"
        )

    try:
        result = await structured_call(
            ModelRole.UTILITY,
            [SystemMessage(content=EXTRACTION_SYSTEM_PROMPT), HumanMessage(content=context)],
            ExtractionResult,
            purpose="extract_memories",
            settings=cfg,
        )
    except ExternalServiceError:
        logger.warning("memory.extraction_failed", exc_info=True)
        raise

    kept = _apply_confidence_floor(result.memories, cfg.memory_min_confidence)

    logger.info(
        "memory.extracted",
        proposed=len(result.memories),
        kept=len(kept),
        language=result.detected_language,
    )
    return ExtractionResult(memories=kept, detected_language=result.detected_language)


def _apply_confidence_floor(
    candidates: list[CandidateMemory], floor: float
) -> list[CandidateMemory]:
    """Drop candidates the extractor was not confident enough about.

    Enforced in code as well as in the prompt. Models routinely ignore a
    stated numeric threshold, and a low-confidence memory is not merely
    useless - it will be injected into future planning turns as if it were
    established fact.

    Args:
        candidates: Candidates returned by the model.
        floor: Minimum acceptable confidence.

    Returns:
        Candidates meeting the threshold.
    """
    kept: list[CandidateMemory] = []

    for candidate in candidates:
        if candidate.confidence < floor:
            logger.debug(
                "memory.candidate_rejected",
                reason="below_confidence_floor",
                confidence=candidate.confidence,
                floor=floor,
                content=candidate.content[:80],
            )
            continue
        kept.append(candidate)

    return kept
