"""API key pools with automatic failover.

Free tiers are the binding constraint on this project: Groq allows roughly
1,000 requests a day and a single plan-execute turn costs 6-12 of them. One key
runs out well before a demo does.

The fix is to hold several keys per provider and move to the next one when one
is exhausted. Set them comma-separated:

    GROQ_API_KEY=gsk_first,gsk_second,gsk_third

Any number of keys, from any number of accounts. One key is just a pool of
size one, so nothing special-cases the single-key path.

**Two behaviours are the whole point, and they pull in opposite directions
from a normal retry policy.**

*On a rate limit, do not back off - switch keys immediately.* Backing off is
correct when there is one key and the only option is to wait. With a pool, the
limit belongs to the *key*, not to the service: another key is not rate
limited, so sleeping wastes time for no reason. Backoff only applies once every
key in the pool is cooling down.

*Spread load round-robin rather than draining keys in order.* Rate limits are
per-key and per-minute as well as per-day. Hammering key one until it 429s,
then key two, means the pool spends most of its life with exactly one key
active and repeatedly tripping that key's per-minute ceiling. Round-robin keeps
every key below its own limit and makes the daily quotas additive.

Cooldowns are honoured from the provider's own `Retry-After` when it sends
one, so a key comes back exactly when the provider says it may.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Final

from app.core.errors import ConfigurationError
from app.core.logging import get_logger

logger = get_logger(__name__)

# Groq's daily token allowance, per key, per model.
#
# **These are measured, not published in any response.** The binding limit on
# this project is tokens per *day*, and Groq sends it in no header whatsoever -
# `x-ratelimit-remaining-tokens` describes only the per-minute window, which is
# why a key can report "11,959 tokens remaining" while being completely out of
# daily budget. The only place the daily figure ever appears is the body of the
# 429 it eventually returns ("Limit 100000, Used 96696"), and by then the key is
# already spent.
#
# So the numbers below are what a run against the live API reported, and they
# are the basis of an *estimate* rather than a reading. `_KeyState` counts the
# tokens this process has spent per key per model over a rolling 24 hours and
# subtracts; the moment a real 429 arrives, `note_daily_limit` replaces the
# estimate with the figure Groq itself quoted. Anything this process did not
# spend - another machine, an earlier run, a colleague sharing the key - is
# invisible to the estimate, so it reads high until ground truth corrects it.
# That direction is deliberate: an optimistic estimate that gets corrected is
# less harmful than a pessimistic one that stops you using budget you have.
DAILY_TOKEN_LIMITS: Final[dict[str, int]] = {
    "llama-3.3-70b-versatile": 100_000,
    "openai/gpt-oss-120b": 100_000,
    "openai/gpt-oss-20b": 100_000,
    "llama-3.1-8b-instant": 500_000,
}

#: Applied to a model not in the table above, so a newly-configured model
#: reports a conservative estimate rather than no estimate at all.
DEFAULT_DAILY_TOKEN_LIMIT: Final[int] = 100_000

#: The window Groq's daily allowance rolls over. Not a calendar day - nothing
#: frees up at midnight, so usage is aged out continuously.
DAILY_WINDOW_SECONDS: Final[float] = 86_400.0

# Applied when a provider reports a rate limit without a `Retry-After` header.
# Sized for a per-minute window, which is the limit most often hit first.
DEFAULT_RATE_LIMIT_COOLDOWN: Final[float] = 65.0

# A key that failed for a non-quota reason is rested briefly rather than
# retired - the cause is usually a transient network fault, not the key.
DEFAULT_ERROR_COOLDOWN: Final[float] = 15.0

# Consecutive non-quota failures before a key is treated as broken (revoked,
# mistyped) and rested for much longer.
BROKEN_KEY_THRESHOLD: Final[int] = 4
BROKEN_KEY_COOLDOWN: Final[float] = 900.0


@dataclass
class _KeyState:
    """Health of one key within a pool."""

    key: str
    #: Monotonic timestamp before which this key must not be used.
    available_at: float = 0.0
    consecutive_errors: int = 0
    total_uses: int = 0
    total_rate_limits: int = 0
    total_errors: int = 0
    last_used_at: float = 0.0

    #: Token spend this process has observed, per model, as (when, how many).
    #: A deque rather than a running total because the allowance rolls over
    #: continuously: entries older than 24 hours have to leave the count, and
    #: a scalar cannot forget.
    spend: dict[str, deque[tuple[float, int]]] = field(default_factory=dict)

    #: Ground truth from a 429 body, per model: (observed_at, used, limit).
    #: Overrides the estimate, because Groq's own figure accounts for spend
    #: this process never saw.
    reported: dict[str, tuple[float, int, int]] = field(default_factory=dict)

    def is_available(self, now: float) -> bool:
        """Whether this key may be used at `now`."""
        return now >= self.available_at

    def record_tokens(self, model: str, tokens: int, now: float) -> None:
        """Add one call's token cost against a model, ageing out old entries.

        Args:
            model: The model the tokens were spent on. Tracked separately
                because the daily allowance is per model, not per account -
                a spent `llama-3.3-70b` bucket says nothing about `gpt-oss`.
            tokens: Prompt plus completion tokens.
            now: Monotonic clock reading.
        """
        window = self.spend.setdefault(model, deque())
        window.append((now, tokens))
        self._prune(window, now)

    def used_today(self, model: str, now: float) -> int:
        """Tokens spent on a model within the rolling window.

        Prefers Groq's own reported figure when one is recent enough to still
        describe the same window, since it includes spend this process never
        saw - another instance, an earlier restart, a shared key.
        """
        observed = self.reported.get(model)
        if observed is not None:
            seen_at, used, _limit = observed
            if now - seen_at < DAILY_WINDOW_SECONDS:
                # The reported figure is a snapshot; anything spent since then
                # is added on top rather than discarded.
                window = self.spend.get(model)
                since = sum(count for at, count in (window or ()) if at > seen_at)
                return used + since

        window = self.spend.get(model)
        if not window:
            return 0
        self._prune(window, now)
        return sum(count for _at, count in window)

    def limit_for(self, model: str, now: float) -> int:
        """The daily allowance for a model - Groq's figure if it ever said."""
        observed = self.reported.get(model)
        if observed is not None and now - observed[0] < DAILY_WINDOW_SECONDS:
            return observed[2]
        return DAILY_TOKEN_LIMITS.get(model, DEFAULT_DAILY_TOKEN_LIMIT)

    @staticmethod
    def _prune(window: deque[tuple[float, int]], now: float) -> None:
        """Drop entries that have aged out of the rolling window."""
        cutoff = now - DAILY_WINDOW_SECONDS
        while window and window[0][0] < cutoff:
            window.popleft()

    @property
    def masked(self) -> str:
        """A safe identifier for logs: enough to tell keys apart, not to use one."""
        if len(self.key) <= 12:
            return f"{self.key[:2]}…{self.key[-2:]}"
        return f"{self.key[:6]}…{self.key[-4:]}"


class AllKeysExhausted(ConfigurationError):
    """Every key in a pool is cooling down.

    Carries the shortest remaining cooldown so the caller can decide between
    waiting and giving up, rather than having to guess.
    """

    code = "all_keys_exhausted"
    status_code = 429
    retryable = True

    def __init__(self, provider: str, retry_after_seconds: float) -> None:
        """Initialise the error.

        Args:
            provider: Which pool ran out, e.g. `"groq"`.
            retry_after_seconds: Time until the first key becomes available.
        """
        super().__init__(
            f"All {provider} API keys are currently rate limited. "
            f"The next one becomes available in {retry_after_seconds:.0f}s. "
            f"Add more keys to {provider.upper()}_API_KEY as a comma-separated list.",
            details={"provider": provider, "retry_after_seconds": round(retry_after_seconds, 1)},
        )
        self.provider = provider
        self.retry_after_seconds = retry_after_seconds


class KeyPool:
    """A rotating pool of interchangeable API keys for one provider.

    Thread-safe and safe under asyncio: selection and state updates happen
    under a lock, so concurrent requests cannot hand the same key out twice in
    a way that defeats the round-robin, nor lose a rate-limit report.

    Attributes:
        provider: Short provider name, used in logs and errors.
    """

    def __init__(self, keys: list[str], *, provider: str) -> None:
        """Initialise the pool.

        Args:
            keys: The keys, in configuration order. Blanks and duplicates are
                dropped - a duplicated key would otherwise get twice the
                round-robin share while sharing one quota.
            provider: Short provider name.

        Raises:
            ConfigurationError: If no usable key was supplied.
        """
        cleaned: list[str] = []
        seen: set[str] = set()
        for key in keys:
            candidate = key.strip()
            if candidate and candidate not in seen:
                seen.add(candidate)
                cleaned.append(candidate)

        if not cleaned:
            raise ConfigurationError(
                f"No {provider} API key is configured. Set {provider.upper()}_API_KEY "
                "in your .env file. Multiple keys may be given comma-separated to "
                "raise the effective rate limit."
            )

        self.provider = provider
        self._states = [_KeyState(key=key) for key in cleaned]
        self._cursor = 0
        self._lock = threading.Lock()

        if len(cleaned) < len(keys):
            logger.info(
                "keypool.deduplicated",
                provider=provider,
                supplied=len(keys),
                unique=len(cleaned),
            )

        logger.info("keypool.ready", provider=provider, keys=len(cleaned))

    @property
    def size(self) -> int:
        """How many keys the pool holds."""
        return len(self._states)

    def acquire(self) -> str:
        """Return the next available key.

        Round-robin over the healthy keys, so load spreads evenly and no single
        key trips its per-minute ceiling while others idle.

        Returns:
            An API key.

        Raises:
            AllKeysExhausted: If every key is cooling down.
        """
        now = time.monotonic()

        with self._lock:
            # Walk the ring once from the cursor. The first available key wins,
            # and the cursor advances past it so the next call starts elsewhere.
            for offset in range(len(self._states)):
                index = (self._cursor + offset) % len(self._states)
                state = self._states[index]
                if state.is_available(now):
                    self._cursor = (index + 1) % len(self._states)
                    state.total_uses += 1
                    state.last_used_at = now
                    return state.key

            soonest = min(state.available_at for state in self._states)

        wait = max(soonest - now, 0.0)
        logger.warning(
            "keypool.exhausted",
            provider=self.provider,
            keys=len(self._states),
            retry_after_seconds=round(wait, 1),
        )
        raise AllKeysExhausted(self.provider, wait)

    def report_success(self, key: str) -> None:
        """Record that a key worked, clearing its error streak.

        Args:
            key: The key that succeeded.
        """
        with self._lock:
            state = self._find(key)
            if state is not None:
                state.consecutive_errors = 0

    def report_rate_limited(self, key: str, retry_after_seconds: float | None = None) -> None:
        """Rest a key that reported a quota or rate-limit failure.

        Args:
            key: The rate-limited key.
            retry_after_seconds: The provider's own advice, when it sent any.
                Honoured directly so the key returns exactly when permitted.
        """
        cooldown = retry_after_seconds if retry_after_seconds else DEFAULT_RATE_LIMIT_COOLDOWN

        with self._lock:
            state = self._find(key)
            if state is None:
                return
            state.available_at = time.monotonic() + cooldown
            state.total_rate_limits += 1
            # Not an error streak: a rate limit means the key works, and
            # counting it toward "broken" would eventually retire a healthy key.
            state.consecutive_errors = 0
            remaining = sum(1 for other in self._states if other.is_available(time.monotonic()))

        logger.info(
            "keypool.key_rate_limited",
            provider=self.provider,
            key=state.masked,
            cooldown_seconds=round(cooldown, 1),
            keys_still_available=remaining,
        )

    def report_error(self, key: str) -> None:
        """Rest a key that failed for a reason other than quota.

        Repeated failures suggest the key itself is bad - revoked, mistyped,
        or lacking permission - so the cooldown lengthens sharply rather than
        letting a dead key keep taking its turn in the rotation.

        Args:
            key: The key that failed.
        """
        with self._lock:
            state = self._find(key)
            if state is None:
                return

            state.consecutive_errors += 1
            state.total_errors += 1
            broken = state.consecutive_errors >= BROKEN_KEY_THRESHOLD
            cooldown = BROKEN_KEY_COOLDOWN if broken else DEFAULT_ERROR_COOLDOWN
            state.available_at = time.monotonic() + cooldown
            masked, streak = state.masked, state.consecutive_errors

        if broken:
            logger.warning(
                "keypool.key_looks_broken",
                provider=self.provider,
                key=masked,
                consecutive_errors=streak,
                cooldown_seconds=cooldown,
                hint="check whether this key has been revoked or mistyped",
            )
        else:
            logger.info("keypool.key_error", provider=self.provider, key=masked, streak=streak)

    def available_count(self) -> int:
        """How many keys are usable right now."""
        now = time.monotonic()
        with self._lock:
            return sum(1 for state in self._states if state.is_available(now))

    def record_tokens(self, key: str, model: str, tokens: int) -> None:
        """Charge one call's tokens against the key that served it.

        This is the entire mechanism behind the quota display. Groq will not
        tell you how much daily budget is left, so the only way to know is to
        count what you spend - which this process is uniquely placed to do,
        since every call already passes through one metered code path.

        Args:
            key: The key that served the call.
            model: The model it was spent on.
            tokens: Prompt plus completion tokens. Ignored when zero, which is
                what a provider that reported no usage looks like.
        """
        if tokens <= 0:
            return
        now = time.monotonic()
        with self._lock:
            state = self._find(key)
            if state is not None:
                state.record_tokens(model, tokens, now)

    def note_daily_limit(self, key: str, model: str, used: int, limit: int) -> None:
        """Record the daily figures Groq quoted in a 429 body.

        Ground truth, and the only ground truth available. It supersedes the
        local estimate because it accounts for spend this process cannot see:
        an earlier deployment, a second instance, a key shared with another
        project.

        Args:
            key: The key that was refused.
            model: The model whose bucket is spent.
            used: Tokens Groq says have been used.
            limit: The allowance Groq says applies.
        """
        now = time.monotonic()
        with self._lock:
            state = self._find(key)
            if state is not None:
                state.reported[model] = (now, used, limit)
                masked = state.masked

        logger.info(
            "keypool.daily_limit_reported",
            provider=self.provider,
            key=masked,
            model=model,
            used=used,
            limit=limit,
        )

    def status(self, *, include_quota: bool = False) -> dict[str, object]:
        """Pool health, for `/health/ready` and the admin dashboard.

        Args:
            include_quota: Also report the per-key daily token estimate. Off by
                default so the readiness probe stays a cheap liveness answer.

        Returns:
            Counts and per-key statistics. Keys are masked - never returned in
            full, since this is exposed over HTTP.
        """
        now = time.monotonic()
        with self._lock:
            keys = []
            for state in self._states:
                entry: dict[str, object] = {
                    "key": state.masked,
                    "available": state.is_available(now),
                    "cooldown_remaining_s": max(round(state.available_at - now, 1), 0.0),
                    "uses": state.total_uses,
                    "rate_limits": state.total_rate_limits,
                    "errors": state.total_errors,
                }
                if include_quota:
                    entry["quota"] = self._quota_for(state, now)
                keys.append(entry)

            return {
                "provider": self.provider,
                "total_keys": len(self._states),
                "available_keys": sum(1 for s in self._states if s.is_available(now)),
                "keys": keys,
            }

    @staticmethod
    def _quota_for(state: _KeyState, now: float) -> dict[str, object]:
        """Render one key's daily budget position. Caller must hold the lock.

        Reported per model as well as in total, because "this key has budget"
        is not a single fact: the daily allowance is per model, so a key can be
        finished for the executor's model and perfectly usable for the
        planner's. A single aggregate number would hide exactly the situation
        the fallback chain in `models_for_role` exists to handle.
        """
        models = sorted(set(state.spend) | set(state.reported))
        per_model = []
        used_total = 0
        limit_total = 0

        for model in models:
            used = state.used_today(model, now)
            limit = state.limit_for(model, now)
            used_total += used
            limit_total += limit
            per_model.append(
                {
                    "model": model,
                    "used": used,
                    "limit": limit,
                    "remaining": max(limit - used, 0),
                    # True once Groq has quoted real figures for this model, so
                    # the interface can distinguish a measurement from a guess
                    # rather than presenting both with equal confidence.
                    "measured": model in state.reported,
                }
            )

        return {
            "used": used_total,
            "limit": limit_total,
            "remaining": max(limit_total - used_total, 0),
            "models": per_model,
        }

    def _find(self, key: str) -> _KeyState | None:
        """Look up a key's state. Caller must hold the lock."""
        for state in self._states:
            if state.key == key:
                return state
        return None


def parse_keys(raw: str) -> list[str]:
    """Split a comma-separated key string into individual keys.

    Newlines and semicolons are accepted as separators too, because keys are
    usually pasted from somewhere and arrive with whatever the source used.

    Args:
        raw: The configured value, e.g. `"gsk_one, gsk_two"`.

    Returns:
        Individual keys with surrounding whitespace and quotes removed.

    Example:
        >>> parse_keys("gsk_one, gsk_two")
        ['gsk_one', 'gsk_two']
    """
    if not raw:
        return []

    normalised = raw.replace("\n", ",").replace(";", ",")
    return [part.strip().strip("\"'") for part in normalised.split(",") if part.strip()]


# ---------------------------------------------------------------------------
# Process-wide pools
# ---------------------------------------------------------------------------
# Held at module level so every caller shares one view of key health. A pool
# per request would forget that a key was rate limited a second ago and hand it
# straight back out.

_pools: dict[str, KeyPool] = {}
_pools_lock = threading.Lock()


def get_pool(provider: str, raw_keys: str) -> KeyPool:
    """Return the shared pool for a provider, creating it on first use.

    Args:
        provider: Short provider name, e.g. `"groq"`.
        raw_keys: The configured comma-separated key string.

    Returns:
        The shared pool.

    Raises:
        ConfigurationError: If no usable key was configured.
    """
    with _pools_lock:
        pool = _pools.get(provider)
        if pool is None:
            pool = KeyPool(parse_keys(raw_keys), provider=provider)
            _pools[provider] = pool
        return pool


def existing_pool(provider: str) -> KeyPool | None:
    """Return a provider's pool if one has already been built, else None.

    Distinct from `get_pool`, which takes the raw key string and creates the
    pool on demand. Callers that only want to *report* against an existing pool
    - the usage callback, the status endpoint - have no key string to offer and
    no business creating one, and passing a single key in as though it were the
    configured list would build a pool of one that silently shadows the real
    seven.

    Args:
        provider: Short provider name, e.g. `"groq"`.

    Returns:
        The shared pool, or None if nothing has created it yet.
    """
    with _pools_lock:
        return _pools.get(provider)


def reset_pools() -> None:
    """Discard all pools. For tests only."""
    with _pools_lock:
        _pools.clear()


def all_pool_status() -> list[dict[str, object]]:
    """Status of every live pool, for the health endpoint."""
    with _pools_lock:
        return [pool.status() for pool in _pools.values()]
