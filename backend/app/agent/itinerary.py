"""Structured itinerary output.

**Why the itinerary stopped being prose.** A live plan for Geneva came back
as five paragraphs. It was accurate and unreadable: no way to scan it, no way
to attach a photograph to a place, no way to put a pin on a map, and no way
to offer "book this" next to an item. Every one of those is impossible
against a wall of text, and all of them are trivial against a list of typed
objects.

So the responder emits this schema for full plans instead, and the interface
renders it. The model still writes the connective prose - an intro, a note
per day - because that is what it is good at; it just stops being responsible
for layout, which it is not.

**Coordinates are optional and that is deliberate.** They arrive when a place
came from `find_places`, which returns them, and are absent when it came from
a guide article, which does not. Rather than have the model guess latitudes -
which it will do, confidently and wrongly - the field is left empty and the
map simply shows fewer pins.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: What kind of thing an itinerary item is. Drives the icon, the photo
#: category and which follow-up action is offered next to it.
ItemKind = Literal["sight", "food", "activity", "transport", "stay", "neighbourhood", "note"]

#: Which part of the day. "all_day" exists for things like a day trip that
#: cannot be slotted into a morning.
DaySlot = Literal["morning", "afternoon", "evening", "all_day"]


class ItineraryItem(BaseModel):
    """One thing to do, see, eat or book."""

    name: str = Field(
        description=(
            "The place itself, as it would appear on a map or a sign - "
            "'St. Pierre Cathedral', not 'visit the cathedral in the morning'. "
            "No narrative, no verbs."
        )
    )
    kind: ItemKind = Field(default="sight", description="What sort of thing this is.")
    district: str | None = Field(
        default=None,
        description="Neighbourhood or area, when known, e.g. 'Old Town'. Never a slash path.",
    )
    description: str = Field(
        default="",
        max_length=280,
        description=(
            "One or two sentences on what it is and why it suits THIS traveller. "
            "Specific over generic: what they will actually see or eat."
        ),
    )
    latitude: float | None = Field(
        default=None,
        description="Only if a tool returned it. Never estimate or recall coordinates.",
    )
    longitude: float | None = Field(default=None, description="As latitude.")
    approx_duration: str | None = Field(
        default=None, max_length=40, description="Rough time needed, e.g. '1-2 hours'."
    )
    booking_note: str | None = Field(
        default=None,
        max_length=120,
        description="Only when it genuinely matters, e.g. 'book ahead in summer'.",
    )


class ItineraryDay(BaseModel):
    """One day of the trip."""

    day_number: int = Field(ge=1, le=90)
    title: str = Field(
        max_length=80,
        description="A short label for the day's theme, e.g. 'Old Town and the lake'.",
    )
    date: str | None = Field(default=None, description="YYYY-MM-DD when the dates are known.")
    summary: str = Field(
        default="",
        max_length=240,
        description="One sentence on the shape of the day.",
    )
    morning: list[ItineraryItem] = Field(default_factory=list)
    afternoon: list[ItineraryItem] = Field(default_factory=list)
    evening: list[ItineraryItem] = Field(default_factory=list)
    all_day: list[ItineraryItem] = Field(default_factory=list)

    def every_item(self) -> list[ItineraryItem]:
        """All items in this day, in the order they would be done."""
        return [*self.all_day, *self.morning, *self.afternoon, *self.evening]


class Itinerary(BaseModel):
    """A complete day-by-day plan."""

    destination: str = Field(description="Where the trip is.")
    intro: str = Field(
        default="",
        max_length=400,
        description=(
            "Two or three warm sentences framing the trip. This is the only "
            "place for general scene-setting - everything else belongs to a day."
        ),
    )
    days: list[ItineraryDay] = Field(default_factory=list)
    practical_notes: list[str] = Field(
        default_factory=list,
        description=(
            "Short practical points that apply to the whole trip: transport passes, "
            "what the weather means, when to book. Maximum four, each one line."
        ),
    )
    gaps: list[str] = Field(
        default_factory=list,
        description=(
            "Anything that could NOT be checked, in plain words a traveller "
            "understands - 'I couldn't check the forecast that far ahead'. Never "
            "name an internal tool, service or limit."
        ),
    )

    def all_items(self) -> list[ItineraryItem]:
        """Every item across every day, for mapping and photo resolution."""
        return [item for day in self.days for item in day.every_item()]


ITINERARY_INSTRUCTIONS = """\
Return the trip as structured days rather than prose.

RULES THAT MATTER
- `name` is the place as a map would label it. "St. Pierre Cathedral", not \
"visit the beautiful cathedral". Put the narrative in `description`.
- Every item must come from the research findings. If the findings do not \
name a single specific place for a day, do not invent filler like "explore \
nearby villages" - either drop the day's slot or say plainly in `summary` \
that you had nothing specific for it.
- Only set `latitude`/`longitude` when a tool actually returned them. Never \
recall or estimate coordinates.
- Respect the geography. Items in one day should be close enough to do in \
one day; do not scatter a day across areas that are hours apart.
- `gaps` is for things you could not check, written for a traveller. Never \
mention tools, APIs, rate limits or model providers.\
"""
