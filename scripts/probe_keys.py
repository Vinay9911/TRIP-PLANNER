"""Check every configured API key by actually using it.

**This exists because `/health/ready` cannot answer the question it looks like
it answers.** That endpoint reports whether a key is *configured* and whether
the pool is currently resting it. It does not report whether the key works: a
revoked key, a mistyped key and a key with a full day's budget all appear
identically as `"available": true, "uses": 0`, right up until the moment a real
request fails. Availability is a statement about cooldown, not about validity.

The only way to know is to spend a token on each one, which is what this does -
one minimal request per key, a few dozen tokens in total against a daily
allowance of 100,000 per key.

Run it before a demo, and after rotating anything:

    python scripts/probe_keys.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/gemini-embedding-001:embedContent"
)

#: The cheapest model on the account, so a health probe never competes with
#: real work for the budget of a model the agent actually plans with.
PROBE_MODEL = "llama-3.1-8b-instant"


def _load_keys(name: str) -> list[str]:
    """Read one comma-separated key variable from the environment or .env."""
    import os

    raw = os.environ.get(name, "")
    if not raw:
        from dotenv import dotenv_values

        root = Path(__file__).resolve().parent.parent
        for candidate in (root / ".env", root / "backend" / ".env"):
            if candidate.exists():
                raw = dotenv_values(candidate).get(name, "") or ""
                if raw:
                    break

    return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]


def _masked(key: str) -> str:
    """Enough of a key to tell it from its siblings, never enough to use."""
    return f"{key[:6]}...{key[-4:]}" if len(key) > 12 else "short-key"


def main() -> int:
    """Probe every configured key and print a verdict per key.

    Returns:
        0 if every key worked, 1 if any did not - so this is usable as a
        pre-demo check in a shell script.
    """
    import httpx

    failures = 0

    # httpx rather than urllib, and not only for convenience: Groq sits behind
    # Cloudflare, which answers urllib's default user agent with a 403 and
    # "error code: 1010". That looks exactly like every key being banned, which
    # is a genuinely alarming thing to see five minutes before a demo.
    with httpx.Client(timeout=45.0) as client:
        groq_keys = _load_keys("GROQ_API_KEY")
        print(f"GROQ - {len(groq_keys)} key(s)")
        print(f"  {'#':<3}{'KEY':<22}{'VERDICT':<14}DETAIL")

        for index, key in enumerate(groq_keys, 1):
            try:
                response = client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": PROBE_MODEL,
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 1,
                    },
                )
                if response.status_code == 200:
                    remaining = response.headers.get("x-ratelimit-remaining-tokens", "?")
                    # Labelled as the per-minute figure on purpose. It is the
                    # only number Groq returns, it is routinely mistaken for
                    # the daily budget, and that mistake cost this project a
                    # completely wrong diagnosis once already.
                    detail = f"minute-window remaining: {remaining}"
                    verdict = "WORKING"
                else:
                    failures += 1
                    verdict = {
                        401: "INVALID KEY",
                        403: "BLOCKED",
                        429: "RATE/QUOTA",
                    }.get(response.status_code, f"HTTP {response.status_code}")
                    detail = _error_text(response)
            except Exception as exc:  # noqa: BLE001 - any failure is a failure
                failures += 1
                verdict, detail = "UNREACHABLE", str(exc)[:60]

            print(f"  {index:<3}{_masked(key):<22}{verdict:<14}{detail}")

        gemini_keys = _load_keys("GEMINI_API_KEY")
        print(f"\nGEMINI - {len(gemini_keys)} key(s)")
        print(f"  {'#':<3}{'KEY':<22}{'VERDICT':<14}DETAIL")

        for index, key in enumerate(gemini_keys, 1):
            try:
                response = client.post(
                    GEMINI_URL,
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json={
                        "model": "models/gemini-embedding-001",
                        "content": {"parts": [{"text": "probe"}]},
                        "outputDimensionality": 768,
                    },
                )
                if response.status_code == 200:
                    vector = response.json().get("embedding", {}).get("values", [])
                    verdict, detail = "WORKING", f"returned {len(vector)}-dim vector"
                else:
                    failures += 1
                    verdict = {
                        400: "BAD KEY/REQ",
                        401: "INVALID KEY",
                        403: "FORBIDDEN",
                        429: "QUOTA",
                    }.get(response.status_code, f"HTTP {response.status_code}")
                    detail = _error_text(response)
            except Exception as exc:  # noqa: BLE001 - any failure is a failure
                failures += 1
                verdict, detail = "UNREACHABLE", str(exc)[:60]

            print(f"  {index:<3}{_masked(key):<22}{verdict:<14}{detail}")

    print()
    if failures:
        print(f"{failures} key(s) did not work. Replace them before demoing.")
        return 1

    print("Every configured key answered a real request.")
    return 0


def _error_text(response: object) -> str:
    """Pull a short human-readable reason out of an error response."""
    try:
        return response.json()["error"]["message"][:60]  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - a body that will not parse is still a clue
        return str(getattr(response, "text", ""))[:60]


if __name__ == "__main__":
    raise SystemExit(main())
