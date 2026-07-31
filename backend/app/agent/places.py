"""Pull the real places out of a reply, so they can be shown rather than read.

**Why this exists.** Photographs and map pins were driven by the retriever's
district list, and that list is not what the agent talks about. Measured
against the live corpus:

===========  ==================================================
Kyoto        6 real districts - works well
Jaipur       1 entry, ``Jaipur/Forts and palaces`` - a themed
             subpage, not a place. One generic stock photo, no
             map, because a category cannot be photographed or
             pinned.
Goa          0 entries - nothing shown at all
===========  ==================================================

The same Jaipur reply named City Palace, the Birla Temple and Govind Dev Ji
Temple in its prose: three real places, each with a photograph and a
coordinate. The information was there, in the answer, and the interface was
looking somewhere else for it.

So the source of truth for "what should be on screen" is the answer itself.
That also makes this work for every gear and every subject - the hotels and
restaurants an itinerary names are extracted by exactly the same pass, with no
per-category plumbing.

**Cost.** One call on the smallest tier against a reply of a few hundred
words: roughly 300 tokens in, 60 out. Deliberately not the executor - this is
extraction against a strict schema, which is what the utility tier is for.

**Failure is silent by design.** No places extracted means a reply with no
pictures, which is how it looked before this existed. Nothing here may fail a
turn that has already produced a good answer.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.services.llm import ModelRole, structured_call

logger = get_logger(__name__)

#: Upper bound on what one reply can put on screen.
#:
#: An advisory turn names three or four places; an itinerary can name thirty.
#: Eight is about what fits as cards without the reply becoming a gallery with
#: a paragraph attached, and it bounds the geocoding and image lookups that
#: follow - each extracted name costs one of each.
MAX_EXTRACTED_PLACES = 8

EXTRACTION_PROMPT = """\
List the real, specific places named in this travel reply - the ones a \
traveller could point at on a map or see a photograph of.

INCLUDE named landmarks, monuments, temples, forts, palaces, museums, parks, \
beaches, markets, neighbourhoods, towns and cities. Also named hotels and \
named restaurants.

EXCLUDE anything that is a category rather than a place. "Forts and palaces", \
"Cultural Heritage", "the backwaters", "street food" and "old town" are \
themes, not places - they cannot be photographed or pinned, and offering them \
as if they could is the mistake this is fixing. Exclude the destination \
itself, since the whole reply is already about it.

Write each name as a traveller would search for it: "City Palace", not "the \
City Palace in the heart of the city". Keep the order they appear in. If the \
reply names none, return an empty list rather than inventing any.\
"""


class MentionedPlaces(BaseModel):
    """Places named in a reply."""

    places: list[str] = Field(
        default_factory=list,
        description="Specific, real place names in the order they appear. May be empty.",
    )


async def extract_places(
    text: str, destination: str, *, settings: Settings | None = None
) -> list[str]:
    """Find the specific places a reply names.

    Args:
        text: The composed reply shown to the traveller.
        destination: The trip's destination, excluded from the result because
            a card for the place the whole reply is about adds nothing.
        settings: Settings override, for tests.

    Returns:
        Up to `MAX_EXTRACTED_PLACES` names, deduplicated, in the order they
        appear. Empty when nothing was named or the call failed - a reply
        without pictures is the previous behaviour, not a broken one.
    """
    if not text.strip():
        return []

    try:
        result = await structured_call(
            ModelRole.UTILITY,
            [
                SystemMessage(content=EXTRACTION_PROMPT),
                HumanMessage(content=f"DESTINATION: {destination}\n\nREPLY:\n{text}"),
            ],
            MentionedPlaces,
            purpose="extract_places",
            settings=settings or get_settings(),
        )
    except ExternalServiceError as exc:
        logger.info("agent.place_extraction_failed", error=exc.message)
        return []

    seen: set[str] = set()
    places: list[str] = []
    for raw in result.places:
        # The curly apostrophe is deliberate and is why this line is exempt
        # from the ambiguous-character rule: models emit U+2019 as often as
        # the straight quote, and a name left with one trailing would be sent
        # to the geocoder and the image search verbatim.
        name = raw.strip(" .,'\"’")  # noqa: RUF001
        # The destination itself is filtered here rather than in the prompt
        # alone, because models include it anyway about a third of the time
        # and a card captioned "Jaipur" on a reply about Jaipur is noise.
        if not name or name.casefold() == destination.strip().casefold():
            continue
        if name.casefold() in seen:
            continue
        seen.add(name.casefold())
        places.append(name)
        if len(places) >= MAX_EXTRACTED_PLACES:
            break

    return places
