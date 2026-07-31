"""Live progress for a running turn.

**Why this exists.** A full plan makes roughly fifty model calls and fifty
tool calls, and until now showed nothing at all until the last one finished.
Measured against the live API a single Groq call takes about a second, so the
agent was never "hanging" - it was working, silently, for minutes. That is a
presentation failure rather than a performance one, and it is the single most
common complaint about this app: people assume it has crashed and reload,
which throws away the work in flight and starts the cost again.

Nothing here makes the agent faster. It makes the waiting legible, which is a
different and cheaper problem to solve.

**Design.** Context-local, matching `usage.py` and `tools/base.py`. Progress
is emitted from deep inside the graph - nodes, and the tool wrapper - and read
at the very top by the streaming route. Threading a queue through every node
signature to be used in two places would be noise; a `ContextVar` is
inherited by tasks spawned inside the request and is invisible everywhere it
is not wanted.

**Dropping is deliberate.** `put_nowait` on a bounded queue, and a full queue
discards rather than blocks. A progress note is worth strictly less than the
work it describes, so a slow or vanished reader must never be able to stall
the agent. The non-streaming endpoint has no reader at all, and emitting into
a queue nobody drains has to be free.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

EventKind = Literal["stage", "step", "tool", "note", "done"]

#: Bounded so a reader that stops draining cannot grow this without limit.
#: Sized well above the ~100 events a heavy plan produces, so in practice
#: nothing is dropped and the cap only matters in the pathological case.
MAX_QUEUED_EVENTS = 512


@dataclass
class ProgressEvent:
    """One thing worth telling the traveller while they wait.

    Attributes:
        kind: What sort of event this is, which decides how a client renders
            it - a stage change replaces the current line, a tool call adds
            detail beneath it.
        message: Human-readable text, written for a traveller rather than an
            engineer. "Looking up flights" and not "invoking search_flights".
        detail: Optional extra shown smaller, e.g. which place is being
            looked up.
        data: Structured extras for clients that want them.
    """

    kind: EventKind
    message: str
    detail: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Render as the JSON payload sent over the wire."""
        payload: dict[str, Any] = {"kind": self.kind, "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        if self.data:
            payload["data"] = self.data
        return payload


_channel: ContextVar[asyncio.Queue[ProgressEvent] | None] = ContextVar(
    "progress_channel", default=None
)


def start_progress() -> asyncio.Queue[ProgressEvent]:
    """Open a progress channel for the current context.

    Returns:
        The queue the streaming route drains. Only the streaming path calls
        this; the plain JSON endpoint leaves the channel closed and every
        `emit` below becomes a no-op.
    """
    queue: asyncio.Queue[ProgressEvent] = asyncio.Queue(maxsize=MAX_QUEUED_EVENTS)
    _channel.set(queue)
    return queue


def stop_progress() -> None:
    """Close the progress channel for the current context."""
    _channel.set(None)


def emit(kind: EventKind, message: str, *, detail: str | None = None, **data: Any) -> None:
    """Publish one progress event, if anybody is listening.

    Args:
        kind: Event kind.
        message: Traveller-facing text.
        detail: Optional secondary text.
        **data: Structured extras.
    """
    queue = _channel.get()
    if queue is None:
        return
    try:
        queue.put_nowait(ProgressEvent(kind=kind, message=message, detail=detail, data=data))
    except asyncio.QueueFull:
        # See the module docstring: progress is the first thing to sacrifice.
        pass


#: Traveller-facing wording for each plan step kind. The model writes step
#: descriptions for itself ("Retrieve any stored traveller preferences..."),
#: which are the wrong register to show someone waiting on a holiday.
STEP_LABELS: dict[str, str] = {
    "research": "Researching places",
    "logistics": "Checking flights, stays and weather",
    "verify": "Double-checking the details",
    "compose": "Writing up your plan",
}

#: Same idea for tools. A traveller does not need the function name.
TOOL_LABELS: dict[str, str] = {
    "search_travel_guide": "Reading travel guides",
    "search_web": "Searching the web",
    "find_places": "Finding places",
    "search_flights": "Looking up flights",
    "search_accommodation": "Looking up places to stay",
    "get_weather_forecast": "Checking the weather",
    "recall_user_preferences": "Remembering what you like",
    "get_current_datetime": "Checking the date",
}


def label_for_tool(name: str) -> str:
    """Return traveller-facing wording for a tool name.

    Args:
        name: The registered tool name.

    Returns:
        A friendly label, falling back to the name with underscores removed
        so an unlabelled new tool still reads acceptably.
    """
    return TOOL_LABELS.get(name, name.replace("_", " ").capitalize())
