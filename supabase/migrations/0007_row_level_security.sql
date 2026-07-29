-- =============================================================================
-- 0007  Row level security
-- =============================================================================
-- This migration is the reason the project stores memories in Postgres rather
-- than in a separate vector service.
--
-- With a standalone vector database, "user A can never read user B's
-- memories" is a promise made by application code: every query must remember
-- to filter by user id, and one forgotten `where` clause is a data breach.
-- Here the guarantee is enforced by the database. Even a query with no filter
-- at all returns only the caller's rows.
--
-- HOW THE BACKEND PARTICIPATES
-- ----------------------------
-- The FastAPI backend connects to Postgres directly, not through PostgREST,
-- so it must opt in to RLS explicitly. Before running any user-scoped query
-- it executes, inside the transaction:
--
--     set local role authenticated;
--     select set_config('request.jwt.claims', '{"sub":"<user-uuid>",...}', true);
--
-- `auth.uid()` reads that setting, so every policy below applies exactly as it
-- would for a browser client. See `app/db/session.py`. Genuinely privileged
-- work (writing traces, running the memory extractor) uses a separate,
-- explicit escalation path that is easy to grep for and is audited.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- Admin predicate
-- ---------------------------------------------------------------------------
-- SECURITY DEFINER so it can read `profiles` without triggering the policies
-- defined ON `profiles`. Without this indirection, a policy on `profiles` that
-- looks up the caller's role in `profiles` recurses infinitely - the most
-- common way RLS setups break on Supabase.

create or replace function public.is_admin()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select exists (
        select 1
        from public.profiles
        where id = auth.uid()
          and app_role = 'admin'
    );
$$;

comment on function public.is_admin is
    'True when the current caller is an admin. SECURITY DEFINER to avoid recursive policy evaluation.';

revoke all on function public.is_admin() from public;
grant execute on function public.is_admin() to authenticated, service_role;


-- ---------------------------------------------------------------------------
-- Enable RLS everywhere
-- ---------------------------------------------------------------------------
-- Enabled on every table without exception. A table with RLS disabled is
-- fully readable by any authenticated client through the auto-generated REST
-- API, which is a silent hole rather than a visible one.

alter table public.profiles         enable row level security;
alter table public.sessions         enable row level security;
alter table public.messages         enable row level security;
alter table public.memories         enable row level security;
alter table public.agent_runs       enable row level security;
alter table public.agent_steps      enable row level security;
alter table public.tool_calls       enable row level security;
alter table public.documents        enable row level security;
alter table public.document_chunks  enable row level security;
alter table public.admin_audit_log  enable row level security;
alter table public.message_feedback enable row level security;


-- ---------------------------------------------------------------------------
-- Profiles
-- ---------------------------------------------------------------------------

drop policy if exists profiles_select_own on public.profiles;
create policy profiles_select_own on public.profiles
    for select to authenticated
    using (id = auth.uid() or public.is_admin());

drop policy if exists profiles_update_own on public.profiles;
create policy profiles_update_own on public.profiles
    for update to authenticated
    using (id = auth.uid())
    with check (id = auth.uid());

-- Privilege escalation guard. The policy above lets users edit their own
-- profile, which would otherwise include `app_role`. RLS cannot express
-- column-level restrictions, so the invariant is enforced by trigger instead:
-- role changes are permitted only for the service role.
create or replace function public.guard_app_role()
returns trigger
language plpgsql
as $$
begin
    if new.app_role is distinct from old.app_role
       and current_setting('role', true) is distinct from 'service_role' then
        raise exception 'app_role may only be changed by the service role'
            using errcode = '42501';
    end if;
    return new;
end;
$$;

drop trigger if exists profiles_guard_app_role on public.profiles;
create trigger profiles_guard_app_role
    before update on public.profiles
    for each row execute function public.guard_app_role();


-- ---------------------------------------------------------------------------
-- Sessions and messages
-- ---------------------------------------------------------------------------

drop policy if exists sessions_select on public.sessions;
create policy sessions_select on public.sessions
    for select to authenticated
    using (user_id = auth.uid() or public.is_admin());

drop policy if exists sessions_insert_own on public.sessions;
create policy sessions_insert_own on public.sessions
    for insert to authenticated
    with check (user_id = auth.uid());

drop policy if exists sessions_update_own on public.sessions;
create policy sessions_update_own on public.sessions
    for update to authenticated
    using (user_id = auth.uid())
    with check (user_id = auth.uid());

drop policy if exists sessions_delete_own on public.sessions;
create policy sessions_delete_own on public.sessions
    for delete to authenticated
    using (user_id = auth.uid());


drop policy if exists messages_select on public.messages;
create policy messages_select on public.messages
    for select to authenticated
    using (user_id = auth.uid() or public.is_admin());

drop policy if exists messages_insert_own on public.messages;
create policy messages_insert_own on public.messages
    for insert to authenticated
    with check (user_id = auth.uid());

-- Deliberately no update policy: a transcript that can be edited after the
-- fact is not a transcript. Corrections are new messages.


-- ---------------------------------------------------------------------------
-- Memories
-- ---------------------------------------------------------------------------
-- Users can read, correct and delete what the system believes about them.
-- Admins can read and delete (for support and abuse handling) but cannot
-- write - an admin should never be able to fabricate a preference that the
-- agent will then act on as if the user had stated it.

drop policy if exists memories_select on public.memories;
create policy memories_select on public.memories
    for select to authenticated
    using (user_id = auth.uid() or public.is_admin());

drop policy if exists memories_insert_own on public.memories;
create policy memories_insert_own on public.memories
    for insert to authenticated
    with check (user_id = auth.uid());

drop policy if exists memories_update_own on public.memories;
create policy memories_update_own on public.memories
    for update to authenticated
    using (user_id = auth.uid())
    with check (user_id = auth.uid());

drop policy if exists memories_delete on public.memories;
create policy memories_delete on public.memories
    for delete to authenticated
    using (user_id = auth.uid() or public.is_admin());


-- ---------------------------------------------------------------------------
-- Agent traces
-- ---------------------------------------------------------------------------
-- Read-only to everyone except the service role. Traces are an audit record;
-- if the subject of a trace could edit it, it would not be worth keeping.
-- Steps and tool calls are reachable through their parent run's ownership.

drop policy if exists agent_runs_select on public.agent_runs;
create policy agent_runs_select on public.agent_runs
    for select to authenticated
    using (user_id = auth.uid() or public.is_admin());

drop policy if exists agent_steps_select on public.agent_steps;
create policy agent_steps_select on public.agent_steps
    for select to authenticated
    using (
        exists (
            select 1 from public.agent_runs r
            where r.id = agent_steps.run_id
              and (r.user_id = auth.uid() or public.is_admin())
        )
    );

drop policy if exists tool_calls_select on public.tool_calls;
create policy tool_calls_select on public.tool_calls
    for select to authenticated
    using (
        exists (
            select 1 from public.agent_runs r
            where r.id = tool_calls.run_id
              and (r.user_id = auth.uid() or public.is_admin())
        )
    );


-- ---------------------------------------------------------------------------
-- RAG corpus
-- ---------------------------------------------------------------------------
-- Not user data - a shared cache of public, freely licensed articles. Any
-- signed-in user may read it; only the service role populates it.

drop policy if exists documents_select on public.documents;
create policy documents_select on public.documents
    for select to authenticated
    using (true);

drop policy if exists document_chunks_select on public.document_chunks;
create policy document_chunks_select on public.document_chunks
    for select to authenticated
    using (true);


-- ---------------------------------------------------------------------------
-- Audit log
-- ---------------------------------------------------------------------------
-- Readable by admins, writable by nobody. There is no insert, update or
-- delete policy for any role, so the table is append-only via the service
-- role and immutable to every authenticated caller including admins
-- themselves - an audit log an admin can edit records nothing.

drop policy if exists admin_audit_select on public.admin_audit_log;
create policy admin_audit_select on public.admin_audit_log
    for select to authenticated
    using (public.is_admin());


-- ---------------------------------------------------------------------------
-- Feedback
-- ---------------------------------------------------------------------------

drop policy if exists message_feedback_select on public.message_feedback;
create policy message_feedback_select on public.message_feedback
    for select to authenticated
    using (user_id = auth.uid() or public.is_admin());

drop policy if exists message_feedback_write_own on public.message_feedback;
create policy message_feedback_write_own on public.message_feedback
    for insert to authenticated
    with check (user_id = auth.uid());

drop policy if exists message_feedback_update_own on public.message_feedback;
create policy message_feedback_update_own on public.message_feedback
    for update to authenticated
    using (user_id = auth.uid())
    with check (user_id = auth.uid());


-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------
-- RLS narrows access; it does not grant it. Both are required.
-- `anon` is granted nothing: every route in this application requires a
-- signed-in user.

grant usage on schema public to authenticated, service_role;

grant select                         on public.profiles         to authenticated;
grant update                         on public.profiles         to authenticated;
grant select, insert, update, delete on public.sessions         to authenticated;
grant select, insert                 on public.messages         to authenticated;
grant select, insert, update, delete on public.memories         to authenticated;
grant select                         on public.agent_runs       to authenticated;
grant select                         on public.agent_steps      to authenticated;
grant select                         on public.tool_calls       to authenticated;
grant select                         on public.documents        to authenticated;
grant select                         on public.document_chunks  to authenticated;
grant select                         on public.admin_audit_log  to authenticated;
grant select, insert, update         on public.message_feedback to authenticated;

grant execute on function public.match_memories(uuid, extensions.vector, integer, real,
                                                public.memory_type[]) to authenticated;
grant execute on function public.find_similar_memories(uuid, extensions.vector, text, integer)
    to authenticated;
grant execute on function public.match_document_chunks(extensions.vector, uuid[], text[],
                                                       integer, real) to authenticated;
grant execute on function public.memory_salience(real, integer, timestamptz, public.memory_type)
    to authenticated;

grant all on all tables    in schema public to service_role;
grant all on all functions in schema public to service_role;
grant all on all sequences in schema public to service_role;
