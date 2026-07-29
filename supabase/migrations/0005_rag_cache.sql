-- =============================================================================
-- 0005  RAG corpus cache
-- =============================================================================
-- The retrieval corpus is Wikivoyage: freely licensed, no API key, and -
-- crucially - *structurally consistent*. Every city article uses the same
-- section headings (Understand / Get in / See / Do / Eat / Drink / Sleep), and
-- large cities link out to per-district sub-articles.
--
-- That structure is why multi-hop retrieval here is real rather than staged:
-- hop 1 reads a city article to learn its districts, hop 2 retrieves the
-- specific district articles that hop 1 named, hop 3 pulls venues from those
-- districts and cross-references them against the traveller's constraints.
-- Each hop's query is constructed from the previous hop's output.
--
-- Articles are fetched and embedded ON DEMAND, then cached here with a TTL,
-- rather than pre-ingesting a full dump. A dump would be tens of gigabytes
-- against a 500 MB free-tier database; this way the index warms around the
-- cities people actually ask about.
-- =============================================================================

create table if not exists public.documents (
    id           uuid primary key default gen_random_uuid(),

    -- Corpus this came from. Only 'wikivoyage' today, but the column keeps
    -- the retriever honest about provenance and leaves room for a second
    -- corpus without a migration.
    source       text not null default 'wikivoyage',

    -- Natural key within the source. For Wikivoyage this is the exact page
    -- title, e.g. 'Tokyo' or 'Tokyo/Shinjuku' - note that the district
    -- articles powering hop 2 are addressable by construction.
    source_id    text not null,

    title        text not null,
    url          text,
    lang         text not null default 'en',

    -- Populated for city articles from the article's own district links.
    -- Hop 1 writes this; hop 2 reads it. Caching it means a repeat visit to
    -- the same city skips hop 1 entirely.
    districts    text[] not null default '{}',

    content_hash text,
    fetched_at   timestamptz not null default now(),
    expires_at   timestamptz not null,

    unique (source, source_id, lang)
);

comment on table public.documents is
    'Cached source articles for RAG. Fetched on demand, expired by TTL.';
comment on column public.documents.districts is
    'District sub-article titles parsed from a city article. The link between RAG hop 1 and hop 2.';

create index if not exists documents_lookup_idx  on public.documents (source, source_id, lang);
create index if not exists documents_expiry_idx  on public.documents (expires_at);


-- ---------------------------------------------------------------------------
-- Chunks
-- ---------------------------------------------------------------------------
-- Chunking follows the article's own section headings rather than a fixed
-- character window. A Wikivoyage "Eat" section is already a coherent semantic
-- unit written by a human; splitting it every 512 characters would cut
-- restaurant entries in half and blur the section boundaries that make the
-- corpus useful. Oversized sections are sub-split, but the section label is
-- retained on every piece so retrieval can filter by intent - "Eat" for a
-- dietary constraint, "See" for attractions.

create table if not exists public.document_chunks (
    id           uuid primary key default gen_random_uuid(),
    document_id  uuid not null references public.documents (id) on delete cascade,

    chunk_index  integer not null,

    -- Wikivoyage section this text came from ('See', 'Eat', 'Sleep', ...).
    -- Doubles as a cheap metadata filter before the vector search runs.
    section      text,

    content      text not null,
    token_count  integer,
    embedding    extensions.vector(768),

    created_at   timestamptz not null default now(),

    unique (document_id, chunk_index)
);

create index if not exists document_chunks_embedding_idx
    on public.document_chunks
    using hnsw (embedding extensions.vector_cosine_ops)
    with (m = 16, ef_construction = 64);

create index if not exists document_chunks_document_idx on public.document_chunks (document_id);
create index if not exists document_chunks_section_idx  on public.document_chunks (section);


-- ---------------------------------------------------------------------------
-- Chunk search
-- ---------------------------------------------------------------------------
-- Scoped by document id list and/or section so the caller can express
-- "search only the Shinjuku and Shibuya articles, only their Eat sections".
-- That narrowing is what makes hop 3 precise instead of another blind
-- whole-corpus query.

create or replace function public.match_document_chunks(
    p_query_embedding extensions.vector(768),
    p_document_ids    uuid[] default null,
    p_sections        text[] default null,
    p_match_count     integer default 6,
    p_min_similarity  real default 0.30
)
returns table (
    id           uuid,
    document_id  uuid,
    document_title text,
    source_id    text,
    url          text,
    section      text,
    content      text,
    similarity   real
)
language sql
stable
as $$
    select
        c.id,
        c.document_id,
        d.title as document_title,
        d.source_id,
        d.url,
        c.section,
        c.content,
        (1 - (c.embedding <=> p_query_embedding))::real as similarity
    from public.document_chunks c
    join public.documents d on d.id = c.document_id
    where c.embedding is not null
      and (p_document_ids is null or c.document_id = any (p_document_ids))
      and (p_sections     is null or c.section     = any (p_sections))
      and (1 - (c.embedding <=> p_query_embedding)) >= p_min_similarity
    order by c.embedding <=> p_query_embedding
    limit greatest(p_match_count, 1);
$$;

comment on function public.match_document_chunks is
    'Vector search over cached article chunks, narrowable by document and section.';


-- ---------------------------------------------------------------------------
-- Cache eviction
-- ---------------------------------------------------------------------------
-- Called from the scheduled keep-alive job. The free tier caps the database
-- at 500 MB, so expired articles must actually be reclaimed rather than left
-- to accumulate. Chunks disappear via ON DELETE CASCADE.

create or replace function public.purge_expired_documents()
returns integer
language plpgsql
as $$
declare
    removed integer;
begin
    delete from public.documents where expires_at < now();
    get diagnostics removed = row_count;
    return removed;
end;
$$;
