# CLAUDE.md — working notes for this repository

Persistent context for future sessions. Read this before changing anything.

## What this is

An AI travel-planning agent, built as a take-home technical assessment for
**Hipster Pte. Ltd. (Singapore)**. Real engineers review the code, the docs and
the deployed API, and the author must be able to defend every decision in a
follow-up interview.

**That framing drives two rules:**

1. **No cleverness without a reason that can be said out loud.** If a decision
   cannot be explained in one sentence, it is the wrong decision here.
2. **Explain *why*, not *what*.** Comments and docstrings throughout say why a
   thing is done that way, especially where the obvious approach was rejected.
   That is what an interviewer probes.

Graded areas, in the client's own weighting: **Agent Logic**, **LLM + RAG
Integration**, **Memory Use**, then Code Quality and Deployment. Depth in the
first three beats polish elsewhere.

## Stack, and why

| Choice | Why | Rejected alternative |
|---|---|---|
| LangGraph `StateGraph`, hand-built | LangChain 1.x removed the prebuilt Plan-and-Execute agent the brief names | `create_agent` alone — it is a ReAct loop, it does not plan |
| `create_agent` **inside** the executor node | The inner tool-calling loop is a solved problem; reimplementing it buys nothing | Hand-rolled tool dispatch |
| Groq, three model tiers | The cap is 100k **tokens**/day *per model*; tiering spreads one turn's 6–12 calls across three buckets | One model for everything |
| Gemini `gemini-embedding-001` @ 768d | Groq has no embeddings; local torch will not fit in 512 MB | `sentence-transformers` |
| Supabase Postgres + pgvector | One DB for auth, chats, memories, traces; RLS enforces isolation | Pinecone/Chroma — splits users from memories, breaks the admin joins |
| Wikivoyage as RAG corpus | Districts are addressable subpages → real multi-hop | Wikipedia dump — no district structure, too large for 500 MB |
| Open-Meteo | No key, no card | OpenWeatherMap One Call — requires a card |
| Geoapify | Documented free tier; `conditions` filters vegetarian/wheelchair/dogs | OpenTripMap — could not confirm signup is still open |
| Mock flight provider | **Amadeus Self-Service was decommissioned 2026-07-17** | Duffel sandbox — real API, fictional airline data |
| Ollama `llama3.2:3b` as local fallback | A spent *daily* budget is the one failure a second key cannot fix | `llama3.1:8b` (6x slower, spills 4 GB VRAM), `qwen3:4b` (emits no tool calls at all) |
| SSE for progress | One-way, short-lived, survives buffering proxies, no state on a host that sleeps | WebSockets |

Versions verified at build time: LangChain 1.3.14, LangGraph 1.2.10,
FastAPI 0.140, Python 3.13 local / 3.12 in Docker.

## Commands

```bash
cd backend
python -m venv .venv && ./.venv/Scripts/python.exe -m pip install -e ".[dev]"

# Windows note: set this or non-Latin test output raises UnicodeEncodeError
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/ -q

./.venv/Scripts/python.exe -m ruff check app tests --fix
./.venv/Scripts/python.exe -m ruff format app tests
./.venv/Scripts/python.exe run.py    # → localhost:8000/docs  (NOT bare uvicorn on Windows)

# Local models (optional). LLM_PROVIDER=auto falls back to these when the
# daily Groq budget is spent; =local stays off the cloud entirely.
ollama pull llama3.2:3b
```

Tests need no network, no database and no API keys. Keep it that way — a
suite that needs credentials is a suite that stops being run.

## Layout

```
backend/app/
  core/       config, logging, errors, security   (imports nothing from app)
  services/   llm, embeddings, http, geocoding, images, progress
                                                   (one module per external system)
  db/         session (RLS scoping), repositories (ALL SQL lives here)
  providers/  flights                             (swappable implementations)
  tools/      8 tools + registry                  (the model's action surface)
  memory/     short_term + extractor/consolidator/store/service
  rag/        corpus, chunking, index, retriever
  agent/      state, trip_state, graph, runner, nodes/
  api/        deps + v1/routes/
supabase/migrations/   0001–0009, applied in order
```

**The three-gear conversation** (`agent/trip_state.py` owns the rules):
`clarify` (no destination → one friendly question) / `advise` (destination but
no trip yet → one-hop grounded options + ≤2 questions, ~3k tokens) / `plan`
(the full pipeline — runs when duration + a date signal are known, on "just
plan it", or for scoped requests like "flights to Tokyo"). The gear decision
is pure code over model-extracted slots (`decide_mode`), never model judgement.
Slots persist per conversation in `sessions.trip_state` (JSONB); absence never
erases. The composer's Flights/Attractions/Stays/Restaurants toggles travel as
`focus` on every chat request — flights/stays are enforced by removing the
tool, attractions/restaurants by prompt (they share `find_places`).

**The rule that keeps this honest:** `agent/` imports `tools/`; `tools/` never
imports `agent/`. A tool that knows about the planner cannot be tested alone.

## Things that will bite you

- **`DATABASE_URL` must use the session pooler (port 5432), not the
  transaction pooler (6543).** The LangGraph checkpointer needs prepared
  statements, which the transaction pooler does not support.
- **Wikimedia returns 403** for a generic User-Agent. `HTTP_USER_AGENT` must
  name the client and a real contact. Verified against the live API.
- **`gemini-embedding-001` does not normalise** below 3072 dimensions. We
  truncate to 768, so `_normalise()` in `services/embeddings.py` is required.
  Cosine hides this; inner-product distance would not.
- **The extraction gate has two length floors** — 12 chars for Latin scripts,
  4 for CJK/Arabic. A single floor silently disabled memory for CJK users;
  `私はベジタリアンです` ("I am vegetarian") is 10 characters. There is a
  regression test.
- **`memory_conflict_threshold` must stay below `memory_dedupe_threshold`.**
  A validator enforces it; inverting them makes the arbitration band vanish.
- **Free tiers:** Render sleeps after 15 min, Supabase pauses after 7 days.
  `.github/workflows/keep-warm.yml` pings `/health/ready` every 10 min, which
  fixes both because that endpoint touches the database.
- **Groq's real ceiling is tokens per DAY, and no header reports it.**
  Measured against the live API: `llama-3.3-70b-versatile` allows **100,000
  tokens/day per account**, and the 429 body says so ("on tokens per day
  (TPD): Limit 100000, Used 96696"). The `x-ratelimit-*` headers only expose
  the *per-minute* window (12,000 TPM) and the request count - so a key can
  read "11,959 tokens remaining" while being completely out of daily budget.
  An earlier diagnosis was wrong for exactly this reason. Per-minute limits
  differ per model: planner `gpt-oss-120b` 8,000 TPM, executor
  `llama-3.3-70b-versatile` 12,000, utility `llama-3.1-8b-instant` 6,000.
  Keys from **separate accounts** each get their own 100,000/day. The budget
  is also **per model**, so `models_for_role` steps down a fallback chain when
  one model's bucket is spent - the executor is the heaviest consumer and sat
  on one of the smallest allowances. The reset is a rolling 24-hour window,
  not a calendar day: nothing frees up at midnight.
- **Tool docstrings are billed on every executor call.** They are serialised
  into the schema and re-sent each iteration of the ReAct loop. Prose
  descriptions came to 3,758 tokens *per call*, ~37,600 per plan - a third of
  a day's budget in tool descriptions alone. Keep docstrings terse and put
  the reasoning in comments, which cost nothing. `get_tools_for_step` narrows
  the toolbox by step kind on top of that; together they cut schema cost 68%.
- **`create_agent` ignores callbacks bound to the model.**
  `model.with_config(callbacks=...)` looks right and silently does nothing,
  because `create_agent` builds its own graph around the model. Pass them in
  the *invocation* config - `agent.ainvoke(..., {"callbacks": [...]})` - or
  the executor's token usage reports as zero.
- **Windows dev needs `python run.py`, not bare `uvicorn`.** uvicorn hardcodes
  `ProactorEventLoop` on Windows via a loop factory, and psycopg's async mode
  cannot use it - every DB call fails and the app silently falls back to
  in-memory stores. `run.py` pins `SelectorEventLoop`. Linux/Docker unaffected.
- **Wikivoyage redirects break title-keyed retrieval.** `list_districts`
  offers titles that redirect: `Mumbai/Colaba and Fort` → `Mumbai/South`, and
  several offered titles can collapse onto one article. Always index *and*
  search by `article.title` after fetching, never by the requested title —
  getting this wrong made hops 2–4 return zero chunks and silently reduced
  multi-hop RAG to a single hop. Regression tests in `test_rag_redirects.py`.
- **`create_agent` bypasses `call_model`, so it bypasses metering.** The
  executor hands a model object to LangChain's ReAct loop, which calls it
  directly. Usage is captured with a callback attached in `get_model`; without
  it, the largest token consumer in the system is invisible and rate limits
  look inexplicable. It also holds one key for its lifetime, so its burst
  lands on a single key's 12,000 TPM rather than spreading.
- **Open-Meteo's geocoder ranks by relevance, not size.** `count=1` for
  "Bali" returns a village in West Bengal ahead of the Indonesian island.
  `services/geocoding.py` asks for 10 and picks the most populous.
- **Two geocoders, and they are not interchangeable.** Open-Meteo indexes
  *populated places* - great for "Kyoto", and it returns nothing at all for
  "Shaniwar Wada". Geoapify indexes points of interest. Itinerary pins use
  `geocode_landmark` (Geoapify); weather and the mock provider use
  `geocode_place` (Open-Meteo). Measured: Open-Meteo resolved 0 of 11 stops
  on a real Pune plan, Geoapify resolved all of them.
- **Itinerary coordinates are backfilled, not hoped for.** Items only carry
  coordinates when they came from `find_places`, and a plan built from guide
  research and web search produces none - so the map was always empty. The
  responder geocodes the composed itinerary before returning it.
- **Naming the city in a geocoder query is a hint, not a constraint.** Both
  geocoders here would rather return a real coordinate for the wrong place
  than admit a miss - the same failure as the Bali bullet above, one layer
  down. "Jaigarh Fort, Jaipur" resolved to Maharashtra, 1,100 km away, and
  rendered identically to the nine correct pins beside it. So the responder
  resolves the destination once and hands `geocode_landmark` a `centre`,
  which biases the search *and* rejects anything past `MAX_PIN_DISTANCE_KM`
  (300 km - wide enough for any day trip, since the errors being caught are
  an order of magnitude worse). Two traps: Geoapify's `bias` takes
  `lon,lat`, the reverse of everywhere else in this codebase and silent when
  swapped; and no centre must mean no filtering, because an empty map is a
  worse outcome than an optimistic one.
- **`recursion_limit` does not limit tool calls.** It counts LangGraph
  super-steps, and every tool call in one assistant message shares a
  super-step - so a model calling tools in parallel can make any number of
  them within the limit. Observed: 43 `search_accommodation` calls in a single
  step, 194s against a 90s timeout, 156,870 tokens for one reply. The cap is
  enforced in `_budgeted_tools` instead, which also memoises identical calls -
  worth more than the cap, since most of the 43 were the same query repeated.
  Related: the step timeout was being applied *per attempt*, so every key
  rotation restarted the clock; the deadline is now computed once per step.
- **A geocoder's `match_type` is the only thing that catches its worst
  answer.** Asked for "Catskill Mountains, New York", Geoapify cannot find the
  Catskills, matches the part it recognises, and returns Manhattan with
  `match_by_city_or_disrict` and confidence 0.25. `MAX_PIN_DISTANCE_KM` cannot
  see this - the wrong answer is *zero km* from the point being searched. The
  proximity bias added for the Jaigarh Fort bug made it worse, not better.
  Reject `_QUALIFIER_ONLY_MATCHES` and anything under `MIN_MATCH_CONFIDENCE`.
- **`geocode_place` must not be used for a map centre.** Open-Meteo indexes
  populated places, so the Indian state of Kerala is either absent or carries
  no population - and "most populous wins" cannot break a tie when every
  candidate reports nothing. It answers "Kerala" with **Kerälä, Finland**. The
  centre is what every landmark is then measured against, so that one lookup
  did not misplace a pin, it rejected all four correct ones as 7,000 km
  outliers. Use `geocode_centre` (Geoapify, filtered to `_CENTRE_RESULT_TYPES`).
- **`needs_clarification` must not outrank the slot ledger.** The model set it
  after "trip to usa" and asked "Which city or country did you have in mind?" -
  a question already answered in the first sentence. The prompt forbids this
  and the model did it anyway, which is the argument for gear selection being
  code. `decide_mode` now clarifies only when there is genuinely no
  destination; the one truly blocked case (flights with no origin) is checked
  against the ledger.
- **Concurrent steps break trace attribution unless each gets its own
  recorder.** Tool calls were filed under a step by slicing the shared list
  between a before and after length, which mis-attributes the moment two steps
  interleave. `start_step_recording` rebinds the recorder inside each task and
  the executor merges them back in *plan* order (not completion order). It
  deliberately does **not** reset `_result_cache` - that dict is shared by
  reference, and concurrent steps about one destination are the likeliest to
  want the same guide article.
- **`compose` must never join a parallel batch**, and this is enforced in code
  rather than trusted to the planner's flag. A compose step marked independent
  writes the answer from findings that have not arrived - a plausible-looking
  itinerary rather than an obvious failure.
- **Pydantic `max_length` on a structured-output field bills you for the
  violation.** Groq validates tool arguments against the schema and rejects
  the whole call, so a 322-character `reasoning` string burned an entire
  planner call (1,103 in, 886 out) before the retry. Constrain a field only
  where the constraint earns more than a wasted call costs.
- **Two components could be imported as "the place image", and only one
  fetched a photograph.** `ui.tsx` served a random picsum shot seeded on the
  name, so a card labelled Lucknow showed a cactus. Naming was the whole bug:
  `PlacePhoto` is the real one, `DecorativeArt` is filler. If it says photo,
  it is one.
- **Accommodation must skip the Wikipedia tier.** Hotels have no article, but
  the search still *matches* on the city half of the name - "Lucknow 4-star
  hotel" returned a photograph of Hazratganj Market, which would then be
  captioned as that hotel and marked genuine. Also map `ItemKind` to
  photographic words (`_PHOTO_KEYWORD`): a library indexes "hotel", not "stay".
- **`simulated` is derived from the payload's own `source`, never a list.**
  Flight and hotel data is generated, and a made-up price rendered like a
  measured forecast is the worst thing this UI can do. Reading the payload
  means swapping in a real provider flips the badge and nothing else.
- **Anything read from a ContextVar must be read before its `stop_*` call.**
  `llm_providers` was empty on every reply for days because
  `active_providers()` ran after `stop_metering()`. `rag_hops` already carried
  this warning; the fix is to read from the object the runner holds
  (`meter.provider_list()`) rather than the binding.
- **Progress must be free when nobody is listening.** `services/progress.py`
  is a no-op with no channel open, and drops on a full queue rather than
  blocking. The plain `/chat` endpoint opens no channel, so every emit on that
  path has to cost nothing, and a stalled reader must never stall the agent.
- **Cast arguments to Postgres functions explicitly** (`%s::uuid`, `%s::int`,
  `%s::real`). psycopg infers `smallint` from a small int and `double
  precision` from a float, neither of which matches the `integer`/`real`
  parameters the migrations declare - overload resolution then fails with a
  bare "function does not exist" that reads like a missing migration.

## Conventions

- Type hints and Google-style docstrings on everything. Ruff enforces `ANN`
  and `D`; `BLE` and `S` are on, so every broad `except` and every SQL
  f-string must justify itself in a comment.
- Config only via `core/config.py`. Nothing else reads `os.environ`.
- Tools return `ToolResult`, never raise. A degraded result's `message` is
  written **for the model** and must say what to do instead *and* whether to
  retry — without that, models loop on the failing call.
- Every loop has an explicit budget. Runaway retrieve/replan is the known
  failure mode of agentic systems.
- User-scoped DB work → `db.user_scope(user_id)`. System work →
  `db.service_scope(reason=...)`, which is greppable and logged.
- Commit messages: explain the reasoning, plain prose, no bullet-point
  changelogs, no AI-tell phrasing.

## Frontend

```
frontend/src/
  app/        page (chat), dashboard, memories, admin/*, login, layout
  components/ AppShell (sidebar + history), ui, charts, icons, AuthGate
  lib/        api (typed client), supabase
```

**The chat is two columns.** Conversation left, `TripPanel` right - trip
summary, map, weather, stays, flights. A transcript is chronological, so
anything describing the *state* of the trip scrolled away the moment the
traveller replied. Facts accumulate across turns (asking about weather must
not blank the hotels) and every panel has three visible states: empty with a
line saying what to ask for, loading, or filled with a provenance badge. A
panel that renders nothing when it knows nothing is indistinguishable from a
broken one. Below `lg` it collapses into a drawer above the composer.

**One shell, every page.** `AppShell` renders the sidebar — new chat, nav,
conversation history, account actions — and every page supplies only its own
content. Per-page headers were why the layout appeared to jump between tabs.

**A conversation is a URL** (`/?session=<id>`), so history entries are
reopenable and linkable. The sessions API always worked; nothing had ever
called it.

Design tokens live in `globals.css`. Two coral values on purpose:
`--color-brand` is the vivid tone for gradients and large type,
`--color-brand-strong` is the darker one that clears 4.5:1 with white for
buttons and small text. Icons are inline SVG (`components/icons.tsx`) — emoji
appear only inside the agent's own message text, never as interface chrome.
Charts are hand-rolled SVG with table fallbacks; no charting dependency.
Destination photos are seeded placeholders, labelled as illustrative.

## Documentation map

- `README.md` — setup, running, example requests, live URL
- `docs/ARCHITECTURE.md` — the technical document: design and rationale
- `docs/WORKFLOW.md` — plain-language request walkthrough for non-engineers
- `docs/TESTING.md` — runbook: how to start everything and verify each claim
- `docs/AUDIT.md` — 30 Jul 2026 diagnostic pass: what was broken and why
- `scripts/` — verify_setup.py, apply_migrations.py, smoke_test.py
- Module docstrings carry the detailed reasoning; keep them current.

## Status

Backend and frontend complete, 238 tests passing. Verified end-to-end against
live Supabase, Groq, Gemini, Tavily, Geoapify and Wikivoyage: planning, dynamic
tool selection, multi-hop RAG, memory extraction and cross-session recall,
clarification, Japanese replies, and the admin trace all confirmed working.

The three-gear conversation is live-verified: "i want to go to kerala" ran an
advisory turn (mode=advise, 2,780 tokens, four grounded options, two
questions); the follow-up filling duration/window/origin ran the full
pipeline (mode=plan) with a flight step from Delhi in the plan.

Maps and photos are live-verified on a two-day Jaipur plan: 10 of 12 stops
pinned with zero implausible coordinates, and 6 of 6 stops illustrated (four
real Wikipedia photographs, two labelled representative). `GROQ_API_KEY` now
holds 7 keys; probing them individually is the only way to see which still
have daily budget, since the headers will not say.

Both are now shown in ordinary conversation as well, not only in a finished
plan - most conversations never reach one. Advisory turns geocode the options
they offer: Kerala resolves 4 of 4, New York 1 of 4 (Geoapify has no POI for
"Catskill Mountains", so those are dropped rather than guessed). Progress is
streamed over SSE and verified end to end; the local Ollama path is verified
too - `resolve_provider` routes correctly and a real reply came back in 10.4s
on `llama3.2:3b`, recorded as `providers=['local']`.

Docs are current as of this pass: README, ARCHITECTURE (new §11 on latency,
quota and pin honesty), WORKFLOW (timings corrected - a full plan is 1-3
minutes, not 10-20s), TESTING (§9 rewritten; it still carried the disproven
tokens-per-minute diagnosis) and `.env.example`.

Live Supabase project: `nlwzlplylmgawangqdhm` (ap-southeast-1), 9 migrations
applied, RLS verified (an authenticated user cannot escalate to admin and sees
only their own rows).

Remaining, in order: **deploy to Render + Vercel** (the only assignment
requirement not yet met - pick Render's Singapore region, next to the
database), push to GitHub, and rotate the API keys that were shared in chat.

Independent plan steps now run concurrently (`_batch_from` / `_run_batch`,
cap `agent_max_parallel_steps`=3). Live-verified on a Jaipur plan: 5 steps in
4 waves, district research overlapping the weather forecast. The planner had
to be *told* about `depends_on_previous` - it defaults True and the prompt
never mentioned it, so the batch was always one step until the prompt changed.
