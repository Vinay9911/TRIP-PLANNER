-- =============================================================================
-- 0004  Agent execution traces
-- =============================================================================
-- Every claim this project makes about its agent - "it plans", "it chooses
-- tools dynamically", "it replans when a step fails" - is unverifiable
-- without a record of what actually happened. These three tables are that
-- record, and they are what the admin portal's most useful panel renders.
--
--   agent_runs   one row per user message the agent responded to
--   agent_steps  the plan: one row per planned step, with its outcome
--   tool_calls   every tool invocation, with arguments, latency and errors
--
-- They also double as the operational telemetry: tool error rates, p95
-- latency per upstream dependency, and token spend per user all come from
-- here, so there is no separate metrics pipeline to run on a free tier.
-- =============================================================================

do $$
begin
    if not exists (select 1 from pg_type where typname = 'run_status') then
        create type public.run_status as enum (
            'running',
            'completed',
            'clarifying',    -- stopped early to ask the user a question
            'partial',       -- a guardrail tripped; answered with what we had
            'failed'
        );
    end if;

    if not exists (select 1 from pg_type where typname = 'step_status') then
        create type public.step_status as enum (
            'pending', 'running', 'completed', 'failed', 'skipped'
        );
    end if;
end
$$;


-- ---------------------------------------------------------------------------
-- Runs
-- ---------------------------------------------------------------------------

create table if not exists public.agent_runs (
    id                uuid primary key default gen_random_uuid(),
    session_id        uuid not null references public.sessions (id) on delete cascade,
    user_id           uuid not null references public.profiles (id) on delete cascade,
    request_message_id  uuid references public.messages (id) on delete set null,
    response_message_id uuid references public.messages (id) on delete set null,

    status            public.run_status not null default 'running',

    -- The plan as first produced by the planner node, kept verbatim. Storing
    -- the original alongside the executed steps is what makes replanning
    -- visible: you can see the plan change rather than only its final form.
    initial_plan      jsonb,
    replan_count      integer not null default 0,

    -- Which memories were injected into this run, and how many RAG hops were
    -- spent. Both are needed to explain an answer after the fact.
    injected_memory_ids uuid[] not null default '{}',
    rag_hops          integer not null default 0,

    detected_language text,

    prompt_tokens     integer not null default 0,
    completion_tokens integer not null default 0,
    latency_ms        integer,

    error_code        text,
    error_message     text,

    started_at        timestamptz not null default now(),
    finished_at       timestamptz
);

comment on table public.agent_runs is
    'One agent execution. The unit the admin trace viewer displays.';
comment on column public.agent_runs.initial_plan is
    'Planner output before any replanning, kept so plan evolution is inspectable.';

create index if not exists agent_runs_session_idx on public.agent_runs (session_id, started_at desc);
create index if not exists agent_runs_user_idx    on public.agent_runs (user_id, started_at desc);
create index if not exists agent_runs_status_idx  on public.agent_runs (status, started_at desc);


-- ---------------------------------------------------------------------------
-- Steps
-- ---------------------------------------------------------------------------

create table if not exists public.agent_steps (
    id           uuid primary key default gen_random_uuid(),
    run_id       uuid not null references public.agent_runs (id) on delete cascade,

    step_index   integer not null,
    description  text not null,
    status       public.step_status not null default 'pending',

    -- Which replan cycle introduced this step. Cycle 0 is the original plan;
    -- anything higher was added by the replanner in response to a result.
    replan_cycle integer not null default 0,

    result_summary text,
    error_message  text,
    latency_ms     integer,

    started_at   timestamptz,
    finished_at  timestamptz,
    created_at   timestamptz not null default now(),

    unique (run_id, replan_cycle, step_index)
);

create index if not exists agent_steps_run_idx on public.agent_steps (run_id, replan_cycle, step_index);


-- ---------------------------------------------------------------------------
-- Tool calls
-- ---------------------------------------------------------------------------

create table if not exists public.tool_calls (
    id            uuid primary key default gen_random_uuid(),
    run_id        uuid not null references public.agent_runs (id) on delete cascade,
    step_id       uuid references public.agent_steps (id) on delete cascade,

    tool_name     text not null,

    -- Arguments the model chose. This column is the direct evidence for
    -- "the model decides when and how to use tools" - it shows the same
    -- toolbox producing different calls for different questions.
    arguments     jsonb not null default '{}'::jsonb,

    -- Trimmed result. Full payloads can be large and occasionally contain
    -- third-party content we have no licence to retain, so only a summary is
    -- persisted; the complete response stays in the request-scoped log.
    result_summary text,

    succeeded     boolean not null default true,

    -- Set when the tool degraded instead of failing outright - an upstream
    -- outage that the agent routed around. Distinguishing this from a hard
    -- failure is what makes the dependency health panel meaningful.
    degraded      boolean not null default false,

    error_code    text,
    error_message text,
    latency_ms    integer,
    created_at    timestamptz not null default now()
);

comment on column public.tool_calls.degraded is
    'True when the tool returned a usable fallback after an upstream failure rather than erroring.';

create index if not exists tool_calls_run_idx  on public.tool_calls (run_id, created_at);
create index if not exists tool_calls_name_idx on public.tool_calls (tool_name, created_at desc);

-- Supports the dependency-health panel without scanning the whole table.
create index if not exists tool_calls_failures_idx
    on public.tool_calls (tool_name, created_at desc)
    where succeeded = false or degraded = true;
