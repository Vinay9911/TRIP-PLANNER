-- =============================================================================
-- 0003  Long-term memory
-- =============================================================================
-- The core of the assignment's memory requirement: durable, structured facts
-- about a traveller, embedded and retrievable by meaning, decoupled from any
-- single conversation.
--
-- Each row is ONE atomic statement ("prefers vegetarian food"), not a
-- paragraph and not a message. Atomicity is what makes the rest of the design
-- work: you can only deduplicate, contradict, reinforce or delete a fact if
-- the fact is the unit of storage.
--
-- Three columns carry most of the design weight:
--
--   memory_type  Governs retrieval policy. `constraint` rows are injected
--                unconditionally rather than by similarity ranking - a
--                dietary or mobility requirement must never be dropped
--                because it scored 0.61 against the query. That is a
--                correctness decision, not a relevance one.
--
--   subject      A slot key ('diet', 'budget', 'pace', ...). Contradiction
--                detection is scoped to a slot, which is what lets
--                "budget: luxury" supersede "budget: shoestring" while
--                leaving "diet: vegetarian" untouched.
--
--   content      Stored canonically in ENGLISH regardless of the language the
--                user spoke, with the original language recorded in
--                `source_lang`. A preference stated in Japanese therefore
--                stays retrievable in an English session and vice versa.
-- =============================================================================

do $$
begin
    if not exists (select 1 from pg_type where typname = 'memory_type') then
        create type public.memory_type as enum (
            'preference',   -- soft: likes, dislikes, travel style
            'constraint',   -- hard: dietary, mobility, budget ceiling, pets
            'identity',     -- stable: home city, languages spoken
            'experience'    -- history: places visited, what they thought
        );
    end if;

    if not exists (select 1 from pg_type where typname = 'memory_status') then
        create type public.memory_status as enum (
            'active',       -- eligible for retrieval
            'superseded',   -- replaced by a newer, contradictory fact
            'deleted'       -- removed by the user or an admin
        );
    end if;
end
$$;


create table if not exists public.memories (
    id               uuid primary key default gen_random_uuid(),
    user_id          uuid not null references public.profiles (id) on delete cascade,

    memory_type      public.memory_type not null,
    subject          text not null,
    content          text not null,

    -- 768 dimensions: gemini-embedding-001 truncated via its Matryoshka
    -- property. Full 3072-dim vectors quadruple index size for a negligible
    -- recall gain at this corpus size, and pgvector's HNSW index has a
    -- 2000-dimension ceiling anyway.
    embedding        extensions.vector(768),

    -- ---- Confidence and reinforcement --------------------------------------
    -- `confidence` is the extractor's certainty that this is a durable fact.
    -- `mention_count` increments each time the same fact is independently
    -- restated. Together they drive salience: something said five times
    -- across three sessions outranks a single hedged aside.
    confidence       real not null default 0.7 check (confidence >= 0 and confidence <= 1),
    mention_count    integer not null default 1 check (mention_count >= 1),

    -- ---- Lifecycle ---------------------------------------------------------
    status           public.memory_status not null default 'active',

    -- When a newer fact contradicts this one, the old row is marked
    -- 'superseded' and points at its replacement rather than being deleted.
    -- Retrieval ignores it; the admin portal can still show how a traveller's
    -- profile evolved, which is far more useful for debugging the memory
    -- pipeline than a row that simply vanished.
    superseded_by    uuid references public.memories (id) on delete set null,

    -- ---- Provenance --------------------------------------------------------
    -- Every memory can be traced to the exact message that produced it. This
    -- is what makes the admin memory inspector trustworthy: a reviewer can
    -- always answer "why does the system believe this?".
    source_message_id uuid references public.messages (id) on delete set null,
    source_session_id uuid references public.sessions (id) on delete set null,
    source_lang       text,
    extractor_model   text,

    metadata         jsonb not null default '{}'::jsonb,

    first_seen_at    timestamptz not null default now(),
    last_seen_at     timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);

comment on table public.memories is
    'Atomic, embedded, user-scoped long-term facts. One statement per row.';
comment on column public.memories.subject is
    'Slot key scoping contradiction detection, e.g. diet / budget / pace / accommodation.';
comment on column public.memories.content is
    'Canonical English statement. Original language preserved in source_lang.';
comment on column public.memories.superseded_by is
    'Points at the memory that replaced this one. Retrieval skips superseded rows.';


-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- HNSW over cosine distance. HNSW rather than IVFFlat because IVFFlat needs
-- a representative sample of vectors present *before* the index is built to
-- choose its lists; this table starts empty and grows continuously, which is
-- precisely the case IVFFlat handles badly.
create index if not exists memories_embedding_idx
    on public.memories
    using hnsw (embedding extensions.vector_cosine_ops)
    with (m = 16, ef_construction = 64);

-- Retrieval is always scoped to one user and to active rows.
create index if not exists memories_user_active_idx
    on public.memories (user_id, memory_type)
    where status = 'active';

-- Supports the unconditional "fetch every hard constraint" read path.
create index if not exists memories_constraints_idx
    on public.memories (user_id)
    where status = 'active' and memory_type = 'constraint';

-- Supports slot-scoped contradiction lookup during consolidation.
create index if not exists memories_user_subject_idx
    on public.memories (user_id, subject)
    where status = 'active';

drop trigger if exists memories_touch_updated_at on public.memories;
create trigger memories_touch_updated_at
    before update on public.memories
    for each row execute function public.touch_updated_at();


-- ---------------------------------------------------------------------------
-- Salience
-- ---------------------------------------------------------------------------
-- Similarity alone is a poor ranking signal for memory. "I might try
-- vegetarian food sometime" and "I am vegetarian" embed almost identically,
-- but they deserve very different weight. Salience folds in how confident the
-- extractor was, how often the fact has been restated, and how long ago it
-- was last confirmed.
--
-- Decay is applied to `preference` and `experience` only. Constraints and
-- identity facts do not become less true with time: someone with a nut
-- allergy still has it eight months later, and decaying that would be a
-- safety bug rather than a relevance tradeoff.

create or replace function public.memory_salience(
    p_confidence    real,
    p_mention_count integer,
    p_last_seen_at  timestamptz,
    p_memory_type   public.memory_type
)
returns real
language sql
immutable
as $$
    select (
        p_confidence
        -- Reinforcement: saturating, so a fact repeated 20 times does not
        -- dominate one repeated 5 times. Reaches 1.0 at ~5 mentions.
        * least(1.0, 0.45 + 0.55 * ln(1 + p_mention_count) / ln(6))
        -- Recency: exponential decay with a ~180-day half-life, floored at
        -- 0.5 so an old preference is down-weighted but never erased.
        * case
            when p_memory_type in ('constraint', 'identity') then 1.0
            else greatest(
                0.5,
                exp(-extract(epoch from (now() - p_last_seen_at)) / (180 * 86400.0))
            )
          end
    )::real;
$$;

comment on function public.memory_salience is
    'Ranking weight from confidence, reinforcement and recency. Constraints and identity do not decay.';


-- ---------------------------------------------------------------------------
-- Similarity search
-- ---------------------------------------------------------------------------
-- Exposed as a database function rather than assembled as SQL in Python for
-- three reasons: the ORDER BY must be expressed exactly as pgvector wants it
-- for the HNSW index to be used; the ranking formula stays next to the data it
-- ranks; and it can be called over PostgREST as an RPC if the frontend ever
-- needs it.
--
-- SECURITY INVOKER (the default) is deliberate: the function runs with the
-- caller's privileges, so the row level security policies in migration 0007
-- still apply inside it. A SECURITY DEFINER function here would quietly
-- become a hole straight through per-user isolation.

create or replace function public.match_memories(
    p_user_id        uuid,
    p_query_embedding extensions.vector(768),
    p_match_count    integer default 8,
    p_min_similarity real    default 0.35,
    p_types          public.memory_type[] default null
)
returns table (
    id            uuid,
    memory_type   public.memory_type,
    subject       text,
    content       text,
    confidence    real,
    mention_count integer,
    last_seen_at  timestamptz,
    similarity    real,
    salience      real,
    score         real
)
language sql
stable
as $$
    select
        m.id,
        m.memory_type,
        m.subject,
        m.content,
        m.confidence,
        m.mention_count,
        m.last_seen_at,
        (1 - (m.embedding <=> p_query_embedding))::real as similarity,
        public.memory_salience(m.confidence, m.mention_count, m.last_seen_at, m.memory_type)
            as salience,
        ((1 - (m.embedding <=> p_query_embedding))
            * public.memory_salience(m.confidence, m.mention_count, m.last_seen_at, m.memory_type)
        )::real as score
    from public.memories m
    where m.user_id = p_user_id
      and m.status  = 'active'
      and m.embedding is not null
      and (p_types is null or m.memory_type = any (p_types))
      and (1 - (m.embedding <=> p_query_embedding)) >= p_min_similarity
    order by score desc
    limit greatest(p_match_count, 1);
$$;

comment on function public.match_memories is
    'User-scoped semantic memory search ranked by similarity x salience. Runs under the caller''s RLS.';


-- ---------------------------------------------------------------------------
-- Consolidation lookup
-- ---------------------------------------------------------------------------
-- Used on the WRITE path, before inserting a candidate memory. Returns the
-- closest existing memories so the consolidator can decide between three
-- outcomes:
--
--   similarity >= dedupe_threshold      -> reinforce the existing row
--   conflict <= similarity < dedupe     -> ask a small model to arbitrate
--   similarity <  conflict_threshold    -> insert as genuinely new
--
-- Restricting the comparison to the same `subject` slot when one is supplied
-- keeps the arbitration call rare, which matters on a rate-limited free tier.

create or replace function public.find_similar_memories(
    p_user_id         uuid,
    p_query_embedding extensions.vector(768),
    p_subject         text default null,
    p_limit           integer default 5
)
returns table (
    id            uuid,
    memory_type   public.memory_type,
    subject       text,
    content       text,
    confidence    real,
    mention_count integer,
    similarity    real
)
language sql
stable
as $$
    select
        m.id,
        m.memory_type,
        m.subject,
        m.content,
        m.confidence,
        m.mention_count,
        (1 - (m.embedding <=> p_query_embedding))::real as similarity
    from public.memories m
    where m.user_id = p_user_id
      and m.status  = 'active'
      and m.embedding is not null
      and (p_subject is null or m.subject = p_subject)
    order by m.embedding <=> p_query_embedding
    limit greatest(p_limit, 1);
$$;

comment on function public.find_similar_memories is
    'Write-path neighbour lookup driving deduplicate / arbitrate / insert.';
