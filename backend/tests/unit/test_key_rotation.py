"""Tests for multi-key failover.

The pool exists so a free-tier quota stops being a hard ceiling. Two
behaviours carry that, and both are the opposite of what a single-key retry
policy does, so both are pinned here:

* a rate-limited key is **skipped, not waited on**;
* load spreads **round-robin**, so keys stay under their per-minute limits
  instead of one being hammered until it 429s.
"""

from __future__ import annotations

import time

import pytest

from app.core.errors import ConfigurationError
from app.services.keys import (
    BROKEN_KEY_THRESHOLD,
    AllKeysExhausted,
    KeyPool,
    get_pool,
    parse_keys,
    reset_pools,
)


@pytest.fixture(autouse=True)
def _clean_pools():
    reset_pools()
    yield
    reset_pools()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("one", ["one"]),
        ("one,two", ["one", "two"]),
        ("one, two , three", ["one", "two", "three"]),
        ("one,,two,", ["one", "two"]),
        ("one\ntwo", ["one", "two"]),
        ("one;two", ["one", "two"]),
        ('"one", "two"', ["one", "two"]),
        ("", []),
        ("   ", []),
    ],
)
def test_keys_parse_from_a_variety_of_separators(raw, expected):
    """Keys are pasted from wherever, so accept what people actually paste."""
    assert parse_keys(raw) == expected


def test_pool_rejects_an_empty_configuration():
    with pytest.raises(ConfigurationError) as caught:
        KeyPool([], provider="groq")

    # The message must say how to fix it, including the multi-key syntax.
    assert "GROQ_API_KEY" in str(caught.value)
    assert "comma-separated" in str(caught.value)


def test_duplicate_keys_are_collapsed():
    """A duplicate would take two round-robin turns while sharing one quota."""
    pool = KeyPool(["same", "same", "other"], provider="test")
    assert pool.size == 2


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def test_keys_are_handed_out_round_robin():
    pool = KeyPool(["a", "b", "c"], provider="test")

    assert [pool.acquire() for _ in range(6)] == ["a", "b", "c", "a", "b", "c"]


def test_a_rate_limited_key_is_skipped():
    pool = KeyPool(["a", "b", "c"], provider="test")
    pool.report_rate_limited("b", 60)

    handed_out = {pool.acquire() for _ in range(6)}

    assert handed_out == {"a", "c"}
    assert pool.available_count() == 2


def test_a_single_key_pool_still_works():
    """One key must not be a special case anywhere."""
    pool = KeyPool(["only"], provider="test")

    assert pool.size == 1
    assert [pool.acquire() for _ in range(3)] == ["only"] * 3


def test_exhausting_every_key_reports_when_one_returns():
    pool = KeyPool(["a", "b"], provider="test")
    pool.report_rate_limited("a", 30)
    pool.report_rate_limited("b", 90)

    with pytest.raises(AllKeysExhausted) as caught:
        pool.acquire()

    # The soonest key, not the last one reported.
    assert 25 <= caught.value.retry_after_seconds <= 31
    assert caught.value.provider == "test"
    assert caught.value.status_code == 429


def test_a_key_returns_once_its_cooldown_expires():
    pool = KeyPool(["a", "b"], provider="test")
    # A cooldown short enough to elapse within the test.
    pool.report_rate_limited("a", 0.05)
    pool.report_rate_limited("b", 0.05)

    with pytest.raises(AllKeysExhausted):
        pool.acquire()

    time.sleep(0.1)

    assert pool.acquire() in {"a", "b"}
    assert pool.available_count() == 2


def test_provider_retry_after_is_honoured_over_the_default():
    pool = KeyPool(["a", "b"], provider="test")
    pool.report_rate_limited("a", 5)
    pool.report_rate_limited("b", 5)

    with pytest.raises(AllKeysExhausted) as caught:
        pool.acquire()

    # The 65s default would be wrong here - the provider said 5.
    assert caught.value.retry_after_seconds < 10


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_a_repeatedly_failing_key_is_rested_for_much_longer():
    """A revoked or mistyped key should stop taking its turn in the rotation."""
    pool = KeyPool(["good", "bad"], provider="test")

    for _ in range(BROKEN_KEY_THRESHOLD):
        pool.report_error("bad")

    status = {entry["key"]: entry for entry in pool.status()["keys"]}
    bad = next(entry for key, entry in status.items() if key.startswith("ba"))

    assert bad["available"] is False
    assert bad["cooldown_remaining_s"] > 60
    assert pool.acquire() == "good"


def test_success_clears_an_error_streak():
    pool = KeyPool(["a"], provider="test")

    pool.report_error("a")
    pool.report_error("a")
    pool.report_success("a")
    pool.report_error("a")

    # One error after a success must not inherit the earlier streak and land
    # the key in the long "broken" cooldown.
    entry = pool.status()["keys"][0]
    assert entry["cooldown_remaining_s"] < 60


def test_rate_limits_do_not_count_toward_the_broken_threshold():
    """A rate limit proves a key works; counting it would retire healthy keys."""
    pool = KeyPool(["a", "b"], provider="test")

    for _ in range(BROKEN_KEY_THRESHOLD + 2):
        pool.report_rate_limited("a", 0.01)
        time.sleep(0.02)

    entry = next(e for e in pool.status()["keys"] if e["rate_limits"] > 0)
    assert entry["available"] is True


def test_reporting_an_unknown_key_is_ignored():
    """A stale key from a reloaded config must not raise."""
    pool = KeyPool(["a"], provider="test")

    pool.report_rate_limited("never-existed", 30)
    pool.report_error("never-existed")
    pool.report_success("never-existed")

    assert pool.acquire() == "a"


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


def test_status_never_exposes_a_full_key():
    """Status is served over HTTP, so keys must be masked."""
    secret = "gsk_thisisaverylongsecretkeyvalue"
    pool = KeyPool([secret], provider="test")
    pool.acquire()

    status = pool.status()
    rendered = str(status)

    assert secret not in rendered
    assert status["total_keys"] == 1
    assert status["available_keys"] == 1
    assert status["keys"][0]["uses"] == 1


def test_status_counts_uses_rate_limits_and_errors():
    pool = KeyPool(["a", "b"], provider="test")
    pool.acquire()
    pool.acquire()
    pool.report_rate_limited("a", 30)
    pool.report_error("b")

    by_key = {entry["key"]: entry for entry in pool.status()["keys"]}
    values = list(by_key.values())

    assert sum(entry["uses"] for entry in values) == 2
    assert sum(entry["rate_limits"] for entry in values) == 1
    assert sum(entry["errors"] for entry in values) == 1


# ---------------------------------------------------------------------------
# Shared pools
# ---------------------------------------------------------------------------


def test_pools_are_shared_per_provider():
    """A per-request pool would forget a key was limited a second ago."""
    first = get_pool("groq", "a,b")
    second = get_pool("groq", "a,b")

    assert first is second

    first.report_rate_limited("a", 60)
    assert second.available_count() == 1


def test_different_providers_get_separate_pools():
    groq = get_pool("groq", "g1,g2")
    gemini = get_pool("gemini", "x1")

    assert groq is not gemini
    assert groq.size == 2
    assert gemini.size == 1
