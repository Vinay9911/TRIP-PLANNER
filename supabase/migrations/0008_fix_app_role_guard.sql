-- =============================================================================
-- 0008  Fix the app_role guard so legitimate admin paths still work
-- =============================================================================
-- The trigger added in 0007 blocks privilege escalation by refusing any change
-- to `profiles.app_role` unless `current_setting('role')` is 'service_role'.
--
-- That check is too narrow, and it was caught the first time someone tried to
-- promote an admin: `current_setting('role')` only returns 'service_role' when
-- something has explicitly issued `SET ROLE service_role`. A direct superuser
-- connection - the Supabase SQL editor, psql, or a backend connecting as
-- `postgres` - reports 'none' and was refused. The instruction in README.md
-- for creating the first admin could therefore never have worked.
--
-- The security property we actually want is narrower than the check was:
--
--   an `authenticated` end user must never be able to change app_role,
--   including their own.
--
-- Everything else - the service role, the Supabase SQL editor, a migration -
-- is already a trusted administrative context. So the guard now rejects the
-- change when the *connecting* role is untrusted, rather than requiring one
-- specific value of a session GUC.
--
-- `current_user` is the role actually in force, and `SET LOCAL ROLE
-- authenticated` (which app/db/session.py issues for every user-scoped query)
-- changes it. So a request made on behalf of a signed-in user is still
-- refused, which is the case that matters.
-- =============================================================================

create or replace function public.guard_app_role()
returns trigger
language plpgsql
as $$
declare
    -- Roles permitted to change app_role. Deliberately does NOT include
    -- `authenticated` or `anon`: those are what an end user's request runs as,
    -- and letting either through would make the column self-assignable.
    trusted_roles constant text[] := array[
        'service_role', 'postgres', 'supabase_admin', 'supabase_auth_admin'
    ];
begin
    if new.app_role is distinct from old.app_role then
        if not (
            current_user = any (trusted_roles)
            or coalesce(current_setting('role', true), '') = 'service_role'
        ) then
            raise exception
                'app_role may only be changed by a trusted administrative role '
                '(attempted as %)', current_user
                using errcode = '42501';
        end if;
    end if;

    return new;
end;
$$;

comment on function public.guard_app_role is
    'Blocks privilege escalation: an authenticated end user can never change app_role.';


-- ---------------------------------------------------------------------------
-- Convenience: promote a user to admin by email
-- ---------------------------------------------------------------------------
-- Wraps the update so the documented first-admin step is one obvious call
-- rather than a hand-written UPDATE that has to get the predicate right.
--
-- Deliberately NOT `security definer`: it must run with the caller's own
-- privileges so the guard above still applies. Called by an untrusted role, it
-- fails exactly as a direct UPDATE would.

create or replace function public.promote_to_admin(p_email text)
returns text
language plpgsql
as $$
declare
    v_id uuid;
begin
    select id into v_id from public.profiles where email = p_email;

    if v_id is null then
        return format(
            'No profile found for %s. The account must sign in once before it can '
            'be promoted - the profile row is created by trigger on first signup.',
            p_email
        );
    end if;

    update public.profiles set app_role = 'admin' where id = v_id;
    return format('%s is now an admin (%s).', p_email, v_id);
end;
$$;

comment on function public.promote_to_admin is
    'Promote an existing user to admin by email. Run from the Supabase SQL editor.';

revoke all on function public.promote_to_admin(text) from public, anon, authenticated;
