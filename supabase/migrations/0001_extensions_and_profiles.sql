-- =============================================================================
-- 0001  Extensions and user profiles
-- =============================================================================
-- Supabase Auth owns the `auth.users` table: it stores credentials, OAuth
-- identities and email confirmation state, and we neither read nor write it
-- directly. Application-level facts about a user live in `public.profiles`,
-- keyed 1:1 by the same UUID.
--
-- Why a separate table rather than `auth.users.raw_user_meta_data`? Because
-- user metadata on `auth.users` is writable by the user's own client. A role
-- flag stored there would be self-assignable - any authenticated user could
-- make themselves an admin from the browser console. `profiles.app_role` is
-- writable only by the service role, which the browser never holds.
-- =============================================================================

create extension if not exists "vector"    with schema extensions;
create extension if not exists "pgcrypto"  with schema extensions;


-- Application roles. Kept as an enum so a typo becomes an error at write time
-- rather than a silently unmatched string in an authorization check.
do $$
begin
    if not exists (select 1 from pg_type where typname = 'app_role') then
        create type public.app_role as enum ('user', 'admin');
    end if;
end
$$;


create table if not exists public.profiles (
    id                 uuid primary key references auth.users (id) on delete cascade,
    email              text,
    display_name       text,
    avatar_url         text,

    -- Set only by the service role. See the note above.
    app_role           public.app_role not null default 'user',

    -- Last language the user was observed writing in (BCP-47, e.g. 'ja').
    -- Used to pick a reply language on the first turn of a new session,
    -- before the current message has been classified.
    preferred_language text,

    -- Lets a user opt out of long-term memory extraction entirely. Checked
    -- before the extractor runs, not merely before retrieval, so opting out
    -- means nothing is written rather than written-but-hidden.
    memory_enabled     boolean not null default true,

    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    last_seen_at       timestamptz
);

comment on table public.profiles is
    'Application-level user record, 1:1 with auth.users. Holds the authoritative role flag.';
comment on column public.profiles.app_role is
    'Authorization role. Service-role writable only - never derived from a client-supplied claim.';
comment on column public.profiles.memory_enabled is
    'When false, the long-term memory extractor is skipped entirely for this user.';

create index if not exists profiles_app_role_idx    on public.profiles (app_role);
create index if not exists profiles_last_seen_idx   on public.profiles (last_seen_at desc nulls last);


-- ---------------------------------------------------------------------------
-- updated_at maintenance
-- ---------------------------------------------------------------------------
-- Applied by trigger rather than by application code so that the timestamp is
-- correct regardless of which code path performed the write - including
-- manual fixes run from the SQL editor.

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists profiles_touch_updated_at on public.profiles;
create trigger profiles_touch_updated_at
    before update on public.profiles
    for each row execute function public.touch_updated_at();


-- ---------------------------------------------------------------------------
-- Profile provisioning
-- ---------------------------------------------------------------------------
-- A profile row is created by trigger the moment Supabase Auth inserts into
-- auth.users. Doing it in the database rather than in the API guarantees the
-- row exists no matter how the account was created - email signup, Google
-- OAuth, or an invite issued from the Supabase dashboard. An application-side
-- "create profile after signup" call would silently miss the latter two.

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.profiles (id, email, display_name, avatar_url)
    values (
        new.id,
        new.email,
        coalesce(
            new.raw_user_meta_data ->> 'full_name',
            new.raw_user_meta_data ->> 'name',
            split_part(coalesce(new.email, ''), '@', 1)
        ),
        new.raw_user_meta_data ->> 'avatar_url'
    )
    on conflict (id) do nothing;
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();
