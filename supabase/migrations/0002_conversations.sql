-- =============================================================================
-- 0002  Conversations: sessions and messages
-- =============================================================================
-- This is the *short-term* memory substrate: the transcript of one ongoing
-- conversation, scoped to a session.
--
-- A point worth being explicit about, because the brief calls it out: this
-- table is NOT the long-term memory system. A stored chat log is a
-- transcript, not memory - you cannot ask it "is this traveller vegetarian?"
-- without re-reading and re-reasoning over every message ever sent. Durable,
-- queryable user knowledge lives in `memories` (migration 0003), which is
-- derived from these rows but deliberately decoupled from them.
--
-- Two things read this table:
--   * the agent, for recent turns (short-term context);
--   * the admin portal, for full transcripts.
-- =============================================================================

do $$
begin
    if not exists (select 1 from pg_type where typname = 'message_role') then
        create type public.message_role as enum ('user', 'assistant', 'system');
    end if;
end
$$;


-- ---------------------------------------------------------------------------
-- Sessions
-- ---------------------------------------------------------------------------
-- `id` doubles as the LangGraph `thread_id`, so the checkpointer's saved graph
-- state and this row refer to the same conversation without a mapping table.

create table if not exists public.sessions (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references public.profiles (id) on delete cascade,

    -- Short human label, generated from the first user message so the admin
    -- portal and the frontend sidebar can list sessions meaningfully.
    title         text,

    -- Destination this session is about, when the agent has resolved one.
    -- Denormalised from the conversation because it is the single most useful
    -- filter in the admin portal ("show me every Tokyo session").
    destination   text,

    -- BCP-47 tag of the language the conversation is being held in.
    language      text,

    message_count integer not null default 0,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),

    -- Soft delete. A user "deleting" a session hides it from their view while
    -- preserving the audit trail; a hard delete is a separate, explicit
    -- privacy operation (see 0006).
    archived_at   timestamptz
);

comment on table public.sessions is
    'One conversation thread. The id is also the LangGraph checkpointer thread_id.';

create index if not exists sessions_user_recent_idx
    on public.sessions (user_id, updated_at desc)
    where archived_at is null;
create index if not exists sessions_destination_idx
    on public.sessions (destination)
    where destination is not null;


-- ---------------------------------------------------------------------------
-- Messages
-- ---------------------------------------------------------------------------

create table if not exists public.messages (
    id           uuid primary key default gen_random_uuid(),
    session_id   uuid not null references public.sessions (id) on delete cascade,

    -- Denormalised from `sessions` so that per-user row level security can be
    -- enforced on this table without a join. RLS policies that require a
    -- subquery are both slower and easier to get subtly wrong.
    user_id      uuid not null references public.profiles (id) on delete cascade,

    role         public.message_role not null,
    content      text not null,

    -- Language the message was written in, as detected for user messages or
    -- as chosen for assistant messages.
    language     text,

    token_count  integer,

    -- Whether the long-term memory extractor has already processed this
    -- message. Extraction runs in the background after the response is
    -- returned, so this flag is what makes that work idempotent and
    -- resumable after a crash or a redeploy mid-extraction.
    memory_processed_at timestamptz,

    metadata     jsonb not null default '{}'::jsonb,
    created_at   timestamptz not null default now()
);

comment on column public.messages.memory_processed_at is
    'Set when the memory extractor has consumed this message. Makes background extraction idempotent.';

create index if not exists messages_session_order_idx
    on public.messages (session_id, created_at);
create index if not exists messages_user_idx
    on public.messages (user_id, created_at desc);

-- Partial index over the extraction backlog only. The queue is small and
-- drains continuously, so indexing the whole table would be wasteful.
create index if not exists messages_pending_memory_idx
    on public.messages (created_at)
    where memory_processed_at is null and role = 'user';


-- ---------------------------------------------------------------------------
-- Session counters
-- ---------------------------------------------------------------------------
-- Maintained by trigger so `message_count` and `updated_at` cannot drift from
-- reality when a write happens outside the normal API path.

create or replace function public.bump_session_on_message()
returns trigger
language plpgsql
as $$
begin
    update public.sessions
       set message_count = message_count + 1,
           updated_at    = now()
     where id = new.session_id;
    return new;
end;
$$;

drop trigger if exists messages_bump_session on public.messages;
create trigger messages_bump_session
    after insert on public.messages
    for each row execute function public.bump_session_on_message();

drop trigger if exists sessions_touch_updated_at on public.sessions;
create trigger sessions_touch_updated_at
    before update on public.sessions
    for each row execute function public.touch_updated_at();
