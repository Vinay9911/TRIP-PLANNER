"""Regression tests for per-key daily token accounting.

The thing being pinned down is a distinction the interface depends on: a
*estimate* built from metered spend, versus a *measurement* Groq itself quoted
in a 429 body. Presenting the two identically would be dishonest, and the only
thing stopping that is `measured` being computed correctly.
"""

from __future__ import annotations

import time

import pytest

from app.services.keys import (
    DAILY_TOKEN_LIMITS,
    DAILY_WINDOW_SECONDS,
    KeyPool,
)
from app.services.llm import _parse_daily_figures

MODEL = "llama-3.3-70b-versatile"


def _pool() -> KeyPool:
    """A two-key pool with recognisable keys."""
    return KeyPool(["gsk_first_key_00001", "gsk_second_key_0002"], provider="groq")


def test_spend_is_charged_to_the_key_that_served_it() -> None:
    """Budget is per key, so usage must not pool across the whole account."""
    pool = _pool()
    first, second = pool.acquire(), pool.acquire()

    pool.record_tokens(first, MODEL, 10_000)
    pool.record_tokens(second, MODEL, 250)

    by_key = {
        entry["key"]: entry["quota"]
        for entry in pool.status(include_quota=True)["keys"]  # type: ignore[index]
    }
    used = sorted(quota["used"] for quota in by_key.values())
    assert used == [250, 10_000]


def test_budget_is_tracked_per_model_not_per_account() -> None:
    """A spent executor bucket says nothing about the planner's.

    This is the whole premise of `models_for_role` stepping down a fallback
    chain: reporting one aggregate number per key would hide the situation
    that mechanism exists to handle.
    """
    pool = _pool()
    key = pool.acquire()

    pool.record_tokens(key, MODEL, 90_000)
    pool.record_tokens(key, "openai/gpt-oss-120b", 1_000)

    quota = pool.status(include_quota=True)["keys"][0]["quota"]  # type: ignore[index]
    per_model = {row["model"]: row for row in quota["models"]}

    assert per_model[MODEL]["used"] == 90_000
    assert per_model["openai/gpt-oss-120b"]["used"] == 1_000
    assert per_model[MODEL]["remaining"] == DAILY_TOKEN_LIMITS[MODEL] - 90_000


def test_reported_figures_override_the_local_estimate() -> None:
    """Groq's own number wins, because it counts spend this process never saw.

    A key shared with another machine, or used before this server restarted,
    has burned budget no local counter can know about. When Groq finally says
    so, that figure has to replace the estimate rather than be added to it.
    """
    pool = _pool()
    key = pool.acquire()

    pool.record_tokens(key, MODEL, 5_000)
    pool.note_daily_limit(key, MODEL, 96_696, 100_000)

    quota = pool.status(include_quota=True)["keys"][0]["quota"]  # type: ignore[index]
    row = quota["models"][0]

    assert row["used"] == 96_696
    assert row["remaining"] == 3_304
    assert row["measured"] is True


def test_estimates_are_labelled_as_estimates() -> None:
    """A key Groq has never refused must not claim to be measured."""
    pool = _pool()
    key = pool.acquire()
    pool.record_tokens(key, MODEL, 1_000)

    quota = pool.status(include_quota=True)["keys"][0]["quota"]  # type: ignore[index]
    assert quota["models"][0]["measured"] is False


def test_spend_ages_out_of_the_rolling_window() -> None:
    """The allowance rolls continuously; nothing frees up at midnight.

    Recorded with a timestamp just outside the window, so the entry has to be
    pruned rather than counted forever.
    """
    pool = _pool()
    key = pool.acquire()

    # Reach past the public API to plant an aged entry - the alternative is a
    # test that sleeps for 24 hours.
    state = pool._find(key)
    assert state is not None
    stale = time.monotonic() - DAILY_WINDOW_SECONDS - 60
    state.record_tokens(MODEL, 50_000, stale)
    state.record_tokens(MODEL, 1_000, time.monotonic())

    quota = pool.status(include_quota=True)["keys"][0]["quota"]  # type: ignore[index]
    assert quota["models"][0]["used"] == 1_000


def test_quota_is_omitted_unless_asked_for() -> None:
    """The readiness probe must stay cheap and unchanged."""
    pool = _pool()
    assert "quota" not in pool.status()["keys"][0]  # type: ignore[index]


def test_zero_token_calls_are_ignored() -> None:
    """A provider that reported no usage must not create an empty model row."""
    pool = _pool()
    key = pool.acquire()
    pool.record_tokens(key, MODEL, 0)

    quota = pool.status(include_quota=True)["keys"][0]["quota"]  # type: ignore[index]
    assert quota["models"] == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "rate limit reached for model llama-3.3-70b-versatile on tokens per "
            "day (tpd): limit 100000, used 96696, requested 1200",
            (96_696, 100_000),
        ),
        ("service unavailable", None),
        ("tokens per day exceeded", None),
    ],
)
def test_daily_figures_are_parsed_from_the_429_body(
    text: str, expected: tuple[int, int] | None
) -> None:
    """The body is the only place Groq ever states the daily allowance.

    A miss must return None rather than raising: failing to parse simply means
    no ground truth this time, and the estimate carries on.
    """
    assert _parse_daily_figures(text) == expected
