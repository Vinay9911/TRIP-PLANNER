"""Check that every credential and service this project needs actually works.

Run this first, before starting the app. It answers "is my setup correct?"
with a specific verdict per dependency rather than a stack trace forty seconds
into a request.

    python scripts/verify_setup.py

Every check is read-only and cheap. The Groq check additionally reports each
key's independent rate-limit headroom, which is the number that actually
governs how many itineraries you can plan per minute.
"""

from __future__ import annotations

import os
import pathlib
import sys

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
PASS, FAIL, WARN = "[ OK ]", "[FAIL]", "[WARN]"

failures = 0
warnings = 0


def report(ok: bool | None, label: str, detail: str = "") -> None:
    """Print one check result and track the failure count."""
    global failures, warnings
    if ok is None:
        marker = WARN
        warnings += 1
    elif ok:
        marker = PASS
    else:
        marker = FAIL
        failures += 1
    print(f"  {marker} {label}" + (f" - {detail}" if detail else ""))


def load_env() -> dict[str, str]:
    """Read `.env` from the repository root into a dict."""
    path = ROOT / ".env"
    if not path.exists():
        print(f"{FAIL} No .env at {path}. Copy .env.example and fill it in.")
        sys.exit(1)

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def split_keys(raw: str) -> list[str]:
    """Split a comma-separated key list."""
    return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]


def check_groq(raw: str) -> None:
    """Verify each Groq key and report its independent quota."""
    print("\nGroq (language model)")
    keys = split_keys(raw)
    if not keys:
        report(False, "no key configured")
        return

    total_tpm = 0
    for index, key in enumerate(keys, start=1):
        label = f"key {index} (...{key[-6:]})"
        try:
            response = httpx.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                },
                timeout=60,
            )
        except Exception as exc:  # noqa: BLE001
            report(False, label, str(exc)[:70])
            continue

        if response.status_code != 200:
            report(False, label, f"HTTP {response.status_code}")
            continue

        limit = response.headers.get("x-ratelimit-limit-tokens", "?")
        left = response.headers.get("x-ratelimit-remaining-tokens", "?")
        requests_left = response.headers.get("x-ratelimit-remaining-requests", "?")
        report(
            True,
            label,
            f"{left}/{limit} tokens/min free, {requests_left} requests left today",
        )
        if limit.isdigit():
            total_tpm += int(limit)

    if not total_tpm:
        return

    # Estimated, not exact. Measured: a run that only planned and replanned,
    # calling no tools, cost 5,446 tokens. Every executor step adds the eight
    # tool schemas plus the accumulated findings again, and a full itinerary
    # runs three to five such steps - so tens of thousands is the right order.
    # Treat this as a planning figure; `total_tokens` on each chat response
    # reports what a request actually cost.
    cost_per_run = 35_000
    print(
        f"\n  Combined budget: {total_tpm:,} tokens/minute across {len(keys)} key(s)."
    )
    print(f"  A full itinerary costs roughly {cost_per_run:,} tokens (estimate;")
    print("  each chat response reports its real cost as `total_tokens`).")

    if total_tpm >= cost_per_run * 2:
        print("  -> Comfortable: several itineraries per minute.")
    elif total_tpm >= cost_per_run:
        print("  -> Workable: about one itinerary per minute.")
    else:
        minutes = cost_per_run / total_tpm
        shortfall = cost_per_run * 2 - total_tpm
        extra = -(-shortfall // 12_000)
        print(f"  -> Tight: one itinerary needs about {minutes:.1f} minutes of budget,")
        print("     so back-to-back requests will stall part-way through.")
        print(f"     Add {extra} more key(s) to GROQ_API_KEY (comma-separated).")
        print("     They must be from SEPARATE Groq accounts - keys on one account")
        print("     share a single quota, so rotating between them gains nothing.")


def check_gemini(raw: str) -> None:
    """Verify each Gemini key can produce an embedding."""
    print("\nGoogle Gemini (embeddings)")
    keys = split_keys(raw)
    if not keys:
        report(False, "no key configured")
        return

    for index, key in enumerate(keys, start=1):
        label = f"key {index} (...{key[-6:]})"
        try:
            response = httpx.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-embedding-001:embedContent?key={key}",
                json={
                    "model": "models/gemini-embedding-001",
                    "content": {"parts": [{"text": "healthcheck"}]},
                    "outputDimensionality": 768,
                },
                timeout=60,
            )
            values = response.json().get("embedding", {}).get("values", [])
            report(len(values) == 768, label, f"{len(values)} dimensions")
        except Exception as exc:  # noqa: BLE001
            report(False, label, str(exc)[:70])


def check_tavily(key: str) -> None:
    """Verify the Tavily search key."""
    print("\nTavily (web search)")
    if not key:
        report(None, "not configured - web search will degrade gracefully")
        return
    try:
        response = httpx.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {key}"},
            json={"query": "test", "max_results": 1},
            timeout=60,
        )
        report(response.status_code == 200, "key valid", f"HTTP {response.status_code}")
    except Exception as exc:  # noqa: BLE001
        report(False, "key check failed", str(exc)[:70])


def check_geoapify(key: str) -> None:
    """Verify the Geoapify places key."""
    print("\nGeoapify (places)")
    if not key:
        report(None, "not configured - place lookup will degrade gracefully")
        return
    try:
        response = httpx.get(
            "https://api.geoapify.com/v2/places",
            params={
                "categories": "catering.restaurant",
                "filter": "circle:135.76,35.01,1000",
                "limit": 1,
                "apiKey": key,
            },
            timeout=60,
        )
        report(response.status_code == 200, "key valid", f"HTTP {response.status_code}")
    except Exception as exc:  # noqa: BLE001
        report(False, "key check failed", str(exc)[:70])


def check_keyless_services(env: dict[str, str]) -> None:
    """Verify the two services that need no credentials."""
    print("\nKeyless services")
    try:
        response = httpx.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": "Kyoto", "count": 1},
            timeout=60,
        )
        report(response.status_code == 200, "Open-Meteo (weather) reachable")
    except Exception as exc:  # noqa: BLE001
        report(False, "Open-Meteo unreachable", str(exc)[:60])

    agent = env.get("HTTP_USER_AGENT", "")
    try:
        response = httpx.get(
            env.get("WIKIVOYAGE_API_BASE_URL", "https://en.wikivoyage.org/w/api.php"),
            params={
                "action": "query",
                "list": "allpages",
                "apprefix": "Kyoto/",
                "aplimit": 3,
                "format": "json",
            },
            headers={"User-Agent": agent} if agent else {},
            timeout=60,
        )
        if response.status_code == 403:
            report(
                False,
                "Wikivoyage returned 403",
                "HTTP_USER_AGENT must name your app and a real contact address",
            )
        else:
            count = len(response.json().get("query", {}).get("allpages", []))
            report(
                response.status_code == 200,
                "Wikivoyage reachable",
                f"{count} districts found",
            )
    except Exception as exc:  # noqa: BLE001
        report(False, "Wikivoyage unreachable", str(exc)[:60])


def check_database(url: str) -> None:
    """Verify the database, schema and pgvector extension."""
    print("\nSupabase Postgres")
    if not url:
        report(False, "DATABASE_URL not set")
        return

    if ":6543" in url:
        report(
            False,
            "using the transaction pooler (port 6543)",
            "switch to the session pooler on 5432 - the checkpointer needs prepared statements",
        )
        return

    try:
        import psycopg
    except ImportError:
        report(None, "psycopg not installed - run from the backend venv")
        return

    try:
        with psycopg.connect(url, connect_timeout=30) as conn:
            report(True, "connection established")

            (tables,) = conn.execute(
                "select count(*) from information_schema.tables where table_schema = 'public'"
            ).fetchone()
            report(tables >= 11, f"{tables} tables present", "expected at least 11")

            row = conn.execute(
                "select extversion from pg_extension where extname = 'vector'"
            ).fetchone()
            report(row is not None, f"pgvector {row[0] if row else 'MISSING'}")

            (policies,) = conn.execute(
                "select count(*) from pg_policies where schemaname = 'public'"
            ).fetchone()
            report(policies >= 15, f"{policies} row-level-security policies")

            (admins,) = conn.execute(
                "select count(*) from public.profiles where app_role = 'admin'"
            ).fetchone()
            report(
                admins > 0 or None,
                f"{admins} admin account(s)",
                ""
                if admins
                else "run: select public.promote_to_admin('you@example.com');",
            )
    except Exception as exc:  # noqa: BLE001
        report(False, "connection failed", str(exc)[:90])


def main() -> None:
    """Run every check and summarise."""
    print("=" * 70)
    print("Trip Planner - setup verification")
    print("=" * 70)

    env = load_env()
    for key, value in env.items():
        os.environ.setdefault(key, value)

    check_groq(env.get("GROQ_API_KEY", ""))
    check_gemini(env.get("GEMINI_API_KEY", ""))
    check_tavily(env.get("TAVILY_API_KEY", ""))
    check_geoapify(env.get("GEOAPIFY_API_KEY", ""))
    check_keyless_services(env)
    check_database(env.get("DATABASE_URL", ""))

    print("\n" + "=" * 70)
    if failures:
        print(
            f"{failures} check(s) FAILED, {warnings} warning(s). Fix the above before running."
        )
        sys.exit(1)
    print(
        f"All checks passed ({warnings} warning(s)). Start the API with: python backend/run.py"
    )
    print("=" * 70)


if __name__ == "__main__":
    main()
