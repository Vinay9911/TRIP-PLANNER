"""Regression tests for the token and RAG-hop meters.

Found by inspecting a live run: `agent_runs.rag_hops` was 0 for every run ever
recorded, including one that made three real (non-cached) `search_travel_guide`
calls, each internally running several hops. The cause was an ordering bug,
not a missing write: `runner.run_turn` read `total_rag_hops()` *after*
`stop_metering()` had already cleared the ContextVar it lives in, so the read
always landed on the just-cleared default.

Token accounting sits right next to this code and does not have the bug,
which is instructive: `meter` is a plain Python object returned by
`start_metering()` and held in a local variable, so clearing the ContextVar
binding afterwards has no effect on the object itself. `rag_hops` had no such
object to hold onto - only a function call reading a ContextVar - so the read
had to move to before the clear rather than being fixed by capturing a
reference. These tests pin the *ordering*, which is the part that is easy to
silently break again in a future edit.
"""

from __future__ import annotations

from app.services.usage import (
    record_rag_hops,
    start_metering,
    stop_metering,
    total_rag_hops,
)


def test_hop_count_accumulates_across_multiple_calls():
    start_metering()
    try:
        record_rag_hops(4)
        record_rag_hops(2)
        assert total_rag_hops() == 6
    finally:
        stop_metering()


def test_hop_count_must_be_read_before_stop_metering_clears_it():
    """The exact bug: reading after stop_metering() silently returns 0.

    This is intentionally written as a demonstration of the failure mode
    rather than only testing the fix, so a future change that moves the read
    back to the wrong side of `stop_metering()` fails loudly here instead of
    silently shipping a metric that is always zero.
    """
    start_metering()
    record_rag_hops(5)

    stop_metering()

    assert total_rag_hops() == 0, (
        "reading after stop_metering() should be 0 - if this fails, the "
        "ContextVar semantics changed and runner.py's read-before-clear "
        "ordering may no longer be necessary, but verify before removing it"
    )


def test_hop_counter_is_isolated_between_runs():
    """One run's hops must not leak into the next run's total."""
    start_metering()
    record_rag_hops(3)
    stop_metering()

    start_metering()
    try:
        assert total_rag_hops() == 0, "a fresh run must start from zero"
        record_rag_hops(1)
        assert total_rag_hops() == 1
    finally:
        stop_metering()


def test_recording_without_an_active_meter_does_not_raise():
    """A tool might run outside a metered run (a script, a test); must be inert."""
    stop_metering()  # ensure no meter is active
    record_rag_hops(2)  # must not raise
    assert total_rag_hops() == 0
