-- Trip state: the slot-filling ledger for the conversational planner.
--
-- The agent no longer jumps straight from "i want to go to kerala" to a full
-- itinerary. It advises first - grounded options, a draft outline, at most two
-- questions - and only runs the expensive plan-execute pipeline once the trip
-- is specified or the traveller asks for it. That requires remembering, per
-- conversation, which slots are already filled (origin, duration, dates,
-- priorities, budget tier) and which services the traveller has switched off
-- (flights / attractions / stays / restaurants).
--
-- A JSONB column on sessions rather than new columns per slot, deliberately:
-- the slot set will evolve with the prompt, and a migration per prompt tweak
-- is friction that ends with slots quietly living in application memory
-- instead. The schema of the blob is owned and documented by
-- backend/app/agent/trip_state.py, which is the only writer.
--
-- Not stored in long-term memory: trip state is about THIS trip ("5 days,
-- early October") and is useless - actively wrong - on the next one. Durable
-- facts (home city, diet) still flow through the memory pipeline.

alter table public.sessions
    add column if not exists trip_state jsonb not null default '{}'::jsonb;

comment on column public.sessions.trip_state is
    'Per-conversation planning slots (origin, duration, priorities, service focus, outline confirmation). Schema owned by backend/app/agent/trip_state.py.';
