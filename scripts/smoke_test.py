"""End-to-end functional test against a running API.

Proves the claims this project makes, rather than asking you to trust them:
dynamic tool selection, the planning loop, long-term memory persisting across
sessions, clarification, multilingual replies, and the admin trace.

    # terminal 1
    python backend/run.py

    # terminal 2
    python scripts/smoke_test.py --email you@example.com --password ...

Costs real API quota - roughly 4 agent runs, so 100,000-200,000 Groq tokens.
On a two-key free tier that is several minutes of budget, and some checks may
fail with a rate-limit message. That is a quota ceiling, not a defect; wait a
minute and re-run, or add more keys to GROQ_API_KEY.

Use `--quick` to run only the cheap checks (auth, health, memory read, admin).
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
PASS, FAIL, SKIP = "[ OK ]", "[FAIL]", "[SKIP]"

results: list[tuple[str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    """Record and print one assertion."""
    marker = PASS if ok else FAIL
    results.append((marker, label))
    print(f"  {marker} {label}" + (f" - {detail}" if detail else ""))
    return ok


def note(label: str) -> None:
    """Record a skipped check."""
    results.append((SKIP, label))
    print(f"  {SKIP} {label}")


def env_value(name: str) -> str:
    """Read one value from the root `.env`."""
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return ""


def main() -> None:
    """Run the smoke test."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument(
        "--quick", action="store_true", help="Skip the expensive agent runs."
    )
    arguments = parser.parse_args()

    supabase_url = env_value("SUPABASE_URL")
    anon_key = env_value("SUPABASE_ANON_KEY")
    if not supabase_url or not anon_key:
        print("SUPABASE_URL / SUPABASE_ANON_KEY missing from .env")
        sys.exit(1)

    client = httpx.Client(timeout=300)

    print("=" * 70)
    print("Trip Planner - end-to-end smoke test")
    print("=" * 70)

    # -- Auth ---------------------------------------------------------------
    print("\n[1] AUTHENTICATION")
    response = client.post(
        f"{supabase_url}/auth/v1/token?grant_type=password",
        headers={"apikey": anon_key},
        json={"email": arguments.email, "password": arguments.password},
    )
    if response.status_code != 200:
        print(f"  {FAIL} sign-in failed: {response.status_code} {response.text[:160]}")
        sys.exit(1)
    auth = {"Authorization": f"Bearer {response.json()['access_token']}"}
    check(True, "signed in")
    check(
        client.get(f"{arguments.api}/api/v1/sessions").status_code == 401,
        "unauthenticated request rejected",
    )

    # -- Health -------------------------------------------------------------
    print("\n[2] HEALTH AND KEY POOLS")
    ready = client.get(f"{arguments.api}/health/ready").json()
    check(ready["ready"], "service reports ready")
    check(
        bool(ready["checks"]["database"].get("ok")),
        "database connected",
        f"pgvector {ready['checks']['database'].get('pgvector')}",
    )
    for pool in ready["checks"].get("key_pools", []):
        check(
            pool["available_keys"] > 0,
            f"{pool['provider']} pool: {pool['available_keys']}/{pool['total_keys']} keys ready",
        )

    if arguments.quick:
        print("\n[3-6] SKIPPED (--quick)")
        note("agent planning")
        note("memory across sessions")
        note("multilingual")
    else:
        # -- Planning and tool selection -------------------------------------
        print("\n[3] PLANNING AND DYNAMIC TOOL SELECTION")
        started = time.time()
        turn = client.post(
            f"{arguments.api}/api/v1/chat",
            headers=auth,
            json={
                "message": "Plan me 2 relaxed days in Kyoto. I'm vegetarian and I hate crowds."
            },
        ).json()
        print(f"      ({time.time() - started:.0f}s, status={turn['status']})")

        if turn["status"] == "failed" and "limit" in turn["response"].lower():
            print(f"  {SKIP} rate limited - add more keys to GROQ_API_KEY and re-run")
            note("planning (rate limited)")
        else:
            check(len(turn["plan"]) >= 2, f"produced a {len(turn['plan'])}-step plan")
            for step in turn["plan"]:
                print(f"         [{step['kind']}] {step['description'][:66]}")
            tools = [call["tool"] for call in turn["tool_calls"]]
            check(
                len(tools) >= 2,
                f"called {len(set(tools))} distinct tools",
                str(set(tools)),
            )
            check(
                "vegetarian" in turn["response"].lower(),
                "honoured the dietary constraint",
            )

            print("\n[4] TOOL SELECTION VARIES BY QUESTION")
            weather = client.post(
                f"{arguments.api}/api/v1/chat",
                headers=auth,
                json={"message": "Will it rain in Singapore in the next 3 days?"},
            ).json()
            weather_tools = {call["tool"] for call in weather["tool_calls"]}
            check(
                weather_tools != set(tools),
                "a different question chose different tools",
                str(weather_tools),
            )

        # -- Memory ----------------------------------------------------------
        print("\n[5] LONG-TERM MEMORY")
        memories: list[dict] = []
        for _ in range(10):
            time.sleep(5)
            memories = client.get(
                f"{arguments.api}/api/v1/me/memories", headers=auth
            ).json()
            if memories:
                break

        check(len(memories) > 0, f"extracted {len(memories)} durable facts")
        for memory in memories:
            print(
                f"         [{memory['memory_type']}/{memory['subject']}] {memory['content']}"
                f"  (x{memory['mention_count']})"
            )
        check(
            not any("kyoto" in m["content"].lower() for m in memories),
            "trip-specific detail correctly NOT stored",
        )

        print("\n[6] MEMORY APPLIED IN A NEW SESSION, DIFFERENT CITY")
        lisbon = client.post(
            f"{arguments.api}/api/v1/chat",
            headers=auth,
            json={"message": "Plan one day in Lisbon for me."},
        ).json()
        body = lisbon["response"].lower()
        if lisbon["status"] == "failed" and "limit" in body:
            print(f"  {SKIP} rate limited")
            note("memory across sessions (rate limited)")
        else:
            check(
                any(
                    w in body
                    for w in ("vegetarian", "veggie", "plant-based", "meat-free")
                ),
                "vegetarian applied WITHOUT being restated",
            )
            check(not lisbon["needs_clarification"], "did not re-ask what it knew")

    # -- Admin ---------------------------------------------------------------
    print("\n[7] ADMIN PORTAL")
    users = client.get(f"{arguments.api}/api/v1/admin/users", headers=auth)
    if users.status_code == 403:
        print(
            f"  {SKIP} not an admin - run: select public.promote_to_admin('{arguments.email}');"
        )
        note("admin portal (not an admin)")
    else:
        check(users.status_code == 200, f"listed {len(users.json())} users")
        runs = client.get(f"{arguments.api}/api/v1/admin/runs", headers=auth).json()
        check(len(runs) > 0, f"{len(runs)} agent runs recorded")
        if runs:
            trace = client.get(
                f"{arguments.api}/api/v1/admin/runs/{runs[0]['id']}", headers=auth
            ).json()
            check(
                len(trace.get("steps", [])) > 0,
                f"trace has {len(trace['steps'])} steps",
            )
            check(
                len(trace.get("tool_calls", [])) > 0,
                f"trace has {len(trace['tool_calls'])} tool calls with arguments",
            )
        analytics = client.get(
            f"{arguments.api}/api/v1/admin/analytics/tools", headers=auth
        ).json()
        for row in analytics:
            print(
                f"         {row['tool_name']}: {row['calls']} calls, "
                f"p95 {row['p95_latency_ms']}ms"
            )

    # -- Summary -------------------------------------------------------------
    passed = sum(1 for marker, _ in results if marker == PASS)
    failed = sum(1 for marker, _ in results if marker == FAIL)
    skipped = sum(1 for marker, _ in results if marker == SKIP)

    print("\n" + "=" * 70)
    print(f"{passed} passed, {failed} failed, {skipped} skipped")
    if failed:
        print("\nFailed:")
        for marker, label in results:
            if marker == FAIL:
                print(f"  {FAIL} {label}")
    print("=" * 70)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
