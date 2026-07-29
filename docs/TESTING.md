# Running and testing this yourself

A step-by-step guide to starting the project and verifying every claim it
makes. Written so you can check the work rather than take it on trust.

---

## Contents

1. [Quick start](#1-quick-start)
2. [Check your setup](#2-check-your-setup)
3. [Run the unit tests](#3-run-the-unit-tests)
4. [Start the API](#4-start-the-api)
5. [Test it by hand](#5-test-it-by-hand)
6. [Run the end-to-end test](#6-run-the-end-to-end-test)
7. [Test the frontend](#7-test-the-frontend)
8. [Verify each claim individually](#8-verify-each-claim-individually)
9. [Understanding rate limits](#9-understanding-rate-limits)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Quick start

Assuming `.env` is already filled in:

```bash
# Terminal 1 - API
cd backend
python run.py                       # http://localhost:8000/docs

# Terminal 2 - frontend (optional)
cd frontend
npm run dev                         # http://localhost:3000
```

> **On Windows, use `python run.py`, never `uvicorn` directly.** uvicorn picks
> `ProactorEventLoop` on Windows, which psycopg's async mode cannot use — the
> database silently stops working and the app falls back to in-memory stores.
> `run.py` pins the right loop. Linux, macOS and Docker are unaffected.

---

## 2. Check your setup

Before anything else:

```bash
backend/.venv/Scripts/python.exe scripts/verify_setup.py
```

It checks every credential with a real API call and tells you, per dependency,
whether it works. It also reports each Groq key's **independent** token budget
and how many itineraries per minute that supports — the number that actually
governs throughput.

Expected output ends with:

```
All checks passed (0 warning(s)). Start the API with: python backend/run.py
```

If the database check fails, see [Troubleshooting](#10-troubleshooting).

---

## 3. Run the unit tests

122 tests. No network, no database, no API keys — so they are fast and cannot
fail because a third party had a bad afternoon.

```bash
cd backend
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q
```

> `PYTHONIOENCODING=utf-8` is needed on Windows, otherwise the Japanese and
> Arabic assertions crash the console encoder rather than the test.

Useful subsets:

```bash
# The memory pipeline - dedupe, contradiction, isolation
pytest tests/unit/test_memory_consolidation.py -v

# Multi-key rotation and failover
pytest tests/unit/test_key_rotation.py -v

# That retrieval genuinely chains
pytest tests/unit/test_rag.py -v

# Tool degradation and the no-keyword-routing check
pytest tests/unit/test_tools.py -v

# Coverage
pytest --cov=app --cov-report=term-missing
```

---

## 4. Start the API

```bash
cd backend
python run.py
```

Then confirm it is healthy:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/ready | python -m json.tool
```

`/health/ready` should show `"ready": true`, a `pgvector` version, and both key
pools. If `database` says `not_configured`, the app started in degraded mode —
check the terminal for the reason.

**Interactive API docs: <http://localhost:8000/docs>** — every endpoint with
schemas and a "Try it out" button.

---

## 5. Test it by hand

### Get a token

Everything except `/health` needs a Supabase access token.

```bash
curl -X POST "$SUPABASE_URL/auth/v1/token?grant_type=password" \
  -H "apikey: $SUPABASE_ANON_KEY" \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"..."}'
```

Copy `access_token` from the response.

### Send a message

```bash
TOKEN=<paste>

curl -X POST http://localhost:8000/api/v1/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"Plan me 2 relaxed days in Kyoto. I am vegetarian."}'
```

Takes 30–60 seconds. The response contains the reply **plus the plan and every
tool call**, so you can see what it did.

---

## 6. Run the end-to-end test

Exercises everything against live services:

```bash
backend/.venv/Scripts/python.exe scripts/smoke_test.py \
  --email you@example.com --password 'your-password'
```

Cheap checks only (auth, health, memory read, admin — no agent runs):

```bash
python scripts/smoke_test.py --email ... --password ... --quick
```

The full run costs roughly four agent runs — 100k–200k Groq tokens. On a
two-key free tier that is several minutes of budget, so some checks may report
`[SKIP] rate limited`. That is a quota ceiling, not a bug: wait a minute, or
add more keys.

---

## 7. Test the frontend

```bash
cd frontend
npm run dev        # http://localhost:3000
```

1. **Sign in** at `/login` (email/password, or Google if you configured it).
2. **Chat** at `/` — send *"Plan me 2 relaxed days in Kyoto, I'm vegetarian"*.
   When the reply arrives, click **"Show what it did"** to see the plan and
   every tool call with latencies.
3. **Memory** at `/memories` — the facts extracted about you, with confidence
   and mention counts. Toggle *"Show superseded memories"* to see how a
   preference changed over time.
4. **Admin** at `/admin` (admin accounts only) — run traces, tool analytics,
   memory health, users, audit log. Click any run id for the full trace.

---

## 8. Verify each claim individually

Each of these targets one thing the project claims to do.

### "It plans before acting"

Send any itinerary request and read `plan` in the response. Then open the
admin trace (`/api/v1/admin/runs/{run_id}`) and compare `initial_plan` against
the executed `steps` — if `replan_count > 0` you can see where the plan
changed.

### "The model chooses tools — nothing is keyword-routed"

Send these three and compare `tool_calls`:

| Message | Expected tools |
|---|---|
| `Will it rain in Kyoto next week?` | weather only |
| `Plan 2 days in Kyoto` | travel guide, places, weather… |
| `Any vegan ramen in Tokyo?` | places and/or web search |

Same toolbox, different choices. The structural proof is in
`tests/unit/test_tools.py::test_no_hardcoded_keyword_routing_exists`, which
scans the codebase for the `if "weather" in message:` pattern.

### "Long-term memory works across sessions"

This is the headline claim, so test it deliberately:

1. Send: *"Plan 2 days in Kyoto. I'm vegetarian and I hate crowds."*
2. Wait ~30 seconds (extraction runs in the background after the reply).
3. `GET /api/v1/me/memories` → should show `Traveller is vegetarian` and
   `Traveller hates crowds` as **constraints**. It should *not* store "going to
   Kyoto" — that is trip-specific and useless next time.
4. Send a message with **no `session_id`** (a brand-new conversation) about a
   **different city**: *"Plan one day in Lisbon."*
5. The reply should apply vegetarian and crowd-avoidance **without you
   restating them**, and should not ask about either.

### "Repeating yourself doesn't duplicate memories"

Say *"I'm vegetarian"* in three separate conversations, then check
`/api/v1/me/memories`. You should see **one** entry with `mention_count: 3`,
not three entries.

### "Retrieval is multi-hop"

Open a run trace and find a `search_travel_guide` call. Its result contains
`hops`: hop 1 searches the city article, hop 2 searches *specific district
sub-articles* whose names hop 1 discovered. Hop 2's documents cannot appear
before hop 1 runs — that is what makes it chained rather than three searches
in a row.

### "It asks when a request is too vague"

Send *"I want to go somewhere nice"* → `needs_clarification: true`, status
`clarifying`, and a short question rather than a generic itinerary.

But it must **not** ask about things it already knows: once vegetarian is
stored, no request should ask about dietary needs again.

### "It replies in your language"

```json
{"message": "京都で2日間の旅程を立ててください。"}
```

→ `detected_language: "ja"` and a Japanese reply, with place names in local
script plus a romanised form in brackets. The *retrieval* still runs in
English against the English corpus, so answer quality does not depend on the
language you asked in.

### "A failing tool doesn't fail the run"

Break one on purpose — set `GEOAPIFY_API_KEY=invalid` in `.env` and restart.
Ask for an itinerary. You should still get one; the reply should mention that
some venue details could not be verified, and the trace should show that tool
as `degraded` while others succeeded.

### "Users are isolated from each other"

Create a second account, hold a conversation with each, then confirm that
`GET /api/v1/sessions` for user A never returns user B's sessions. This is
enforced by row-level security in the database, not by application code — a
query that forgot its `WHERE` clause would still return only your rows.

---

## 9. Understanding rate limits

**The binding constraint is tokens per minute, not requests per day.**

Measured from the live Groq API:

| Model | Tokens/min | Requests/day |
|---|---|---|
| `llama-3.3-70b-versatile` | **12,000** | 1,000 |
| `openai/gpt-oss-120b` | 8,000 | 1,000 |
| `llama-3.1-8b-instant` | 6,000 | 14,400 |

A full itinerary costs **30,000–50,000 tokens**, because the executor resends
its tool schemas and accumulated findings on every reasoning round trip. So one
run can exceed a single key's per-minute budget, while barely touching the
daily request allowance.

**Each key adds its own 12,000 tokens/minute** — verified experimentally:
spending 2,109 tokens on one key left the other's counter completely
untouched. So:

| Keys | Tokens/min | Realistic throughput |
|---|---|---|
| 1 | 12,000 | one run, then wait |
| 2 | 24,000 | one run comfortably; back-to-back runs stall |
| **4** | **48,000** | **back-to-back runs work** |
| 6 | 72,000 | comfortable for a demo with several users |

Add them comma-separated — keys from **separate Groq accounts**, since keys on
one account share a quota:

```
GROQ_API_KEY=gsk_first,gsk_second,gsk_third,gsk_fourth
```

`scripts/verify_setup.py` reports your combined budget and estimated
throughput.

When every key is momentarily spent, the agent says so plainly — *"I hit my
request limit part-way through planning, try again in a minute"* — rather than
blaming the travel data sources, which answered fine.

---

## 10. Troubleshooting

**`/health/ready` says `database: not_configured`**

On Windows, you almost certainly started with `uvicorn` instead of
`python run.py`. See the warning in [Quick start](#1-quick-start).

Otherwise, check `DATABASE_URL` uses the **session pooler on port 5432**, not
the transaction pooler on 6543 — the LangGraph checkpointer needs prepared
statements, which 6543 does not support.

**`function public.match_memories(...) does not exist`**

Migrations were not applied, or only some were. Run:

```bash
backend/.venv/Scripts/python.exe scripts/apply_migrations.py
```

**Wikivoyage returns 403**

`HTTP_USER_AGENT` must name your application and a real contact address —
Wikimedia's robot policy rejects generic agents. Format:

```
HTTP_USER_AGENT=TripPlannerAgent/0.1 (https://github.com/you/repo; you@example.com)
```

**Admin endpoints return 403**

Your account is not an admin. In the Supabase SQL editor:

```sql
select public.promote_to_admin('you@example.com');
```

The account must have signed in at least once — the profile row is created by
trigger on first signup.

**`UnicodeEncodeError` running tests on Windows**

```bash
PYTHONIOENCODING=utf-8 pytest -q
```

**Runs fail with "I hit my request limit"**

Not a bug — see [Understanding rate limits](#9-understanding-rate-limits).
Wait a minute or add more Groq keys.

**First request after a while is very slow**

Expected. On free hosting the service sleeps after 15 minutes idle and takes
30–60 seconds to wake. A city nobody has asked about before is also slower the
first time while its guide articles are fetched and embedded; the second
request for that city is much faster.
