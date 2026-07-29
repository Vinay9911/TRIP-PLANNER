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
| Groq, three model tiers | ~1,000 requests/day free; one turn costs 6–12 calls | One model for everything |
| Gemini `gemini-embedding-001` @ 768d | Groq has no embeddings; local torch will not fit in 512 MB | `sentence-transformers` |
| Supabase Postgres + pgvector | One DB for auth, chats, memories, traces; RLS enforces isolation | Pinecone/Chroma — splits users from memories, breaks the admin joins |
| Wikivoyage as RAG corpus | Districts are addressable subpages → real multi-hop | Wikipedia dump — no district structure, too large for 500 MB |
| Open-Meteo | No key, no card | OpenWeatherMap One Call — requires a card |
| Geoapify | Documented free tier; `conditions` filters vegetarian/wheelchair/dogs | OpenTripMap — could not confirm signup is still open |
| Mock flight provider | **Amadeus Self-Service was decommissioned 2026-07-17** | Duffel sandbox — real API, fictional airline data |

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
```

Tests need no network, no database and no API keys. Keep it that way — a
suite that needs credentials is a suite that stops being run.

## Layout

```
backend/app/
  core/       config, logging, errors, security   (imports nothing from app)
  services/   llm, embeddings, http               (one module per external system)
  db/         session (RLS scoping), repositories (ALL SQL lives here)
  providers/  flights                             (swappable implementations)
  tools/      8 tools + registry                  (the model's action surface)
  memory/     short_term + extractor/consolidator/store/service
  rag/        corpus, chunking, index, retriever
  agent/      state, graph, runner, nodes/
  api/        deps + v1/routes/
supabase/migrations/   0001–0008, applied in order
```

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
- **Groq's real ceiling is TPM, not RPD.** Measured live: 12,000 tokens/min and
  1,000 requests/day for `llama-3.3-70b-versatile`. A full run is ~30-50k
  tokens, so runs fail on tokens long before requests. Add more keys to
  `GROQ_API_KEY` (comma-separated) - each one adds 12,000 TPM. Do not "fix"
  this by switching to the 8B model: it has *less* TPM (6,000).
- **Windows dev needs `python run.py`, not bare `uvicorn`.** uvicorn hardcodes
  `ProactorEventLoop` on Windows via a loop factory, and psycopg's async mode
  cannot use it - every DB call fails and the app silently falls back to
  in-memory stores. `run.py` pins `SelectorEventLoop`. Linux/Docker unaffected.
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

## Documentation map

- `README.md` — setup, running, example requests, live URL
- `ARCHITECTURE.md` — the technical document: design and rationale
- `WORKFLOW.md` — plain-language request walkthrough for non-engineers
- `docs/TESTING.md` — runbook: how to start everything and verify each claim
- `scripts/` — verify_setup.py, apply_migrations.py, smoke_test.py
- Module docstrings carry the detailed reasoning; keep them current.

## Status

Backend and frontend complete, 122 tests passing. Verified end-to-end against
live Supabase, Groq, Gemini, Tavily, Geoapify and Wikivoyage: planning, dynamic
tool selection, multi-hop RAG, memory extraction and cross-session recall,
clarification, Japanese replies, and the admin trace all confirmed working.

Live Supabase project: `nlwzlplylmgawangqdhm` (ap-southeast-1), 8 migrations
applied, RLS verified (an authenticated user cannot escalate to admin and sees
only their own rows).

Remaining: push to GitHub, deploy to Render + Vercel, and rotate the API keys
that were shared in chat.
