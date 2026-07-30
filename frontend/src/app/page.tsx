"use client";

/**
 * The chat page.
 *
 * A few deliberate choices worth noting.
 *
 * Each reply can expand to show **what the agent actually did** - the plan it
 * wrote and every tool it called, with latencies. Most chat interfaces hide
 * this; showing it turns the documentation's claims into something a reviewer
 * can verify in the UI.
 *
 * The **service toggles** (flights / attractions / stays / restaurants) are
 * not client-side filters. The selection travels with every message, and a
 * switched-off service's tool is removed from the agent's toolbox on the
 * backend - the toggle is a guarantee, not a display preference.
 *
 * The **quick actions** (both the big welcome cards and the slim strip above
 * the composer) exist twice on purpose. A first-time user needs the big,
 * explained version once; a returning user mid-conversation needs the
 * shortcuts to still be reachable without scrolling back to the top - an
 * earlier version only rendered them on the empty state, so they vanished
 * the moment a conversation started, which is exactly the discoverability
 * gap real usage surfaced.
 *
 * The **trip bar** renders the `trip_state` ledger the backend returns: what
 * the agent has gathered so far. It makes the slot-filling conversation
 * legible - the user can see exactly what the agent knows and what it is
 * still missing, without reading the reply text closely enough to reverse
 * engineer it.
 *
 * **Images are illustrative, not literal.** There is no image-search API in
 * this project (no key to manage, no cost to justify), so destination chips
 * use a seeded placeholder photo - deterministic per place name, so the same
 * place always shows the same image, but not actually a photo of that place.
 * That trade-off is stated plainly here because it would be misleading not
 * to: the visual polish is real, the photographic accuracy is not.
 */

import { useEffect, useRef, useState } from "react";

import { AuthGate } from "@/components/AuthGate";
import {
  Badge,
  Button,
  Card,
  ErrorBanner,
  FormattedText,
  Nav,
} from "@/components/ui";
import {
  ApiError,
  api,
  type ChatResponse,
  type FocusService,
  type TripState,
} from "@/lib/api";
import { signOut } from "@/lib/supabase";

interface Turn {
  role: "user" | "assistant";
  content: string;
  meta?: ChatResponse;
}

/** Deterministic placeholder photo for a place name. No API key, no cost -
 *  see the module docstring for why this is a deliberate trade-off. */
function placeImage(seed: string, width = 300, height = 200): string {
  return `https://picsum.photos/seed/${encodeURIComponent(seed.toLowerCase())}/${width}/${height}`;
}

/**
 * The composer toggles. Order and ids mirror FOCUS_SERVICES on the backend;
 * the emoji double as the icons the agent's own replies use, so the sentence
 * "I can also check flights ✈️" teaches the toggle row and vice versa.
 */
const SERVICES: { id: FocusService; label: string }[] = [
  { id: "flights", label: "✈️ Flights" },
  { id: "attractions", label: "🎡 Attractions" },
  { id: "stays", label: "🏨 Stays" },
  { id: "restaurants", label: "🍽️ Restaurants" },
];

/**
 * What the agent can do, as clickable shortcuts. Each fills the composer
 * with a scoped opener the user completes with a destination - discovery by
 * doing, instead of a features page nobody reads. Rendered twice: large on
 * the welcome screen, compact in the always-visible strip above the composer.
 */
const CAPABILITIES: { icon: string; title: string; hint: string; prompt: string }[] = [
  {
    icon: "🌏",
    title: "Plan a trip",
    hint: "Tell it a place — it suggests, asks, then plans",
    prompt: "I want to go to ",
  },
  {
    icon: "✈️",
    title: "Find flights",
    hint: "Compare routes and simulated fares",
    prompt: "Find me flights to ",
  },
  {
    icon: "🏨",
    title: "Find a stay",
    hint: "Places to sleep that fit your budget",
    prompt: "Find me a place to stay in ",
  },
  {
    icon: "🎡",
    title: "Discover attractions",
    hint: "What's genuinely worth seeing",
    prompt: "What are the best things to see in ",
  },
  {
    icon: "🍽️",
    title: "Eat well",
    hint: "Food that matches your dietary needs",
    prompt: "Where should I eat in ",
  },
  {
    icon: "🌦️",
    title: "Check the weather",
    hint: "Real forecasts, up to 16 days out",
    prompt: "What's the weather like in ",
  },
];

const EXAMPLES = [
  "I want to go to Kerala",
  "Plan me 2 relaxed days in Kyoto. I'm vegetarian.",
  "Will I need an umbrella in Singapore next week?",
  "京都で2日間の旅程を立ててください",
];

/** A few seeded photos purely for first-impression polish on the welcome screen. */
const HERO_PLACES = ["Kyoto", "Kerala backwaters", "Santorini", "Marrakech"];

export default function ChatPage() {
  return <AuthGate>{(session) => <Chat session={session} />}</AuthGate>;
}

function Chat({
  session,
}: {
  session: { email: string | null; isAdmin: boolean };
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [sessionId, setSessionId] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [focus, setFocus] = useState<FocusService[]>(SERVICES.map((s) => s.id));
  const [trip, setTrip] = useState<TripState>({});
  const [resetting, setResetting] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, busy]);

  function toggleService(id: FocusService) {
    setFocus((previous) => {
      const next = new Set(previous);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      // Canonical order, so the array sent to the API is stable regardless
      // of the order the user clicked in.
      return SERVICES.map((service) => service.id).filter((service) => next.has(service));
    });
  }

  function insertPrompt(prompt: string) {
    setInput((current) => (current ? `${current.trimEnd()} ${prompt}` : prompt));
    inputRef.current?.focus();
  }

  function startNewChat() {
    setTurns([]);
    setSessionId(undefined);
    setInput("");
    setError(null);
    setTrip({});
    inputRef.current?.focus();
  }

  async function resetEverything() {
    if (
      !window.confirm(
        "This permanently erases everything remembered about you (preferences, past " +
          "trips) and starts a brand new conversation. Your account stays. Continue?",
      )
    ) {
      return;
    }
    setResetting(true);
    setError(null);
    try {
      await api.eraseMyData();
      startNewChat();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not erase your data.");
    } finally {
      setResetting(false);
    }
  }

  async function send(message: string) {
    const trimmed = message.trim();
    if (!trimmed || busy) return;

    setError(null);
    setInput("");
    setTurns((previous) => [...previous, { role: "user", content: trimmed }]);
    setBusy(true);

    try {
      const reply = await api.chat(trimmed, sessionId, focus);
      // Reusing the session id is what gives the agent conversational
      // context - follow-ups like "make day two lighter" depend on it.
      setSessionId(reply.session_id);
      setTrip(reply.trip_state ?? {});
      setTurns((previous) => [
        ...previous,
        { role: "assistant", content: reply.response, meta: reply },
      ]);
    } catch (caught) {
      const message =
        caught instanceof ApiError
          ? caught.message
          : "Could not reach the planner. Please try again.";
      setError(message);
      // Put the message back so a failure does not lose what they typed.
      setInput(trimmed);
      setTurns((previous) => previous.slice(0, -1));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto flex min-h-screen max-w-4xl flex-col px-4 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--color-line)] py-4">
        <span className="font-semibold tracking-tight">🌏 Trip Planner</span>
        <div className="flex flex-wrap items-center gap-2">
          <Nav email={session.email} isAdmin={session.isAdmin} />
          <Button variant="secondary" onClick={startNewChat} disabled={turns.length === 0}>
            + New chat
          </Button>
          <button
            type="button"
            onClick={() => void resetEverything()}
            disabled={resetting}
            title="Erase everything remembered about you and start completely fresh"
            className="rounded-lg px-3 py-2 text-sm text-[var(--color-ink-soft)] transition-colors hover:text-[var(--color-danger)] disabled:opacity-50"
          >
            {resetting ? "Resetting…" : "🗑️ Start fresh"}
          </button>
          <Button variant="ghost" onClick={() => void signOut()}>
            Sign out
          </Button>
        </div>
      </header>

      <main className="flex-1 space-y-5 py-6">
        {turns.length === 0 && (
          <Welcome onPick={(text) => void send(text)} onPrompt={insertPrompt} />
        )}

        {turns.map((turn, index) => (
          <Message key={index} turn={turn} onPickOption={(text) => void send(text)} />
        ))}

        {busy && <Thinking />}
        {error && <ErrorBanner message={error} />}
        <div ref={endRef} />
      </main>

      <footer className="sticky bottom-0 space-y-2 bg-[var(--color-paper)] pb-6 pt-3">
        <TripBar trip={trip} />

        {turns.length > 0 && <QuickActionsStrip onPrompt={insertPrompt} />}

        <div className="flex flex-wrap items-center gap-1.5">
          {SERVICES.map((service) => {
            const enabled = focus.includes(service.id);
            return (
              <button
                key={service.id}
                type="button"
                onClick={() => toggleService(service.id)}
                aria-pressed={enabled}
                title={
                  enabled
                    ? `${service.label} is on — the agent may use it`
                    : `${service.label} is off — its tool is withheld from the agent`
                }
                className={`rounded-full border px-3 py-1 text-xs transition-colors ${
                  enabled
                    ? "border-[var(--color-accent)] bg-[var(--color-accent-soft)] text-[var(--color-ink)]"
                    : "border-[var(--color-line)] bg-[var(--color-surface)] text-[var(--color-ink-soft)] line-through opacity-70"
                }`}
              >
                {service.label}
              </button>
            );
          })}
          <span className="ml-1 text-[11px] text-[var(--color-ink-soft)]">
            switched-off services are hidden from the agent
          </span>
        </div>

        <TripDetailsPicker onInsert={insertPrompt} />

        <form
          onSubmit={(event) => {
            event.preventDefault();
            void send(input);
          }}
          className="flex gap-2"
        >
          <input
            ref={inputRef}
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="Where would you like to go?"
            disabled={busy}
            aria-label="Your message"
            className="flex-1 rounded-xl border border-[var(--color-line)] bg-[var(--color-surface)] px-4 py-3 text-sm disabled:opacity-60"
          />
          <Button type="submit" disabled={busy || !input.trim()}>
            Send
          </Button>
        </form>
        <p className="text-center text-xs text-[var(--color-ink-soft)]">
          Flight and hotel prices are simulated. Everything else is real data.
        </p>
      </footer>
    </div>
  );
}

function Welcome({
  onPick,
  onPrompt,
}: {
  onPick: (text: string) => void;
  onPrompt: (prompt: string) => void;
}) {
  return (
    <div className="py-6">
      <div className="flex flex-wrap items-center gap-2 sm:flex-nowrap">
        {HERO_PLACES.map((place) => (
          <img
            key={place}
            src={placeImage(place, 200, 140)}
            alt=""
            aria-hidden
            loading="lazy"
            className="h-16 w-24 flex-1 rounded-xl object-cover sm:h-20 sm:w-32"
          />
        ))}
      </div>

      <h2 className="mt-5 text-xl font-semibold">Where to next? 🧳</h2>
      <p className="mt-1 text-sm text-[var(--color-ink-soft)]">
        Name a place and it will suggest, ask the right questions, then build
        the plan — or jump straight to one specific thing. Mention anything
        that matters (diet, budget, pace, who&apos;s coming) and it remembers
        for next time.
      </p>

      <div className="mt-3 flex items-start gap-2 rounded-xl border border-[var(--color-line)] bg-[var(--color-accent-soft)] px-4 py-3 text-sm">
        <span aria-hidden>💡</span>
        <p>
          <strong>What helps most:</strong> your destination, how many days,
          roughly when, who&apos;s coming, and any must-haves (diet, budget,
          pace). Give what you know — it will ask about anything important
          that&apos;s missing.
        </p>
      </div>

      <div className="mt-5 grid gap-2 sm:grid-cols-3">
        {CAPABILITIES.map((capability) => (
          <button
            key={capability.title}
            type="button"
            onClick={() => onPrompt(capability.prompt)}
            className="rounded-xl border border-[var(--color-line)] bg-[var(--color-surface)] p-3 text-left transition-colors hover:border-[var(--color-accent)]"
          >
            <span className="text-lg" aria-hidden>
              {capability.icon}
            </span>
            <span className="mt-1 block text-sm font-medium">{capability.title}</span>
            <span className="mt-0.5 block text-xs text-[var(--color-ink-soft)]">
              {capability.hint}
            </span>
          </button>
        ))}
      </div>

      <p className="mt-6 text-xs font-medium uppercase tracking-wide text-[var(--color-ink-soft)]">
        Or try one of these
      </p>
      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            onClick={() => onPick(example)}
            className="rounded-xl border border-[var(--color-line)] bg-[var(--color-surface)] p-3 text-left text-sm transition-colors hover:border-[var(--color-accent)]"
          >
            {example}
          </button>
        ))}
      </div>
    </div>
  );
}

/**
 * The always-visible, compact version of the capability shortcuts. Exists
 * because the big welcome cards disappear the moment a conversation starts
 * (by design - they are a first-impression, not a permanent fixture), which
 * left no way to reach "find a stay" or "check weather" mid-conversation
 * without scrolling up and re-reading. This strip is what stays.
 */
function QuickActionsStrip({ onPrompt }: { onPrompt: (prompt: string) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-[11px] font-medium uppercase tracking-wide text-[var(--color-ink-soft)]">
        Quick actions
      </span>
      {CAPABILITIES.map((capability) => (
        <button
          key={capability.title}
          type="button"
          onClick={() => onPrompt(capability.prompt)}
          title={capability.hint}
          className="rounded-full border border-[var(--color-line)] bg-[var(--color-surface)] px-2.5 py-1 text-xs transition-colors hover:border-[var(--color-accent)]"
        >
          <span aria-hidden>{capability.icon}</span> {capability.title}
        </button>
      ))}
    </div>
  );
}

/**
 * A clickable way to add dates and trip length without typing them out.
 * Deliberately simple - native date inputs and a stepper, not a custom
 * calendar widget - because the ask was "make it clickable", not "build a
 * date-picker component library" for something used a handful of times per
 * conversation.
 */
function TripDetailsPicker({ onInsert }: { onInsert: (text: string) => void }) {
  const [open, setOpen] = useState(false);
  const [days, setDays] = useState(0);
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");

  const canInsert = days > 0 || startDate;

  function insert() {
    const parts: string[] = [];
    if (days > 0) parts.push(`${days} day${days === 1 ? "" : "s"}`);
    if (startDate && endDate) parts.push(`from ${startDate} to ${endDate}`);
    else if (startDate) parts.push(`starting ${startDate}`);
    if (parts.length > 0) onInsert(parts.join(", "));
    setOpen(false);
    setDays(0);
    setStartDate("");
    setEndDate("");
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="flex items-center gap-1 text-xs text-[var(--color-accent)] underline underline-offset-2"
      >
        🗓️ Add dates or trip length
      </button>
    );
  }

  return (
    <div className="flex flex-wrap items-end gap-3 rounded-xl border border-[var(--color-line)] bg-[var(--color-surface)] p-3 text-xs">
      <div>
        <label className="mb-1 block text-[var(--color-ink-soft)]">Days</label>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setDays((value) => Math.max(0, value - 1))}
            className="h-7 w-7 rounded-lg border border-[var(--color-line)] font-medium"
            aria-label="Fewer days"
          >
            −
          </button>
          <span className="w-6 text-center tabular-nums">{days}</span>
          <button
            type="button"
            onClick={() => setDays((value) => Math.min(90, value + 1))}
            className="h-7 w-7 rounded-lg border border-[var(--color-line)] font-medium"
            aria-label="More days"
          >
            +
          </button>
        </div>
      </div>
      <div>
        <label className="mb-1 block text-[var(--color-ink-soft)]" htmlFor="trip-start">
          Start date
        </label>
        <input
          id="trip-start"
          type="date"
          value={startDate}
          onChange={(event) => setStartDate(event.target.value)}
          className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-2 py-1.5"
        />
      </div>
      <div>
        <label className="mb-1 block text-[var(--color-ink-soft)]" htmlFor="trip-end">
          End date
        </label>
        <input
          id="trip-end"
          type="date"
          value={endDate}
          min={startDate || undefined}
          onChange={(event) => setEndDate(event.target.value)}
          className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-2 py-1.5"
        />
      </div>
      <Button variant="secondary" onClick={insert} disabled={!canInsert}>
        Add to message
      </Button>
      <button
        type="button"
        onClick={() => setOpen(false)}
        className="text-[var(--color-ink-soft)] underline underline-offset-2"
      >
        Cancel
      </button>
    </div>
  );
}

/**
 * The slot ledger, rendered. Empty slots simply do not appear, so the bar
 * grows as the conversation fills the trip in - a progress indicator that is
 * also an honesty check on what the agent claims to know.
 */
function TripBar({ trip }: { trip: TripState }) {
  const chips: string[] = [];
  if (trip.destination) chips.push(`📍 ${trip.destination}`);
  if (trip.duration_days) chips.push(`🗓️ ${trip.duration_days} days`);
  if (trip.start_date) {
    chips.push(`⏱️ ${trip.start_date}${trip.end_date ? ` → ${trip.end_date}` : ""}`);
  } else if (trip.travel_window) {
    chips.push(`⏱️ ${trip.travel_window}`);
  }
  if (trip.origin) chips.push(`🛫 from ${trip.origin}`);
  if (trip.party) chips.push(`👥 ${trip.party}`);
  if (trip.budget_tier) chips.push(`💰 ${trip.budget_tier}`);
  if (trip.priorities?.length) chips.push(`✨ ${trip.priorities.join(", ")}`);

  if (chips.length === 0) return null;

  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs">
      {trip.destination && (
        <img
          src={placeImage(trip.destination, 40, 40)}
          alt=""
          aria-hidden
          className="h-6 w-6 rounded-full object-cover"
        />
      )}
      <span className="font-medium text-[var(--color-ink-soft)]">Your trip:</span>
      {chips.map((chip) => (
        <span
          key={chip}
          className="rounded-full bg-[var(--color-accent-soft)] px-2.5 py-0.5 text-[var(--color-ink)]"
        >
          {chip}
        </span>
      ))}
    </div>
  );
}

function Message({
  turn,
  onPickOption,
}: {
  turn: Turn;
  onPickOption: (text: string) => void;
}) {
  const [showTrace, setShowTrace] = useState(false);

  if (turn.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-[var(--color-accent)] px-4 py-2.5 text-sm text-white">
          {turn.content}
        </div>
      </div>
    );
  }

  const meta = turn.meta;

  return (
    <div className="flex justify-start">
      <div className="w-full max-w-[92%]">
        <Card className="px-4 py-3">
          <FormattedText text={turn.content} />
        </Card>

        {meta && meta.mode === "advise" && meta.suggested_options.length > 0 && (
          <OptionChips options={meta.suggested_options} onPick={onPickOption} />
        )}

        {meta && (
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
            {meta.mode === "advise" && (
              <span title="The agent is gathering what it needs before building the full plan. Say 'just plan it' to skip ahead.">
                <Badge tone="neutral">💬 shaping the trip</Badge>
              </span>
            )}
            {meta.needs_clarification && <Badge tone="warn">Asked a question</Badge>}
            {meta.status === "partial" && <Badge tone="warn">Partial answer</Badge>}
            {meta.destination && <Badge>{meta.destination}</Badge>}
            {meta.detected_language !== "en" && (
              <Badge>{meta.detected_language.toUpperCase()}</Badge>
            )}
            <span className="text-[var(--color-ink-soft)]">
              {(meta.latency_ms / 1000).toFixed(1)}s
            </span>
            {(meta.plan.length > 0 || meta.tool_calls.length > 0) && (
              <button
                type="button"
                onClick={() => setShowTrace((value) => !value)}
                className="text-[var(--color-accent)] underline underline-offset-2"
              >
                {showTrace ? "Hide" : "Show"} what it did
              </button>
            )}
          </div>
        )}

        {meta && showTrace && <Trace meta={meta} />}
      </div>
    </div>
  );
}

/**
 * Clickable destination/district options from the advisor's own retrieval
 * (real place names, not parsed out of the prose). Clicking one sends it
 * straight back as the next message, so picking a suggestion is one tap
 * instead of retyping a name back out of a paragraph.
 */
function OptionChips({
  options,
  onPick,
}: {
  options: string[];
  onPick: (text: string) => void;
}) {
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {options.map((option) => (
        <button
          key={option}
          type="button"
          onClick={() => onPick(`Let's do ${option}`)}
          className="group flex items-center gap-2 overflow-hidden rounded-full border border-[var(--color-line)] bg-[var(--color-surface)] py-1 pl-1 pr-3 text-xs transition-colors hover:border-[var(--color-accent)]"
        >
          <img
            src={placeImage(option, 32, 32)}
            alt=""
            aria-hidden
            loading="lazy"
            className="h-5 w-5 rounded-full object-cover"
          />
          {option}
        </button>
      ))}
    </div>
  );
}

function Trace({ meta }: { meta: ChatResponse }) {
  return (
    <Card className="mt-2 space-y-4 p-4 text-xs">
      {meta.plan.length > 0 && (
        <section>
          <h4 className="mb-2 font-semibold uppercase tracking-wide text-[var(--color-ink-soft)]">
            Plan
          </h4>
          <ol className="space-y-1">
            {meta.plan.map((step, index) => (
              <li key={index} className="flex gap-2">
                <span className="text-[var(--color-ink-soft)]">{index + 1}.</span>
                <span>{step.description}</span>
                <Badge>{step.kind}</Badge>
              </li>
            ))}
          </ol>
          {meta.replan_count > 0 && (
            <p className="mt-2 text-[var(--color-warn)]">
              Plan was revised {meta.replan_count}{" "}
              {meta.replan_count === 1 ? "time" : "times"} during the run.
            </p>
          )}
        </section>
      )}

      {meta.tool_calls.length > 0 && (
        <section>
          <h4 className="mb-2 font-semibold uppercase tracking-wide text-[var(--color-ink-soft)]">
            Tools used ({meta.tool_calls.length})
          </h4>
          <ul className="space-y-1">
            {meta.tool_calls.map((call, index) => (
              <li key={index} className="flex items-center justify-between gap-2">
                <span className="flex items-center gap-2">
                  <code className="font-mono">{call.tool}</code>
                  {call.status === "degraded" && (
                    <Badge tone="warn">source unavailable</Badge>
                  )}
                  {call.status === "invalid" && (
                    <span title="The model's first arguments didn't validate. The tool told it exactly what was wrong, and it corrected itself on the next call — this is the schema working as intended, not a failure.">
                      <Badge tone="neutral">self-corrected</Badge>
                    </span>
                  )}
                </span>
                <span className="text-[var(--color-ink-soft)]">
                  {call.source} · {call.latency_ms}ms
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <p className="border-t border-[var(--color-line)] pt-3 text-[var(--color-ink-soft)]">
        Run <code className="font-mono">{meta.run_id.slice(0, 8)}</code> ·{" "}
        {meta.steps_executed} steps executed
      </p>
    </Card>
  );
}

function Thinking() {
  return (
    <div className="flex items-center gap-2 px-1 text-sm text-[var(--color-ink-soft)]">
      <span className="flex gap-1" aria-hidden>
        {[0, 1, 2].map((index) => (
          <span
            key={index}
            className="typing-dot h-1.5 w-1.5 rounded-full bg-current"
            style={{ animationDelay: `${index * 0.16}s` }}
          />
        ))}
      </span>
      Thinking… quick answers take a few seconds, full plans up to a minute.
    </div>
  );
}
