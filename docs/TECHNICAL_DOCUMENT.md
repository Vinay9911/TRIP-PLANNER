# Trip Planner Agent — Technical Document

An AI travel-planning agent built around three requirements: **dynamic tool
use**, **layered memory**, and **multi-hop retrieval**. This document covers
the architecture, the tools, how to run it, what has actually been measured,
and where it falls short.

It is self-contained. Deeper material lives in
[ARCHITECTURE.md](ARCHITECTURE.md) (design rationale),
[WORKFLOW.md](WORKFLOW.md) (a plain-language walkthrough),
[TESTING.md](TESTING.md) (a verification runbook) and
[AUDIT.md](AUDIT.md) (a measured diagnostic pass).

| | |
|---|---|
| **Stack** | Python 3.12 · FastAPI · LangGraph 1.2 · LangChain 1.3 · Next.js 15 · Postgres + pgvector |
| **Models** | Groq (three tiers) for inference, Gemini for embeddings, Ollama as a local fallback |
| **Tests** | 248, passing, requiring no network, database or API keys |
| **Deployment** | Render (API) · Vercel (web) · Supabase (database) — all free tier |

---

## 1. Architecture

### 1.1 The shape of it

The assignment names a **Plan-and-Execute** agent. LangChain 1.x removed its
prebuilt implementation, so the loop is built explicitly as a LangGraph
`StateGraph`:

```
START → understand ─┬─(vague)──────→ respond → END
                    ├─(exploring)──→ advise  → END
                    └─(ready)──────→ plan → execute ⇄ replan → respond → END
```

Six nodes, each with one job:

| Node | Responsibility |
|---|---|
| `understand` | Extract goal, destination, dates, language and trip slots; recall long-term memory |
| `advise` | One-hop retrieval → grounded options and at most two questions |
| `plan` | Decompose the goal into 3–8 typed steps |
| `execute` | Carry out a step with tools (this is where `create_agent` is used) |
| `replan` | Look at what happened and revise, truncate or continue |
| `respond` | Compose the answer, backfill map coordinates, attach provenance |

**Why hand-built rather than `create_agent` alone.** `create_agent` is a ReAct
loop: it decides one tool call at a time and never plans ahead. The brief asks
for planning, so the planning loop is explicit in the graph — and each
individual step is delegated *to* `create_agent`, because its inner
tool-calling loop is a solved problem and reimplementing tool-call parsing and
error recovery would buy nothing. The honest answer to "why not just use
`create_agent`?" is that it *is* used, for the part it is good at.

**The executor sees one step, not the plan.** Given the whole plan it runs
ahead and attempts later steps, which defeats planning and makes the
replanner's view of progress wrong.

**Termination has four independent routes**, because a single exit condition
is a single point of hanging: the plan runs out; the replanner decides the
evidence is sufficient; the replan budget is spent; or LangGraph's own
`recursion_limit` backstops all three.

### 1.2 Three gears, not one

The most-used decision in the system is *how much work this message deserves*.

| Gear | Trigger | Cost |
|---|---|---|
| `clarify` | No destination at all, or a flight request with no origin | 1 model call |
| `advise` | A destination but no trip yet | ~3k tokens, one retrieval hop |
| `plan` | Duration + a date signal, "just plan it", or a scoped ask | Full pipeline |

The gear is chosen by **pure code over model-extracted slots**
(`agent/trip_state.py::decide_mode`), never by model judgement. That is a
deliberate correction: the model was asked to decide and got it wrong in a way
users noticed — after "trip to usa" it asked *"Which city or country did you
have in mind?"*, a question already answered in the first sentence. The prompt
forbade exactly that and the model did it anyway. Extraction is a model task;
routing is a code task.

Slots persist per conversation in `sessions.trip_state` (JSONB). Absence never
erases: "make day 2 lighter" mentions no duration, and forgetting the duration
because of it would make every refinement destroy the trip.

### 1.3 Data model and isolation

Postgres with pgvector, not a dedicated vector database. Users, conversations,
memories, traces and embeddings live in one database, so "show me this user's
memories alongside their runs" is a join rather than a distributed transaction
across two systems that can disagree.

Isolation is enforced **by the database, not by the application**. The backend
connects as `postgres`, which would bypass row level security; so
`db.user_scope(user_id)` sets `request.jwt.claims` and issues `set local role
authenticated` inside the transaction. After that, a query that forgets its
`WHERE` clause returns the caller's rows and nothing else. Both settings are
transaction-local and cannot leak into the next borrower of a pooled
connection. System work goes through `db.service_scope(reason=...)` — a
separate, greppable name, so an audit of privileged access is one search.

### 1.4 Authentication

JWTs are verified locally against Supabase's JWKS: no network round trip per
request. Signatures are verified, algorithms are pinned to `ES256`/`RS256`
(accepting the token's own `alg` allows `alg: none`; mixing HS256 with an
asymmetric key lets an attacker sign tokens with the *public* key as an HMAC
secret), and the admin role is read from the database rather than any token
claim, because Supabase user metadata is writable from the user's own browser.

**There is no login wall.** Opening the planner provisions an anonymous
Supabase session, so a reviewer reaches a working agent in one click. Nothing
above changes: an anonymous session is a real identity with a signed JWT and a
real `sub`, so RLS still isolates every traveller. The alternative — dropping
auth and hardcoding a demo user — would have been less work and would have
collapsed every visitor onto one profile, so one person's dietary constraint
would appear in another person's itinerary.

---

## 2. Tools used

### 2.1 The toolbox

Eight tools. **Nothing is keyword-routed** — no code anywhere inspects a user
message to decide which tool to run. The model chooses from the descriptions
alone, every time.

| Tool | Backing service | Key | Notes |
|---|---|---|---|
| `search_travel_guide` | Wikivoyage | no | Multi-hop RAG; the primary research tool |
| `find_places` | Geoapify / OpenStreetMap | free | Filters at source on diet, access, pets |
| `get_weather_forecast` | Open-Meteo | **none** | Beyond ~16 days returns labelled seasonal averages |
| `search_web` | Tavily | free | Time-sensitive facts only |
| `search_flights` | Mock provider | no | **Simulated** — see §5 |
| `search_accommodation` | Mock provider | no | **Simulated** — see §5 |
| `recall_user_preferences` | Long-term memory | — | |
| `save_user_preference` | Long-term memory | — | |

Observed selection: *"Will I need an umbrella in Kyoto?"* → weather only.
*"Plan 2 days in Kyoto"* → guide → places → weather → sometimes web search.

### 2.2 Descriptions are prompt engineering, and they are billed

Tool docstrings are serialised into the schema and **re-sent on every
iteration** of the executor's ReAct loop. Measured against the live API, the
original prose descriptions came to 15,032 characters — about 3,758 tokens per
call and roughly 37,600 across one plan, a third of a day's budget spent on
tool descriptions alone.

So docstrings say only what the model needs to pick a tool and fill its
arguments; the reasoning a human reader wants lives in comments, which cost
nothing at inference time. `get_tools_for_step` narrows the toolbox further by
step kind — a research step cannot book a flight. Together these cut schema
cost by 68%.

### 2.3 Constraints are filters, not hints

A remembered "vegetarian" is passed to `find_places` as a `conditions`
argument and to `search_travel_guide` as a `constraints` argument. It filters
at the source rather than asking the model to discard results afterwards,
which is both cheaper and more reliable — a model asked to filter its own
retrieved list will sometimes keep a steakhouse.

### 2.4 Tools never raise

Every tool returns a `ToolResult` with a status. A degraded result's `message`
is written **for the model** and must say what to do instead *and* whether to
retry — without that, models loop on a failing call. A dead weather API costs
the forecast, not the itinerary.

### 2.5 A guard the framework does not provide

`recursion_limit` looks like it caps tool calls and does not: it counts
LangGraph super-steps, and every tool call in one assistant message shares a
super-step. A model calling tools in parallel escapes it entirely. Observed in
a real trace: **43 `search_accommodation` calls in a single step**, 194s
against a 90s timeout, 156,870 tokens for one reply.

The cap is enforced in `_budgeted_tools`, which also memoises identical calls —
worth more than the cap, since most of the 43 were the same query repeated. A
memo hit returns the cached data and does **not** consume budget, because
charging for a question already answered would starve the genuinely new
lookups the guard exists to protect.

---

## 3. Memory

Two systems, deliberately separate.

**Short-term** is the conversation: a LangGraph Postgres checkpointer keyed by
`session_id`, so context survives a restart.

**Long-term** is a pipeline, not a saved chat log:

```
heuristic gate → extract → confidence floor → embed
     → consolidate (reinforce / arbitrate / insert) → store
```

Three parts are worth defending.

**The gate is a cost filter, not a correctness filter.** Most turns contain
nothing durable, and running a model over every "sounds good, thanks" would
burn the daily quota producing empty lists. It is tuned to over-admit: a false
positive costs one cheap call, a false negative loses a fact permanently. It
carries **two length floors** — 12 characters for Latin scripts, 4 for
CJK/Arabic — because a single floor silently disabled memory for exactly the
users the multilingual support exists for. `私はベジタリアンです` ("I am
vegetarian") is ten characters. There is a regression test.

**Consolidation is the part that matters.** A new candidate is compared by
cosine similarity against what is already known, producing three bands:
*duplicate* (≥ 0.90 — reinforce, increment mention count), *related and
possibly contradictory* (0.75–0.90 — arbitrate, retire the loser with an audit
trail), *unrelated* (< 0.75 — insert). Tell it you are vegetarian five times
and it stores **one** memory with a mention count of five. A validator enforces
that the conflict threshold stays below the dedupe threshold, because
inverting them makes the arbitration band vanish.

**Retrieval unions constraints in unconditionally.** Pure similarity search is
right for preferences: planning Kyoto need not surface a note about liking
Nordic design. It is *wrong* for constraints. "Traveller is allergic to nuts"
has almost no cosine similarity to "plan me two days in Kyoto" — the words
share no meaning — so under pure ranking the allergy is ranked out by a note
about temples. Constraints are therefore retrieved by *type*, regardless of
score. It costs a handful of tokens per turn and removes an entire class of
harm. This is a correctness decision dressed as a retrieval decision, which is
why it is not exposed as a tunable threshold.

**Memory is written after the response is sent**, as a background task.
Extraction costs a model call plus embedding calls; its benefit lands on the
traveller's *next* session, so making them wait for it would be paying latency
now for value later.

**Memory suppresses redundant questions.** Recall happens *before* the
clarification decision, not after, and retrieved facts are injected into it.
Otherwise the agent asks "what's your budget?" of someone who answered that
three sessions ago — precisely the behaviour long-term memory exists to
eliminate.

---

## 4. Multi-hop RAG

"Multi-hop" means each retrieval's **query is built from the previous
retrieval's results**. Running three searches whose queries were all derivable
from the original question is not multi-hop, and the distinction is easy to
fake, so the chaining here is traceable:

| Hop | Name | Query derived from |
|---|---|---|
| 1 | orient | The user request. Fetch the city article, read its **actual district list from the API** |
| 2 | narrow | **Hop 1's district list.** A model picks 2–4 that suit this traveller, choosing only from names that exist; those articles are fetched and indexed |
| 3 | constrain | **Hop 2's selected districts** × each stated constraint, one search each |
| 4 | fill gaps | **A sufficiency check over hops 1–3**, which names what is missing |

Hop 2 cannot run without hop 1: the district names are looked up, not guessed,
and a hallucinated name is filtered against the real list at no cost.

**Wikivoyage was chosen because it makes this real.** Districts are addressable
subpages (`Kyoto/Higashiyama`), which is what gives hop 2 something genuine to
chain into. A Wikipedia dump has no district structure and would have made the
"multi-hop" claim decorative.

**One search per constraint, not one combined search.** Embedding "vegetarian
AND wheelchair accessible" produces a vector near neither — the classic
multi-constraint retrieval failure. They run concurrently, since three parallel
embedding calls complete in roughly the time one takes.

**Stop conditions are as important as the hops.** Unbounded
retrieve-reason-retrieve loops are the dominant failure mode of agentic RAG.
Four guards: a hop ceiling, an early exit when a hop adds no new documents, an
early exit when the sufficiency check passes, and a cap on districts per query.

**A bug worth recording.** Wikivoyage redirects heavily — `Mumbai/Colaba and
Fort` resolves to `Mumbai/South`, and several requested titles can collapse
onto one article. The index recorded the *requested* title and then searched
for it, so hops 2, 3 and 4 returned zero chunks for any destination with
redirects, silently degrading the headline feature to a single hop while still
paying for every hop's embedding call. Always index *and* search by
`article.title` after fetching. Regression tests in `test_rag_redirects.py`.

---

## 5. Setup instructions

### 5.1 Prerequisites

Python 3.11–3.13, Node 18+, and a Supabase project. Free accounts for Groq,
Google AI Studio, Tavily and Geoapify. Open-Meteo and Wikivoyage need no key.

### 5.2 Backend

```bash
cd backend
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"      # Windows
# source .venv/bin/activate && pip install -e ".[dev]"     # macOS/Linux
```

### 5.3 Database

Run `supabase/migrations/0001…0009` in order in the Supabase SQL editor, or:

```bash
python scripts/apply_migrations.py
```

Then in the dashboard, **Authentication → Sign In / Providers → enable "Allow
anonymous sign-ins"**. It is off by default in new projects, and without it
every visitor sees "Could not start a session".

### 5.4 Configuration

```bash
cp .env.example .env
```

Every variable is documented in place. Two that reliably cost an hour if got
wrong:

- **`DATABASE_URL` must use the session pooler (port 5432), not the
  transaction pooler (6543).** The LangGraph checkpointer needs prepared
  statements, which the transaction pooler does not support.
- **`HTTP_USER_AGENT` must name the client and a real contact.** Wikimedia
  answers a generic User-Agent with 403 and a link to its robot policy. This
  is a hard requirement, verified against the live API, not etiquette.

### 5.5 Frontend

```bash
cd frontend
npm install
cp .env.example .env.local     # fill in Supabase URL, anon key, API URL
```

`NEXT_PUBLIC_MAPBOX_TOKEN` is optional; without it every panel still works and
the map area stays blank.

### 5.6 Running

```bash
cd backend && python run.py        # → localhost:8000/docs
cd frontend && npm run dev         # → localhost:3000
```

**On Windows use `python run.py`, not bare `uvicorn`.** uvicorn hardcodes
`ProactorEventLoop` on Windows, and psycopg's async mode cannot use it — every
database call fails and the app silently falls back to in-memory stores.
`run.py` pins `SelectorEventLoop`. Linux and Docker are unaffected.

### 5.7 Optional: the local model

```bash
ollama pull llama3.1:8b
```

`LLM_PROVIDER=auto` falls back to it when the daily Groq budget is spent;
`local` stays off the cloud entirely.

### 5.8 Deployment

| Part | Platform | Free tier |
|---|---|---|
| API | Render (Docker, from the committed `render.yaml`) | 512 MB, no card, sleeps after 15 min |
| Web | Vercel | Hobby |
| Database | Supabase | 500 MB, pauses after 7 days idle |

Railway was considered and rejected: since its pricing change it requires a
minimum of $1/month after a 30-day trial, caps a project at 0.5 GB RAM, and
allows one project — which will not hold an API and a database together.

`.github/workflows/keep-warm.yml` pings `/health/ready` every 10 minutes,
which addresses both free-tier timers, since that endpoint touches the
database. Render grants 750 instance-hours a month and staying awake costs
about 720, so this fits — for one service.

Full step-by-step in the [README](../README.md#deployment).

---

## 6. Evaluation results

### 6.1 Automated

**248 tests, all passing.** They need no network, no database and no API keys —
a suite that needs credentials is a suite that stops being run. They cover the
gear-selection rules, tool-argument construction and caching, memory
consolidation bands, the CJK extraction floor, RAG redirect handling, region
fallback, geocoding distance and match-type rejection, concurrent step
attribution, key rotation, usage metering and the tool-call budget.

```
$ PYTHONIOENCODING=utf-8 pytest -q
248 passed
```

Ruff (`ANN`, `D`, `BLE`, `S` enabled) reports no findings. The frontend
type-checks and builds clean.

### 6.2 Verified against live services

Confirmed end-to-end against live Supabase, Groq, Gemini, Tavily, Geoapify and
Wikivoyage:

| Claim | Evidence |
|---|---|
| Three-gear conversation | "i want to go to kerala" → `mode=advise`, 2,780 tokens, four grounded options, two questions. The follow-up supplying duration/window/origin → `mode=plan`, with a flight step from Delhi |
| Dynamic tool selection | Service toggles verified to remove tools from the model's toolbox, 8 → 6 |
| Multi-hop RAG | Hop trace recorded per run and rendered in the admin dashboard, showing each hop's query and what it was derived from |
| Cross-session memory | Preference stated in one session applied in a new session about a different city, without repetition |
| Multilingual | Japanese input → Japanese reply; memory stored in English and retrievable across languages |
| Parallel steps | Jaipur plan: 5 steps in 4 waves, district research overlapping the weather forecast |
| Map pin accuracy | Two-day Jaipur plan: 10 of 12 stops pinned, zero implausible coordinates |
| Photographs | 6 of 6 stops illustrated — four real Wikipedia photographs, two labelled representative |
| Local fallback | `resolve_provider` routed correctly; a real reply returned in 10.4 s on `llama3.2:3b`, recorded as `providers=['local']` |
| RLS | An authenticated user cannot escalate to admin and sees only their own rows |

### 6.3 Measured performance

| Operation | Time |
|---|---|
| Clarifying turn | 2–4 s |
| Advisory turn | 10–20 s |
| Full plan | 1–3 minutes |
| Cold start (free tier) | +30–60 s |

A full plan is roughly fifty model calls and fifty tool calls. Each is fast;
the total is not. That is why `/chat/stream` exists — the agent was never slow
so much as **silent**, and several minutes of blank screen is
indistinguishable from a crash, so people reloaded and discarded the work in
flight.

### 6.4 Cost

One full itinerary costs 30–50k tokens. Groq's free tier allows 100,000 tokens
per day **per model**, so a single key is worth two or three complete plans a
day. `GROQ_API_KEY` accepts a comma-separated list; keys from separate
accounts each carry their own allowance.

---

## 7. Limitations

Stated plainly, because a submission that hides these is worse than one that
does not.

1. **Flight and hotel data is simulated.** Amadeus decommissioned its
   Self-Service API on 17 July 2026. The mock is grounded in real
   great-circle distances and a defensible pricing curve, and every response
   is labelled `"source": "mock"` and badged **Simulated** in the interface —
   because a generated price rendered like a measured forecast is the most
   misleading thing a travel app can display. A `DuffelFlightProvider` exists
   behind the same interface if a key is supplied. The badge is derived from
   the payload's own `source`, so swapping in a real provider flips it and
   changes nothing else.

2. **The daily token budget is the binding constraint, and it is invisible.**
   Groq reports the daily figure in no header — `x-ratelimit-*` describes only
   the per-minute window, so a key can read "11,959 tokens remaining" while
   being entirely out of daily budget. An earlier diagnosis in this project
   was wrong for exactly that reason: the headers were believed over the
   behaviour. Mitigations: key pooling, per-model fallback chains, a local
   model, and a quota meter in the composer — which is itself an *estimate*
   from metered spend, labelled as such until a real 429 quotes ground truth.

3. **The local model is a quota fix, not a speed fix**, and cannot be
   deployed. Ollama plus a model needs gigabytes of RAM; Render's free
   instance has 512 MB. On the hosted site the switch disables itself and says
   why. Measured on a 4 GB laptop GPU: `llama3.2:3b` runs at 47.8 tok/s but
   cannot fill the larger schemas — asked to extract "how about manali" it
   returned `destination=None` and put "Manali" in the clarifying-question
   field. `llama3.1:8b` extracts correctly and takes 28 s because it spills
   out of VRAM. `qwen3:4b` emits no tool calls at all and is unusable as an
   executor.

4. **Geocoders answer confidently when they should not**, and this took three
   separate fixes. Open-Meteo's geocoder ranks by relevance, so `count=1` for
   "Bali" returns a village in West Bengal. Geoapify, asked for "Jaigarh Fort,
   Jaipur", returned a point in Maharashtra 1,100 km away that rendered
   identically to nine correct pins. Asked for "Catskill Mountains, New York"
   it cannot find the Catskills, matches the part it recognises and returns
   *Manhattan* — which a distance check cannot catch, because the wrong answer
   is zero km from where you were looking. The defences are a proximity bias
   with a 300 km rejection radius, and reading `match_type` and confidence to
   discard qualifier-only matches. The result is **fewer pins, no wrong ones**.

5. **Wikivoyage coverage is uneven.** Major cities have rich district
   articles; smaller destinations may have a single page, in which case hop 2
   is skipped and `stopped_because` records why. Region-level requests fall
   back to parsing the article's own Cities listing.

6. **The grounding check is a heuristic.** It flags named venues and prices
   absent from the retrieved evidence and logs them for the admin dashboard.
   It does not block the response and it has false positives.

7. **First request after idle is slow** on the free tier — cold start plus a
   cold RAG cache for a city nobody has asked about yet.

8. **Anonymous sessions live in one browser.** Clearing site data or moving to
   another device produces a new identity with no memory. Signing in at
   `/login` is the durable path; it is optional by design, and the trade is
   deliberate.

---

## 8. What I would do next

- Replace the mock providers with Duffel's sandbox for genuinely live
  availability, keeping the `simulated` badge wired to the payload.
- Evaluate itinerary quality systematically rather than by inspection — a
  rubric-scored set of held-out requests, scored by a model and spot-checked.
- Move the grounding check from heuristic to entailment, so an unsupported
  venue name is caught rather than flagged.
- Cache embeddings for repeated destinations across users, which the schema
  already permits.
