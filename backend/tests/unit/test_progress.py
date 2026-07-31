"""Tests for the live progress channel.

The behaviour that matters is not "events arrive" but everything around it:
that emitting is free when nobody listens, that one request cannot see
another's progress, and that a stalled reader can never block the agent. The
whole point of the channel is that it is the *cheapest* thing in the system,
because it carries the least important information.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import progress
from app.services.progress import (
    MAX_QUEUED_EVENTS,
    emit,
    label_for_tool,
    start_progress,
    stop_progress,
)


@pytest.mark.asyncio
async def test_events_arrive_in_order() -> None:
    """Progress is a narrative; out-of-order events would read as nonsense."""
    queue = start_progress()

    emit("stage", "Reading your message")
    emit("stage", "Working out a plan", detail="5 steps")
    emit("tool", "Checking the weather")

    messages = [queue.get_nowait().message for _ in range(3)]
    assert messages == [
        "Reading your message",
        "Working out a plan",
        "Checking the weather",
    ]
    stop_progress()


@pytest.mark.asyncio
async def test_emitting_without_a_listener_is_a_no_op() -> None:
    """The plain JSON endpoint opens no channel, and must pay nothing for it.

    If this ever raised, every non-streaming request would fail on the first
    tool call - so the silent path is the one that most needs a test.
    """
    stop_progress()
    emit("stage", "nobody is listening")
    emit("tool", "still nobody")


@pytest.mark.asyncio
async def test_a_full_queue_drops_rather_than_blocks() -> None:
    """A stalled reader must never be able to stall the agent.

    Progress is worth strictly less than the work it describes. Given the
    choice between losing a status line and blocking a plan mid-flight, the
    status line goes.
    """
    start_progress()

    for index in range(MAX_QUEUED_EVENTS + 50):
        emit("tool", f"event {index}")

    assert progress._channel.get().qsize() == MAX_QUEUED_EVENTS
    stop_progress()


@pytest.mark.asyncio
async def test_concurrent_turns_do_not_see_each_others_progress() -> None:
    """Two travellers, two channels.

    A shared queue would leak one traveller's trip into the other's screen,
    which is a privacy failure and not merely a cosmetic bug.
    """

    async def turn(label: str) -> list[str]:
        queue = start_progress()
        emit("stage", f"{label} first")
        await asyncio.sleep(0)
        emit("stage", f"{label} second")
        return [queue.get_nowait().message for _ in range(queue.qsize())]

    first, second = await asyncio.gather(turn("alice"), turn("bob"))

    assert first == ["alice first", "alice second"]
    assert second == ["bob first", "bob second"]


def test_tool_labels_are_written_for_travellers() -> None:
    """Nobody waiting on a holiday wants to read a function name."""
    assert label_for_tool("search_accommodation") == "Looking up places to stay"
    assert label_for_tool("get_weather_forecast") == "Checking the weather"


def test_an_unlabelled_tool_still_reads_acceptably() -> None:
    """A new tool must read acceptably before anyone labels it.

    Otherwise adding a tool silently leaks an identifier into the UI.
    """
    assert label_for_tool("search_ferry_times") == "Search ferry times"
