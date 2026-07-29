# Architecture

The technical document for the Trip Planner Agent: how the system is put
together, and why each decision went the way it did.

Written to be argued with. Where the obvious approach was rejected, the
rejected option is named and the reason given.

---

## Contents

1. [System overview](#1-system-overview)
2. [Request flow](#2-request-flow)
3. [The planning loop](#3-the-planning-loop)
4. [Tools and dynamic selection](#4-tools-and-dynamic-selection)
5. [Memory](#5-memory)
6. [Multi-hop RAG](#6-multi-hop-rag)
7. [Data model and isolation](#7-data-model-and-isolation)
8. [Multilingual support](#8-multilingual-support)
9. [Failure handling](#9-failure-handling)
10. [Deployment](#10-deployment)
11. [Decision log](#11-decision-log)
12. [What I would do next](#12-what-i-would-do-next)

---

## 1. System overview

```mermaid
graph TB
    subgraph Client
        FE[Next.js chat + admin]
    end

    subgraph API["FastAPI"]
        AUTH[JWT verification<br/>local, via JWKS]
        ROUTES[Routes]
        RUN[Agent runner]
    end

    subgraph Agent["LangGraph StateGraph"]
        U[understand]
        P[plan]
        E[execute<br/>create_agent]
        R[replan]
        RESP[respond]
    end

    subgraph Capabilities
        TOOLS[8 tools]
        MEM[Memory service]
        RAG[Multi-hop retriever]
    end

    subgraph External
        GROQ[Groq LLM]
        GEM[Gemini embeddings]
        WV[Wikivoyage]
        GEO[Geoapify]
        OM[Open-Meteo]
        TAV[Tavily]
    end

    subgraph Storage["Supabase Postgres"]
        PG[(profiles, sessions,<br/>messages, memories,<br/>traces, RAG cache)]
    end

    FE --> AUTH --> ROUTES --> RUN --> U
    U --> P --> E --> R
    R -.loop.-> E
    R --> RESP
    U -.recall.-> MEM
    E --> TOOLS
    TOOLS --> RAG
    TOOLS --> GEO & OM & TAV
    RAG --> WV
    MEM --> GEM
    U & P & E & R & RESP --> GROQ
    MEM & RAG & RUN --> PG
```

**Layering.** Each layer imports only from those above it:

```
core/  →  services/  →  db/  →  providers/  →  tools/  →  memory/, rag/  →  agent/  →  api/
```

The rule that keeps it honest: **`agent/` may import `tools/`; `tools/` may
never import `agent/`.** A tool that knows about the planner cannot be tested
in isolation, and testability is the point of the boundary.

---

## 2. Request flow

```mermaid
sequenceDiagram
    participant U as User
    participant API as FastAPI
    participant R as Runner
    participant G as Graph
    participant M as Memory
    participant T as Tools
    participant DB as Postgres

    U->>API: POST /chat + Bearer token
    API->>API: verify JWT against cached JWKS
    API->>DB: create/resolve session (RLS-scoped)
    API->>R: run_turn(user_id, session_id, message)

    R->>DB: persist user message
    R->>DB: open trace row
    R->>R: bind tool context (identity)

    R->>G: invoke
    G->>M: recall relevant memories
    M->>DB: vector search + all constraints
    M-->>G: memory block

    G->>G: understand — language, dates, clarify?
    alt too vague
        G-->>R: clarifying question
    else clear
        G->>G: plan
        loop each step
            G->>T: execute (model picks tools)
            T-->>G: results
            G->>G: replan — continue / revise / finish
        end
        G->>G: respond, grounded, in user's language
    end

    G-->>R: final state
    R->>DB: persist reply + full trace
    R-->>API: TurnResult
    API-->>U: response + plan + tool calls

    Note over API,M: after the response is sent
    API->>M: extract memories (background)
    M->>DB: consolidate and store
```

Two orderings in that diagram are deliberate and load-bearing:

- **Memory recall happens before the clarification decision** (§5.3).
- **Memory extraction happens after the response is sent** (§5.4).

---

## 3. The planning loop

### 3.1 Why hand-built

The brief names *"LangChain's Plan-and-Execute"*. That class no longer exists:
LangChain 1.0 collapsed the old agent types into `create_agent`, and
`create_agent` is a **ReAct loop** — it picks a tool, sees the result, picks
another. It does not plan ahead.

So the planning loop is an explicit `StateGraph`:

```mermaid
graph LR
    S((start)) --> U[understand]
    U -->|needs clarification| RESP[respond]
    U -->|clear| P[plan]
    P --> E[execute]
    E -->|steps remain| R[replan]
    E -->|plan done| RESP
    R -->|continue / revise| E
    R -->|finish| RESP
    RESP --> EN((end))
```

### 3.2 `create_agent` inside the executor

The executor node **is** a `create_agent` instance. The inner tool-calling loop
— parse a tool call, run it, feed the result back, decide whether to call
another — is a solved problem with maintained edge-case handling. Rewriting it
would buy nothing.

So: the graph plans, `create_agent` executes. The honest answer to *"why not
just use `create_agent`?"* is that it **is** used, for the part it is good at.

**The executor sees one step, never the whole plan.** Given the full plan it
reliably runs ahead and attempts later steps, which defeats planning and makes
the replanner's view of progress wrong.

### 3.3 Node responsibilities

| Node | Model | Does |
|---|---|---|
| `understand` | planner | Language, dates, destination, constraints, clarify? |
| `plan` | planner | 2–5 ordered steps |
| `execute` | executor | One step, tools chosen by the model |
| `replan` | planner | continue / revise / finish |
| `respond` | executor | Grounded answer in the user's language |

### 3.4 Termination

Runaway retrieve-reason loops are the documented failure mode of agentic
systems, and the one that costs money. Four independent exits:

1. The plan runs out of steps.
2. The replanner truncates the plan (`finish`).
3. The replan budget is exhausted → status `partial`, answer with what exists.
4. LangGraph's `recursion_limit` as a backstop.

That limit is **derived** from the configured budgets, not hardcoded:

```python
def recursion_limit_for(settings):
    steps = settings.agent_max_plan_steps + settings.agent_max_replan_cycles
    return steps * 2 + 6      # execute+replan per step, plus fixed nodes
```

Raising `AGENT_MAX_PLAN_STEPS` in config would otherwise silently start
tripping LangGraph's guard instead of ours — surfacing as an opaque
`GraphRecursionError` rather than the intended partial answer.

### 3.5 Two cost optimisations worth defending

**The replanner skips its own model call when the last step succeeded and more
steps remain.** That answer is obvious, and asking costs one request per step
against a ~1,000/day budget.

**The replanner is biased toward `finish`.** Plans are written before any
evidence exists, so they over-specify; and a supervisor asked "could this be
better?" always says yes. Without the bias the agent grinds through steps
whose output nobody reads.

---

## 4. Tools and dynamic selection

### 4.1 The requirement

> *The model itself decides which tool(s) to call and when — never hardcoded
> keyword routing.*

Nothing in this system inspects a message for keywords. Tool choice comes
entirely from the schemas and descriptions in `app/tools/registry.py`.

That claim is **tested structurally**, since testing it with a live LLM would
be slow, costly and non-deterministic:

- No tool schema exposes `user_id`, `settings`, `session_id`, or a service
  object.
- A scan asserts the `if "weather" in message:` dispatch pattern appears
  nowhere in `app/`.
- Every tool has a description substantial enough to drive selection.

### 4.2 Why descriptions are prompt engineering

Tool descriptions say **when to prefer one tool over another**:

> Prefer this over `search_web` for anything about what a place is like…
> Use `search_web` instead for things that change: current prices, opening
> hours, events on particular dates.

That single sentence is what stops the agent burning Tavily credits on
questions Wikivoyage answers for free.

### 4.3 Constraints as filters, not hints

Geoapify's `conditions` parameter filters on `vegetarian`, `wheelchair`,
`dogs`. When memory says the traveller is vegetarian, the restaurant search is
**constrained at the source** rather than filtered afterwards by a model that
may forget. This is what turns the constraints bonus from a prompt instruction
into a query guarantee.

### 4.4 Why these tools

| Tool | Chosen | Rejected, and why |
|---|---|---|
| Weather | **Open-Meteo** — no key, no card | OpenWeatherMap One Call 3.0 now requires a card on file |
| Places | **Geoapify** — documented, `conditions` filters | OpenTripMap — could not confirm signup is still open; building on it was an avoidable risk |
| Web | **Tavily** — returns extracted content | Raw search engine — would need a scraping pipeline nobody asked for |
| Guides | **Wikivoyage** — free, structured, district subpages | Wikipedia dump — no district structure, far too large for 500 MB |
| Flights | **Mock behind a Protocol** | **Amadeus was decommissioned 2026-07-17**; Duffel's sandbox returns fictional-airline data |

### 4.5 On shipping a mock

The assignment explicitly permits *"mock flight/hotel API"*. Rather than random
numbers, the mock is grounded in things that are true:

- Real great-circle distance between real airport coordinates.
- Duration from distance at cruise speed, plus taxi and connection time.
- Price = fixed cost + per-km rate that **tapers** with distance (long-haul is
  cheaper per km), adjusted for cabin, advance purchase and hemisphere-aware
  seasonality.
- Deterministic: seeded from route and date via SHA-256, so demos reproduce
  and tests can assert exact values.

Sanity check — Singapore→Tokyo 5,299 km, 7.0 h non-stop, ~$541; London→Sydney
17,020 km, 22.9 h one stop, ~$1,328; booking one day out costs ~2.6× booking
six months out.

Every response carries `"source": "mock"` and a disclaimer the model is
instructed to repeat. **Fake data presented as real is the failure mode worth
avoiding**, not fake data as such.

---

## 5. Memory

The area the brief weights most heavily, and where most submissions are thin.

> *A raw conversation log saved to a database does not satisfy the long-term
> requirement.*

Agreed — and worth stating why. A transcript cannot answer "is this traveller
vegetarian?" without re-reading and re-reasoning over every message ever sent.
Memory has to be **retrievable structured knowledge**, not history.

### 5.1 Two systems

| | Short-term | Long-term |
|---|---|---|
| Scope | One session | All sessions, any city |
| Storage | LangGraph checkpointer (Postgres) | `memories` table + pgvector |
| Unit | Message | **One atomic fact** |
| Retrieval | Recency window | Semantic similarity × salience |
| Lifecycle | Trimmed, summarised | Deduplicated, reinforced, superseded, decayed |

### 5.2 The write path

```mermaid
graph TB
    A[User message] --> B{Heuristic gate}
    B -->|no signal| Z[Skip — no model call]
    B -->|possible fact| C[Extract<br/>8B model + strict schema]
    C --> D{Confidence ≥ 0.6?}
    D -->|no| Z2[Discard]
    D -->|yes| E[Embed]
    E --> F[Find neighbours<br/>same subject slot]
    F --> G{Cosine similarity}
    G -->|≥ 0.90| H[REINFORCE<br/>bump count, no new row]
    G -->|0.75–0.90| I[Arbitrate<br/>one small model call]
    G -->|< 0.75| J[INSERT]
    I -->|duplicate| H
    I -->|contradiction| K[INSERT + mark old superseded]
    I -->|compatible| J
```

**Layer 1 — a free heuristic gate.** Most turns contain nothing durable.
Running a model over every "thanks" would burn the daily quota producing empty
lists. Tuned to **over-admit**: a false positive costs one cheap call, a false
negative loses a fact permanently.

**Layer 2 — schema-constrained extraction.** Closed vocabularies for
`memory_type` and `subject`. A schema is enforced; a prompt is a suggestion.

**Layer 3 — an explicit rejection policy.** Without written negative rules,
models reliably store *"user is going to Tokyo in March"* (worthless next time)
and *"user seems friendly"* (unfalsifiable). The governing test in the prompt:

> *If this person books a completely different trip, to a different country,
> in two years — would knowing this still help?*

Also never stored: names, contact details, payment information.

### 5.3 Consolidation — the part that matters

Without it, telling the agent you are vegetarian in five sessions produces five
near-identical rows, all competing for the same retrieval slots. This is the
"memory hoarding" problem, and the fix is the three-band scheme above.

Two details worth defending:

- **Comparison is scoped to the subject slot.** A dietary fact is only ever
  compared against other dietary facts, which keeps the arbitration call rare —
  it costs a model request, so the expensive path must stay narrow.
- **Contradictions supersede rather than delete.** The old row is marked and
  points at its replacement. Retrieval ignores it; the admin inspector can show
  how a profile evolved, which is the first thing you want when diagnosing an
  extractor that learned something wrong.

### 5.4 The read path, and one correctness decision

Ranking is `similarity × salience`, not raw cosine:

```sql
salience = confidence
         × min(1, 0.45 + 0.55 · ln(1+mentions)/ln 6)     -- saturating
         × (constraint/identity ? 1 : max(0.5, exp(-age/180d)))
```

Similarity alone is a poor signal for memory: *"I might try vegetarian food"*
and *"I am vegetarian"* embed almost identically but deserve very different
weight. Confidence and restatement count supply that difference.

**Constraints and identity facts do not decay.** Someone with a nut allergy
still has it eight months later; decaying that is a safety bug, not a
relevance trade-off.

**And the decision I would most want to be asked about:**

> Hard constraints are retrieved **by type, unconditionally**, and merged with
> the similarity results.

*"Plan two days in Kyoto"* has almost no lexical overlap with *"Traveller is
allergic to nuts"* — under pure similarity ranking the allergy loses to a note
about liking temples. That is a **correctness** problem, not a relevance one,
which is why it is not tunable by a threshold. It costs a few extra tokens per
turn and removes an entire class of failure.

### 5.5 Memory suppresses redundant questions

The brief asks the agent to clarify ambiguous input *and* to apply remembered
preferences without repetition. Built independently, those collide: the agent
asks *"what's your budget?"* of someone who answered three sessions ago —
exactly the behaviour memory exists to prevent.

So recall runs **before** the clarification decision, and known facts enter the
prompt marked as already answered. What remains is a question the agent
genuinely could not answer for itself.

### 5.6 Short-term memory

LangGraph checkpointer keyed by `thread_id` = session id, so a conversation
survives the process restarts that free-tier hosting causes routinely.

Context is bounded two ways. `trim_conversation` keeps a recent window and —
the subtle part — **drops tool results orphaned from their originating call**,
because a dangling `ToolMessage` is a hard 400 from the chat API, not a
degraded prompt. `summarise_conversation` compresses older turns, capturing
decisions made and suggestions **rejected**; a summary that omits "they turned
down the Shibuya hotel as too noisy" will see it suggested again two turns
later.

---

## 6. Multi-hop RAG

> *One search's result becomes the input to the next — not a single
> retrieve-and-stuff call.*

Easy to fake. Three searches in a row are not multi-hop if all three queries
were derivable from the original question. Here the dependency is real:

```mermaid
graph TB
    Q[Request + constraints] --> H1

    subgraph H1["HOP 1 — orient"]
        A1[Fetch city article] --> A2[List districts<br/>via MediaWiki API]
    end

    H1 -->|district names<br/>not knowable before| H2

    subgraph H2["HOP 2 — narrow"]
        B1[Model picks 2-4<br/>from the real list] --> B2[Fetch THOSE articles]
    end

    H2 -->|selected documents| H3

    subgraph H3["HOP 3 — constrain"]
        C1[One search per constraint,<br/>scoped to those districts]
    end

    H3 --> H4

    subgraph H4["HOP 4 — fill gaps"]
        D1{Sufficient?} -->|no| D2[One targeted search]
        D1 -->|yes| D3[Stop]
    end

    H4 --> ANS[Grounded passages<br/>with citations]
```

### 6.1 Why Wikivoyage makes this real

Three structural properties, all verified against the live API:

1. **Districts are addressable subpages** — `Tokyo/Shinjuku`,
   `Kyoto/Higashiyama`, `Paris/11th arrondissement`. So "which districts exist"
   is an API query, not an LLM guess. **This is the hinge.** Hop 2 retrieves
   documents whose names hop 1 discovered.
2. **Sections are consistent** — Understand / Get in / See / Do / Eat / Sleep.
   A free metadata filter: dietary questions search `Eat`, attractions `See`.
3. **Genuinely free** — no key, CC BY-SA.

Verified live: Kyoto has 6 district articles, Tokyo 59, Paris 38 after
deduplicating redirect variants.

### 6.2 Chunking follows structure, not a window

The tutorial default — slide a fixed character window — is wrong for this
corpus. A Wikivoyage `See` section is a list of individually-described
attractions; a fixed window cuts through the middle of them, so retrieval
returns half a temple and half a museum, and the planner cites an attraction
whose name was in the previous chunk.

Instead: split on sections; keep sections that fit whole; split oversized ones
on **numbered listing markers** (`1 Kiyomizu Temple`, `2 Yasaka Shrine`), which
never cut an entry in half. Every chunk keeps its section label, and the
provenance is prefixed into the embedded text — `Eat` and `Sleep` sections of
one district otherwise embed very close together, sharing place names and tone.

### 6.3 One search per constraint

Hop 3 issues a **separate** search per constraint rather than one combined
query. Embedding `"vegetarian AND wheelchair accessible"` produces a vector
near neither — the classic multi-constraint retrieval failure.

### 6.4 Stop conditions

Given equal weight to the hops: hop ceiling, no new documents, sufficiency
reached, district cap (4). A model naming a district that does not exist is
filtered against the real list, so a hallucination costs nothing.

### 6.5 Caching

Articles are fetched **on demand** and cached with a TTL. A full dump is tens
of gigabytes against a 500 MB database. Embedding one district costs 6–10
provider calls and a Tokyo itinerary touches three or four — without the cache,
the second person to ask about Tokyo pays the same as the first and the free
embedding quota is gone within a day.

---

## 7. Data model and isolation

### 7.1 Why Postgres + pgvector, not Pinecone

The brief suggested *"Pinecone, FAISS"*. Those are examples, not a fixed list —
and pgvector **is** a vector database, among the most widely deployed in 2026.
The deviation is deliberate:

| | pgvector | Separate vector DB |
|---|---|---|
| Admin portal ("this user, their chats, their memories") | one SQL join | glue code across two systems |
| Per-user isolation | **enforced by the database** | enforced by remembering to filter |
| Survives redeploy on free hosting | yes | yes (but Chroma's local file would not) |
| Backup / restore | one story | two |

FAISS was never viable here: it is a similarity-search *library*, with no
persistence, no metadata filtering and no notion of a user.

The `VectorStore` interface is real — `InMemoryMemoryStore` mirrors the SQL
ranking formula and is what the memory tests run against.

### 7.2 Isolation is enforced by the database

The backend connects to Postgres directly, which authenticates as `postgres` —
a role that can read every row. Used naively, the RLS policies would never run
and isolation would degrade to *"the application promises to always write
`where user_id = …`"*.

`user_scope` closes that, inside the transaction:

```sql
select set_config('request.jwt.claims', '{"sub":"<uuid>",...}', true);
set local role authenticated;
```

Postgres evaluates RLS against the **current role**, so after the second
statement the connection is subject to exactly the policies a browser client
would be. A query that forgets its `WHERE` clause returns the caller's rows and
nothing else. Both settings are transaction-local, so they cannot leak into the
next borrower of a pooled connection.

`service_scope(reason=...)` is the deliberate escape hatch — a separate,
greppable function name so an audit of privileged access is one search.

### 7.3 Schema

```mermaid
erDiagram
    profiles ||--o{ sessions : has
    profiles ||--o{ memories : has
    sessions ||--o{ messages : contains
    sessions ||--o{ agent_runs : produced
    agent_runs ||--o{ agent_steps : has
    agent_runs ||--o{ tool_calls : made
    messages ||--o| memories : "sourced (provenance)"
    memories ||--o| memories : supersedes
    documents ||--o{ document_chunks : "chunked into"
```

Two authorization details:

- **`app_role` lives in `profiles`, not in `auth.users` metadata**, which is
  writable by the user's own client — a role stored there would be
  self-assignable from the browser console. A trigger additionally blocks role
  changes from anything but the service role.
- **`is_admin()` is `SECURITY DEFINER`**, so a policy on `profiles` can check
  the caller's role without recursing into the policies on `profiles` — the
  most common way RLS setups break on Supabase.

### 7.4 Auth

JWTs are verified **locally** against Supabase's JWKS: no network round trip
per request, and the API keeps working through a brief auth-service outage.

Three properties, each guarding a mistake that is easy to ship:

- **Verify the signature.** A JWT payload is base64, not encryption. An
  unverified `sub` is attacker-controlled — and `sub` is what this system hands
  Postgres as the identity RLS trusts.
- **Pin the algorithms** to `ES256`/`RS256`. Reading `alg` from the token
  allows `alg: none`; permitting HS256 alongside an asymmetric key lets an
  attacker sign tokens using the *public* key as an HMAC secret.
- **Never trust a role claim.** Admin status is read from the database.

---

## 8. Multilingual support

The design decision that makes this more than a translation veneer:

> **Retrieval language is decoupled from response language.**

Retrieval runs in **English** against an English corpus; only the final
composition switches language. A Japanese-speaking traveller therefore gets the
full quality of the English guide corpus, rather than whatever happens to exist
in Japanese.

Supporting pieces:

- Language is detected in `understand` as part of an existing call — no extra
  request.
- Memories are stored in **canonical English** with `source_lang` recorded, so
  a preference stated in Japanese is retrievable in an English session.
  `gemini-embedding-001` is multilingual, which makes the cross-lingual
  retrieval work rather than merely the storage.
- Non-English replies are asked to keep place names in local script **with a
  romanised form in brackets** — a name only in local script cannot be typed
  into a map.

A bug found and fixed while testing this: the extraction heuristic had a flat
12-character minimum, but `私はベジタリアンです` ("I am vegetarian") is 10
characters. It was silently disabling long-term memory for CJK and Arabic users
— no error, just a profile that never filled up. There is now a second floor
for non-Latin scripts and a regression test.

---

## 9. Failure handling

### 9.1 A failing tool must not fail the run

If the weather API is down while planning Kyoto, the right outcome is an
itinerary without weather advice — not a 502. Every tool returns a
`ToolResult`; upstream failures become `status="degraded"`.

### 9.2 The degraded message is written for the model

The part that is easy to get wrong. `{"error": "timeout"}` tells a model
nothing about what to do next, and models faced with an unexplained failure
either retry forever or invent the missing data. So:

> *"Weather data could not be retrieved. Continue planning without
> weather-specific advice, tell the user forecasts were unavailable, and **do
> not call this tool again for this location in this conversation**."*

That final clause prevents the retry loop. A test asserts every degraded
message contains a do-not-repeat instruction.

### 9.3 Error taxonomy

Split by **required response**, not by where it was thrown:

| Error | Response |
|---|---|
| `ExternalServiceError` | degrade — let the agent route around the gap |
| `RateLimitError` | back off with jittered retry, then degrade |
| `ConfigurationError` | fail loudly at startup; retrying cannot help |
| `AuthenticationError` / `AuthorizationError` | reject — never degrade, that would be a security bug |
| `AgentBudgetExceededError` | stop and answer with what exists |

### 9.4 Nothing about telemetry can break a request

Trace persistence, memory extraction and `last_seen_at` updates all swallow
their exceptions. By the time they run the traveller has an answer; turning a
successful turn into an error to record it would be absurd.

---

## 10. Deployment

```mermaid
graph LR
    GH[GitHub] -->|push| CI[CI: lint, types, 97 tests, docker build]
    GH -->|blueprint| RENDER[Render — Docker, free]
    RENDER --> SUPA[(Supabase)]
    CRON[Keep-warm cron<br/>every 10 min] --> RENDER
    VERCEL[Vercel — Next.js] --> RENDER
```

Two free-tier constraints, handled rather than hidden:

- Render sleeps after **15 minutes** idle; 30–60 s cold start.
- Supabase pauses a project after **7 days** without database activity, and
  must then be restored by hand.

A scheduled ping to `/health/ready` every 10 minutes solves both, because that
endpoint touches the database while `/health` deliberately does not. Liveness
staying dependency-free matters: a probe that fails when the database blips
gets a healthy process restarted into a crash loop.

Production also tightens the agent budgets (5 steps, 2 replans, 60 s per step).
A shared-CPU free instance is likelier to hit the platform's request timeout
than to produce a better itinerary with a longer plan.

---

## 11. Decision log

| # | Decision | Rejected | Reason |
|---|---|---|---|
| 1 | Hand-built `StateGraph` | `create_agent` alone | It is ReAct; it does not plan |
| 2 | `create_agent` inside the executor | Hand-rolled dispatch | The inner loop is solved; reimplementing buys nothing |
| 3 | Three Groq model tiers | One model | ~1,000 req/day; one turn costs 6–12 |
| 4 | Gemini embeddings, 768d | Local `sentence-transformers` | torch will not fit in 512 MB; also multilingual |
| 5 | Supabase pgvector | Pinecone, Chroma, FAISS | Admin joins, DB-enforced isolation, one backup story |
| 6 | Wikivoyage corpus | Wikipedia dump | Districts are addressable → real multi-hop |
| 7 | Section-aware chunking | Fixed window | A window cuts attraction entries in half |
| 8 | Constraints retrieved unconditionally | Pure similarity | An allergy must not be ranked out — correctness, not relevance |
| 9 | Supersede, don't delete | Hard delete | The audit trail is how you debug a bad extractor |
| 10 | Mock flights behind a Protocol | Duffel sandbox | Amadeus is dead; Duffel's test data is fictional anyway |
| 11 | Open-Meteo | OpenWeatherMap | One Call 3.0 requires a card |
| 12 | Geoapify | OpenTripMap | Could not confirm signup is open; `conditions` filters are a real win |
| 13 | RLS via `set local role` | Trust application filters | The database refuses; code merely promises |
| 14 | Local JWKS verification | Call the auth server | No per-request round trip; survives auth outages |
| 15 | Background memory extraction | Inline | Costs ~2 s; benefit lands next session |

---

## 12. What I would do next

Honest about what is thin, in priority order.

1. **Automated evaluation.** A golden set of ~30 requests scored for
   constraint adherence, grounding and tool appropriateness. Today's evidence
   is unit tests plus manual inspection; that does not catch a regression in
   *answer quality*.
2. **Streaming responses.** A full turn takes 10–20 s and currently arrives at
   once. Token streaming would transform the perceived latency, and LangGraph
   supports it natively.
3. **Semantic caching.** Two users asking about Kyoto with similar preferences
   repeat most of the work. Caching on an embedding of (destination +
   constraints) would cut both latency and quota use.
4. **Parallel step execution.** `PlanStep.depends_on_previous` already exists
   but is unused — independent steps could run concurrently.
5. **Stronger grounding.** The current check is a heuristic that logs rather
   than blocks. A proper claim-level verifier gating the response would be
   better, at the cost of another model call.
6. **Real flight data**, if a budget for it existed.
