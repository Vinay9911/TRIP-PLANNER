-- =============================================================================
-- 0006  Admin audit log, feedback, and privacy operations
-- =============================================================================
-- The admin portal lets a privileged user read every conversation and every
-- stored memory belonging to every user. That is a legitimate operational
-- need for this system and also a genuine privacy exposure, so it is paired
-- with two controls:
--
--   * an append-only audit log of what admins looked at and changed, and
--   * a user-facing export/erase path, so "the system remembers you" comes
--     with a way to make it stop.
--
-- Neither is required by the brief. Both are the kind of thing that gets
-- asked about in review once someone notices the admin can read everything.
-- =============================================================================

create table if not exists public.admin_audit_log (
    id             uuid primary key default gen_random_uuid(),
    admin_id       uuid not null references public.profiles (id) on delete cascade,

    -- Verb, e.g. 'view_user_sessions', 'delete_memory', 'export_user_data'.
    action         text not null,

    -- Subject of the action, when it concerned one specific user.
    target_user_id uuid references public.profiles (id) on delete set null,

    -- Type and id of the affected row, e.g. ('memory', <uuid>).
    target_type    text,
    target_id      uuid,

    details        jsonb not null default '{}'::jsonb,
    ip_address     inet,
    created_at     timestamptz not null default now()
);

comment on table public.admin_audit_log is
    'Append-only record of privileged access. No update or delete policy exists for any role.';

create index if not exists admin_audit_admin_idx  on public.admin_audit_log (admin_id, created_at desc);
create index if not exists admin_audit_target_idx on public.admin_audit_log (target_user_id, created_at desc);
create index if not exists admin_audit_action_idx on public.admin_audit_log (action, created_at desc);


-- ---------------------------------------------------------------------------
-- Response feedback
-- ---------------------------------------------------------------------------
-- A thumbs up/down on an assistant message. Cheap to collect and it turns the
-- admin portal from a viewer into something you can actually act on: filter
-- to downvoted runs, open the trace, and see which step produced the bad
-- answer.

create table if not exists public.message_feedback (
    id         uuid primary key default gen_random_uuid(),
    message_id uuid not null references public.messages (id) on delete cascade,
    user_id    uuid not null references public.profiles (id) on delete cascade,
    run_id     uuid references public.agent_runs (id) on delete set null,

    rating     smallint not null check (rating in (-1, 1)),
    comment    text,
    created_at timestamptz not null default now(),

    -- One verdict per user per message; a changed mind updates in place.
    unique (message_id, user_id)
);

create index if not exists message_feedback_rating_idx
    on public.message_feedback (rating, created_at desc);


-- ---------------------------------------------------------------------------
-- Erasure
-- ---------------------------------------------------------------------------
-- Hard-deletes everything derived from a user while leaving the account
-- intact, so "forget everything about me" does not mean "delete my login".
--
-- SECURITY DEFINER is required: the function must delete rows that the
-- calling user's own policies allow them to delete anyway, but it also
-- touches trace tables that users cannot address directly. `search_path` is
-- pinned to defeat search-path hijacking, and the body re-checks the caller
-- against the target so a user cannot pass someone else's id.

create or replace function public.erase_user_data(p_user_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_caller     uuid := auth.uid();
    v_is_admin   boolean;
    v_memories   integer;
    v_messages   integer;
    v_sessions   integer;
begin
    select app_role = 'admin' into v_is_admin from public.profiles where id = v_caller;

    -- Callers may erase themselves; admins may erase anyone. Checked here
    -- rather than in the API so the guarantee survives a future caller that
    -- forgets to check.
    if v_caller is distinct from p_user_id and coalesce(v_is_admin, false) = false then
        raise exception 'not authorised to erase data for user %', p_user_id
            using errcode = '42501';
    end if;

    delete from public.memories where user_id = p_user_id;
    get diagnostics v_memories = row_count;

    delete from public.messages where user_id = p_user_id;
    get diagnostics v_messages = row_count;

    -- Cascades into agent_runs, agent_steps and tool_calls.
    delete from public.sessions where user_id = p_user_id;
    get diagnostics v_sessions = row_count;

    return jsonb_build_object(
        'user_id',           p_user_id,
        'memories_deleted',  v_memories,
        'messages_deleted',  v_messages,
        'sessions_deleted',  v_sessions,
        'erased_at',         now()
    );
end;
$$;

comment on function public.erase_user_data is
    'Deletes all derived data for a user, keeping the auth account. Self-service or admin only.';

revoke all on function public.erase_user_data(uuid) from public;
grant execute on function public.erase_user_data(uuid) to authenticated;
