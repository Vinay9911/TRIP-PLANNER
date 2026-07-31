"""Structured findings from a turn, kept so the interface can show them.

**Why.** Everything a tool discovers currently reaches the traveller only as
prose the model wrote about it. That is fine for reading and useless for
building an interface: a weather forecast becomes a sentence, three hotels
become a paragraph, and a panel that wants to render a temperature or a price
has nothing to render. Worse, the model is the only thing standing between a
real payload and what the screen says about it, which is exactly the wrong
place for a number to be retyped.

So tool payloads are collected here as they are produced, and returned
alongside the reply. The panels render the payload; the prose stays prose.

**Provenance travels with the data, always.** Every entry records where it
came from and whether it is simulated, because this project ships a mock
flight and hotel provider - Amadeus withdrew its free tier - and a price that
looks real and is not is the single most misleading thing this app could put
on a screen. The flag is set from the payload's own `source` field rather than
inferred, so it cannot drift from what actually served the request.

Context-local, matching `usage.py`, `progress.py` and the tool recorder:
concurrent requests each collect their own with no shared state and no
parameter threaded through every tool signature.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

#: Tool name -> the panel its payload belongs to.
#:
#: Only tools whose output is worth *showing* appear here. `search_web` and
#: `search_travel_guide` return prose that belongs in the reply, not in a
#: panel, and adding them would fill the interface with things nobody can
#: read at a glance.
_PANEL_FOR_TOOL: dict[str, str] = {
    "get_weather_forecast": "weather",
    "search_accommodation": "stays",
    "search_flights": "flights",
    "find_places": "places",
}

#: Providers whose data is generated rather than fetched.
#:
#: Read from the payload rather than assumed from configuration, so switching
#: `FLIGHT_PROVIDER` to a real one flips the label with no other change.
_SIMULATED_SOURCES = frozenset({"mock"})


@dataclass
class Fact:
    """One tool payload, with where it came from.

    Attributes:
        panel: Which part of the interface renders this.
        tool: The tool that produced it.
        source: The upstream service or provider.
        simulated: True when the numbers were generated rather than fetched.
            Rendered as a badge; never omitted.
        data: The payload itself, exactly as the tool returned it.
    """

    panel: str
    tool: str
    source: str
    simulated: bool
    data: Any

    def to_dict(self) -> dict[str, Any]:
        """Render for the API."""
        return {
            "panel": self.panel,
            "tool": self.tool,
            "source": self.source,
            "simulated": self.simulated,
            "data": self.data,
        }


@dataclass
class FactSheet:
    """Everything worth showing from one turn, newest per panel."""

    #: Last successful payload per panel. Later calls replace earlier ones -
    #: a second hotel search with a corrected budget supersedes the first, and
    #: showing both would leave the traveller comparing a result against a
    #: question they already refined.
    by_panel: dict[str, Fact] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Render the whole sheet for the API."""
        return {panel: fact.to_dict() for panel, fact in self.by_panel.items()}


_sheet: ContextVar[FactSheet | None] = ContextVar("fact_sheet", default=None)


def start_facts() -> FactSheet:
    """Begin collecting structured findings for the current context.

    Returns:
        The sheet the runner reads once the turn finishes.
    """
    sheet = FactSheet()
    _sheet.set(sheet)
    return sheet


def stop_facts() -> None:
    """Stop collecting for the current context."""
    _sheet.set(None)


def record_fact(tool: str, source: str, data: Any) -> None:
    """Keep a tool's payload if it is one the interface renders.

    Args:
        tool: Registered tool name.
        source: The upstream service that answered.
        data: The tool's payload.
    """
    sheet = _sheet.get()
    if sheet is None or not isinstance(data, dict):
        return

    panel = _PANEL_FOR_TOOL.get(tool)
    if panel is None:
        return

    sheet.by_panel[panel] = Fact(
        panel=panel,
        tool=tool,
        source=source,
        # The payload's own source is authoritative. A provider that starts
        # returning real data stops being labelled simulated without anyone
        # remembering to update a list here.
        simulated=str(data.get("source", source)).lower() in _SIMULATED_SOURCES,
        data=data,
    )


def collected_facts() -> dict[str, Any]:
    """Return this turn's findings, keyed by panel."""
    sheet = _sheet.get()
    return sheet.to_dict() if sheet is not None else {}
