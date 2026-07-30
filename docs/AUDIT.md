# Agent audit — 30 July 2026

> **Correction, same day.** Section A below concluded that the four API keys
> were healthy and that key count was not the constraint. The first half of
> that is wrong. It was based on the `x-ratelimit-*` response headers, which
> report only the **per-minute** window — and Groq's binding limit on the free
> tier is **100,000 tokens per day per account**, which appears in no header
> at all. A key can read "11,959 tokens remaining" while being entirely out of
> daily budget, which is exactly what was happening. The corrected analysis is
> in [§A-revised](#a-revised--the-real-ceiling-is-tokens-per-day).

A diagnostic pass over the running system, prompted by two real transcripts
that went wrong and by four specific questions about whether features were
working at all.

Everything below is evidence-backed: measurements against the live database,
the live Groq and Open-Meteo APIs, and instrumented runs. Where a claim could
not be verified, that is stated rather than glossed.

---

## Summary

| # | Reported | Verdict | Root cause |
|---|---|---|---|
| A | "Should I add more Groq keys?" | **No** — keys were never the limit | Executor's tokens were invisible to metering; bursts land on one key |
| B | Chat history not saving | **Backend fine, UI missing** | 27 sessions were stored the whole time; nothing listed them |
| C | "What it remembers" empty | **Working as designed** | Gate correctly rejected every message sent; empty state explained nothing |
| D | Do the service toggles work? | **Yes, genuinely** | Verified: tools removed from the model's toolbox, 8 → 6 |
| F | Replies take 35–80s | **Confirmed, three causes** | Cold-start embeddings, sequential hops, and hops doing wasted work |
| — | *(found during audit)* | **Multi-hop RAG was silently single-hop** | Wikivoyage redirects broke hop 2/3/4 retrieval entirely |

The most serious finding was not on the list: **the headline multi-hop RAG
feature had been quietly degrading to a single hop** for any destination whose
sub-articles redirect, which is most large cities.

---

## A. Rate limits — adding keys would not have helped

**Measured, all four keys, live:**

```
key 1  HTTP 200  limit-requests: 1000  remaining: 998  limit-tokens: 12000
key 2  HTTP 200  limit-requests: 1000  remaining: 999  limit-tokens: 12000
key 3  HTTP 200  limit-requests: 1000  remaining: 999  limit-tokens: 12000
key 4  HTTP 200  limit-requests: 1000  remaining: 999  limit-tokens: 12000
```

Every key was healthy with its full daily allowance intact. Meanwhile the runs
that *failed* with rate-limit errors recorded only ~7,400 tokens — far below
even a single key's 12,000-per-minute ceiling, let alone four.

**The contradiction was the clue.** `create_agent` (the executor's inner
ReAct loop) is handed a model object and calls it directly, several times per
step, resending the accumulated message history and tool results each
iteration. Those calls never passed through `call_model`/`structured_call`,
which is where usage was recorded — so **the single largest consumer of tokens
in the system was not being counted at all**. The reported number was only the
planner, understand and responder calls.

Worse for rate limiting: a model object holds **one key for its lifetime**, so
that unmetered burst concentrates on one key's 12,000 TPM rather than
spreading across four.

**Fixed** by attaching a usage-recording callback in `get_model`, so executor
tokens are now counted and attributed to `"execute"`. Expect the reported
per-run figure to rise sharply — that is the fix working, not a regression.

**Recommendation:** don't buy more keys yet. Re-measure now that the numbers
are honest, and reduce the executor's burn first (the RAG fix below already
removes wasted hops).

---

## B. Chat history — the data was always there

```
vinaycollege1531@gmail.com   sessions=27  active=27  messages=70
```

Sessions, messages and `trip_state` were all persisting correctly, and
`GET /api/v1/sessions` worked. **There was simply no interface that ever
called it.** Every visit rendered a blank page and past trips were
unreachable.

**Fixed** by adding a persistent sidebar listing every conversation, and by
making a conversation a URL (`/?session=<id>`) so it can be reopened, linked
and reloaded.

---

## C. Memory — working correctly; the empty state was the bug

The `memories` table was genuinely empty, but extraction is not broken. The
heuristic gate, run against the exact messages sent:

```
gate=False  bali
gate=False  tell me about india
gate=False  Let's do Mumbai
gate=False  5 days, 8/08/2026
gate=False  Rugged Coastlines
gate=True   I'm vegetarian and I hate crowded places
gate=True   I always travel on a tight budget
```

And a live extraction call on a qualifying message returned:

```
CandidateMemory(memory_type=CONSTRAINT, subject=DIET,
                content='Traveller is vegetarian.')
```

The pipeline works. **None of the messages actually sent contained a fact that
would still be true on a different trip** — which is precisely what the gate
is for. Storing "wants to go to Bali" would fill the profile with places
someone glanced at once.

**Fixed** the real problem, which was that an empty page with no explanation
is indistinguishable from a broken one. The empty state now contrasts what
does and does not get stored, with examples.

---

## D. Service toggles — genuinely enforced

```
focus=None                              flights_tool=True   stays_tool=True   n=8
focus=['attractions','restaurants']     flights_tool=False  stays_tool=False  n=6
focus=['flights']                       flights_tool=True   stays_tool=False  n=7
```

Switching a service off removes its tool from the list the model receives —
absence, not instruction. The selection also persists in `trip_state`, so it
survives a page reload. Attractions and restaurants share `find_places` with
everything else, so those two are enforced by prompt instead; that asymmetry
is documented in the code.

---

## F. Latency — three separate causes

**Measured breakdown:**

| Operation | Time |
|---|---|
| `embed_query` (cold, first call) | 2.51s |
| `embed_query` (warmed) | 0.62s |
| 3 embeddings in parallel | 0.53s total |
| Wikivoyage `fetch_article` | 0.76s |
| Full retrieval, Mumbai | 15.7s |
| Slowest recorded `search_travel_guide` | 48.6s |

Three contributors, all now addressed:

1. **Cold-start embedding latency.** The first Gemini call in a process costs
   ~2.5s (TLS + connection setup) and drops to ~0.6s once warm. Unavoidable
   once per process, but it made every early measurement look worse than
   steady state.
2. **Sequential work that had no reason to be sequential.** Hop 2 fetched and
   indexed district articles one at a time, and hop 3 ran one embedding call
   per constraint in series. Since three parallel embedding calls finish in
   about the time one takes, both are now `asyncio.gather`ed.
3. **Hops doing work and returning nothing** — see below.

---

## The finding that wasn't reported: multi-hop RAG was single-hop

While timing retrieval, hops 2 and 4 were returning **zero chunks**:

```
BEFORE  Mumbai  10.99s  hops=3  TOTAL_CHUNKS=4
    hop1 orient     docs=1  chunks=4
    hop2 narrow     docs=3  chunks=0   <-- wasted
    hop4 fill_gaps  docs=3  chunks=0   <-- wasted
```

The search itself was fine (raw similarities of 0.70–0.76). The cause was
title bookkeeping. Verified against the live Wikivoyage API:

```
requested='Mumbai/Colaba and Fort'   -> article.title='Mumbai/South'
requested='Mumbai/Central Suburbs'   -> article.title='Mumbai/Eastern Suburbs'
requested='Mumbai/Elephanta Island'  -> article.title='Mumbai/Elephanta'
```

Hop 2 fetched each district article and indexed it under **its own** title,
but recorded and then searched the **requested** title. The filter therefore
named a document that had never been indexed, so it matched nothing — and
because hops 3 and 4 filter on the same recorded list, they returned nothing
either.

The effect: for any destination with redirects (most large cities), the
much-discussed multi-hop retrieval **collapsed to a single hop**, while still
paying for every hop's embedding round trip and model call.

```
AFTER   Mumbai  15.71s  hops=3  TOTAL_CHUNKS=13
    hop1 orient     docs=1  chunks=4
    hop2 narrow     docs=2  chunks=6
    hop4 fill_gaps  docs=2  chunks=3
```

**3× more grounded evidence** reaching the answer. Pinned by three regression
tests in `tests/unit/test_rag_redirects.py`.

---

## Other fixes made during the audit

- **Destination narrowing.** "Let's do Mumbai" after discussing India left the
  destination as *India* — the prompt told the model a settled destination
  should not change. Narrowing from a country to a city inside it is now
  explicitly a change. Verified live: `destination='Mumbai'`.
- **Raw article titles leaking into replies.** A reply read "the colonial
  buildings in Mumbai/Colaba and Fort". Sub-area titles are now rendered as
  their last path segment before reaching the model or the UI.

---

## What I could not verify

- **The full itinerary-quality improvements** (geographic coherence, holding
  to stated priorities) are prompt changes. Repeated attempts to confirm them
  end-to-end hit the same rate-limit ceiling as the original report, because
  a day of testing had exhausted the practical per-minute budget. They are
  correct by inspection and consistent with the surrounding tests, but they do
  not yet have a live transcript behind them.
- **Latency after the parallelism fixes** was measured at the retrieval layer
  (15.7s for a full Mumbai retrieval including cold start), not end-to-end
  across many runs. A fair before/after on total reply time needs a quieter
  quota window.

---

## Recommended next steps, in order

1. **Re-measure token usage** now that the executor is metered. The honest
   number should drive any decision about more keys.
2. **Cap the executor's context.** Tool results are capped, but the ReAct loop
   resends the whole history each iteration; lowering
   `AGENT_MAX_TOOL_CALLS_PER_STEP` is the cheapest lever.
3. **Persist the query-embedding cache.** It is currently a per-process LRU,
   so a restart pays cold-start costs again on the same destinations.
4. **Consider a smaller model for the executor.** It does the most calls with
   the largest prompts; the 70B model is not obviously required for
   "call this tool with these arguments".

---

## A-revised — the real ceiling is tokens per day

A four-message conversation ("India" → "Delhi then" → "Let's do Eastern
Delhi" → "places to eat") failed twice on rate limits. Runs recorded only
4,735 and 6,807 tokens. Every key reported full per-minute budget. Both facts
were true and neither explained the failure.

The 429 body did:

```
Rate limit reached for model `llama-3.3-70b-versatile`
in organization `org_01khg29mw3f81re84spb7qcmt7`
on tokens per day (TPD): Limit 100000, Used 96696, Requested 3390
```

**100,000 tokens per day, per account, and no header reports it.** Confirmed
independently on a second key, which showed `Used 99793` while its
`x-ratelimit-remaining-tokens` header still read 11,959.

The keys are from separate accounts (two distinct `org_` ids were observed),
so they do each carry their own daily allowance — that part of the original
setup advice was sound. The problem was how fast each allowance was being
spent.

### Where the day's budget was going

Tool descriptions. They are serialised into the schema and re-sent on **every
iteration** of the executor's ReAct loop:

| | chars | tokens/call |
|---|---|---|
| 8 tools, prose docstrings | 15,032 | 3,758 |
| after trimming | 9,948 | 2,487 |
| research step only (4 tools) | 5,196 | 1,299 |
| compose step (0 tools) | 0 | 0 |

Across a typical plan — two research steps, one logistics, one compose, about
three model calls each — that is **37,580 tokens of tool description per
itinerary**, better than a third of a day's budget before a single word of
travel content. Roughly ten plans per day across four accounts.

### Fixes

- **Docstrings are billed; comments are free.** The descriptions now say only
  what the model needs to choose a tool and fill its arguments. The reasoning
  a human wants moved into comments, which cost nothing at inference.
- **Each step gets only the tools its kind can use.** A research step has no
  business booking a flight; a compose step needs no tools at all.
- Together: **37,580 → 11,859 tokens per plan, 68% less.** Roughly ten plans
  a day becomes roughly thirty-three.
- **A daily exhaustion now parks the key for 30 minutes** instead of honouring
  the provider's suggested retry, which was a trickle and put the pool
  straight back onto a key with nothing left.
- **The failure message tells the truth.** "Try again in a minute" was wrong
  when the budget resets over hours; someone follows that advice, fails, and
  concludes the product is broken.

### And the metering fix from earlier did not work

The previous pass attached the usage callback with
`model.with_config(callbacks=[...])`. That looks correct and does nothing:
`create_agent` builds its own graph around the model, and config bound to the
model object never reaches the calls it makes. Measured afterwards, the meter
read **zero calls, zero tokens** for every executor step — so the numbers in
§A were only ever counting planner and responder work.

Moved into the invocation config, `agent.ainvoke(..., {"callbacks": [...]})`,
and verified firing: `calls=1 tokens=1331` on a single step.
