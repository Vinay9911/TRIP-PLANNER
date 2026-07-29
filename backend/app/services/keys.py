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
from dataclasses import dataclass
from typing import Final

from app.core.errors import ConfigurationError
from app.core.logging import get_logger

logger = get_logger(__name__)

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

    def is_available(self, now: float) -> bool:
        """Whether this key may be used at `now`."""
        return now >= self.available_at

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

    def status(self) -> dict[str, object]:
        """Pool health, for `/health/ready` and the admin dashboard.

        Returns:
            Counts and per-key statistics. Keys are masked - never returned in
            full, since this is exposed over HTTP.
        """
        now = time.monotonic()
        with self._lock:
            return {
                "provider": self.provider,
                "total_keys": len(self._states),
                "available_keys": sum(1 for s in self._states if s.is_available(now)),
                "keys": [
                    {
                        "key": state.masked,
                        "available": state.is_available(now),
                        "cooldown_remaining_s": max(round(state.available_at - now, 1), 0.0),
                        "uses": state.total_uses,
                        "rate_limits": state.total_rate_limits,
                        "errors": state.total_errors,
                    }
                    for state in self._states
                ],
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


def reset_pools() -> None:
    """Discard all pools. For tests only."""
    with _pools_lock:
        _pools.clear()


def all_pool_status() -> list[dict[str, object]]:
    """Status of every live pool, for the health endpoint."""
    with _pools_lock:
        return [pool.status() for pool in _pools.values()]
