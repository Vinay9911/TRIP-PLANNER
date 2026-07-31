"use client";

/**
 * The chat page.
 *
 * Choices worth noting.
 *
 * **A conversation is a URL.** `?session=<id>` selects a stored conversation
 * and its transcript is loaded from the server. Before this, sessions were
 * being persisted correctly but nothing could reach them - every visit
 * started blank and previous trips were effectively lost. Making it a query
 * parameter also means a conversation can be linked to and reloaded.
 *
 * **The trace stays.** Each reply can expand to show what the agent actually
 * did - the plan it wrote and every tool it called, with latencies. Most chat
 * interfaces hide this; showing it turns the documentation's claims into
 * something a reviewer can verify in the UI.
 *
 * **The service toggles are real.** The selection travels with every message,
 * and a switched-off service's tool is removed from the agent's toolbox on
 * the backend - the toggle is a guarantee, not a display filter.
 *
 * **Emoji appear only in the agent's own words.** Interface icons are SVG, so
 * they inherit colour and stroke weight and render identically everywhere.
 * Emoji inside a reply are content the model wrote, which is a different
 * thing from chrome.
 */

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { ItineraryView } from "@/components/Itinerary";
import { PlaceMap, type MappedItem } from "@/components/PlaceMap";
import { AuthGate } from "@/components/AuthGate";
import {
  IconBed,
  IconCalendar,
  IconChevron,
  IconClock,
  IconCompass,
  IconFork,
  IconInfo,
  IconPin,
  IconPlane,
  IconSend,
  IconSparkle,
  IconSun,
  IconTicket,
  IconUsers,
  IconWallet,
  SERVICE_ICONS,
} from "@/components/icons";
import {
  Badge,
  Button,
  Card,
  ErrorBanner,
  FormattedText,
  PlaceImage,
} from "@/components/ui";
import {
  ApiError,
  api,
  type ChatResponse,
  type FocusService,
  type ProgressEvent,
  type TripState,
} from "@/lib/api";

interface Turn {
  role: "user" | "assistant";
  content: string;
  meta?: ChatResponse;
}

const SERVICES: { id: FocusService; label: string }[] = [
  { id: "flights", label: "Flights" },
  { id: "attractions", label: "Attractions" },
  { id: "stays", label: "Stays" },
  { id: "restaurants", label: "Restaurants" },
];

/**
 * The service cards on the welcome screen. Each fills the composer with a
 * scoped opener the user finishes with a destination - discovery by doing
 * rather than a features page nobody reads. Tints echo the reference design.
 */
const CAPABILITIES = [
  {
    icon: IconCompass,
    title: "Build an itinerary",
    hint: "Tell it a place — it suggests, asks, then plans",
    prompt: "I want to go to ",
    tint: "bg-[var(--color-gold-soft)] text-[var(--color-gold)]",
    span: "sm:col-span-2 sm:row-span-2",
    art: "kyoto travel",
  },
  {
    icon: IconPlane,
    title: "Find flights",
    hint: "Compare routes and fares",
    prompt: "Find me flights to ",
    tint: "bg-[var(--color-sky-soft)] text-[var(--color-sky)]",
    span: "",
    art: "airplane sky",
  },
  {
    icon: IconBed,
    title: "Find a stay",
    hint: "Places that fit your budget",
    prompt: "Find me a place to stay in ",
    tint: "bg-[var(--color-grape-soft)] text-[var(--color-grape)]",
    span: "",
    art: "hotel room",
  },
  {
    icon: IconTicket,
    title: "See attractions",
    hint: "What's genuinely worth seeing",
    prompt: "What are the best things to see in ",
    tint: "bg-[var(--color-rose-soft)] text-[var(--color-rose)]",
    span: "",
    art: "landmark monument",
  },
  {
    icon: IconFork,
    title: "Eat well",
    hint: "Food that matches your needs",
    prompt: "Where should I eat in ",
    tint: "bg-[var(--color-mint-soft)] text-[var(--color-mint)]",
    span: "",
    art: "restaurant food",
  },
] as const;

const EXAMPLES = [
  "I want to go to Kerala",
  "Plan me 2 relaxed days in Kyoto. I'm vegetarian.",
  "Will I need an umbrella in Singapore next week?",
  "京都で2日間の旅程を立ててください",
];

export default function ChatPage() {
  return (
    <AuthGate>
      {(session) => (
        // useSearchParams needs a Suspense boundary during prerendering.
        <Suspense fallback={<div className="min-h-dvh" />}>
          <ChatRoute session={session} />
        </Suspense>
      )}
    </AuthGate>
  );
}

function ChatRoute({ session }: { session: { email: string | null; isAdmin: boolean } }) {
  const [historyToken, setHistoryToken] = useState(0);
  return (
    <AppShell session={session} historyToken={historyToken}>
      <Chat onConversationSaved={() => setHistoryToken((n) => n + 1)} />
    </AppShell>
  );
}

function Chat({ onConversationSaved }: { onConversationSaved: () => void }) {
  const searchParams = useSearchParams();
  const router = useRouter();
  const urlSession = searchParams.get("session");

  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [sessionId, setSessionId] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);
  const [loadingTranscript, setLoadingTranscript] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retryMessage, setRetryMessage] = useState<string | null>(null);
  const [focus, setFocus] = useState<FocusService[]>(SERVICES.map((s) => s.id));
  // Off by default: Groq is far faster, and the server falls back to the
  // local model by itself once the daily quota is gone. This switch is for
  // choosing local deliberately - offline, or to stop spending quota.
  const [localOnly, setLocalOnly] = useState(false);
  const [progress, setProgress] = useState<ProgressEvent[]>([]);
  const [trip, setTrip] = useState<TripState>({});
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const endRef = useRef<HTMLDivElement>(null);

  // Load (or clear) the transcript whenever the URL's session changes.
  useEffect(() => {
    let cancelled = false;

    async function load() {
      if (!urlSession) {
        setTurns([]);
        setSessionId(undefined);
        setTrip({});
        return;
      }
      setLoadingTranscript(true);
      try {
        const detail = await api.getSession(urlSession);
        if (cancelled) return;
        setSessionId(detail.session.id);
        setTurns(
          detail.messages
            .filter((m) => m.role === "user" || m.role === "assistant")
            .map((m) => ({ role: m.role as "user" | "assistant", content: m.content })),
        );
        setTrip(detail.session.destination ? { destination: detail.session.destination } : {});
      } catch {
        if (!cancelled) setError("That conversation could not be opened.");
      } finally {
        if (!cancelled) setLoadingTranscript(false);
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, [urlSession]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, busy]);

  function toggleService(id: FocusService) {
    setFocus((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      // Canonical order, so the array sent to the API is stable regardless of
      // the order the user clicked in.
      return SERVICES.map((s) => s.id).filter((s) => next.has(s));
    });
  }

  function insertPrompt(prompt: string) {
    setInput((current) => (current ? `${current.trimEnd()} ${prompt}` : prompt));
    inputRef.current?.focus();
  }

  const send = useCallback(
    async (message: string) => {
      const trimmed = message.trim();
      if (!trimmed || busy) return;

      setError(null);
      setRetryMessage(null);
      setInput("");
      setTurns((previous) => [...previous, { role: "user", content: trimmed }]);
      setBusy(true);
      setProgress([]);

      try {
        const reply = await api.chatStream(trimmed, {
          sessionId,
          focus,
          localOnly,
          onProgress: (event) =>
            setProgress((previous) => {
              // A stage replaces the headline; tools accumulate beneath it.
              // Keeping every stage would scroll the interesting part - the
              // thing happening *now* - off the top of a small panel.
              const next =
                event.kind === "stage"
                  ? previous.filter((e) => e.kind !== "stage")
                  : previous;
              return [...next, event].slice(-6);
            }),
        });
        setProgress([]);
        setSessionId(reply.session_id);
        setTrip(reply.trip_state ?? {});
        setTurns((previous) => [
          ...previous,
          { role: "assistant", content: reply.response, meta: reply },
        ]);
        onConversationSaved();
        // Put the (possibly brand-new) conversation in the URL so a reload or
        // a click in the sidebar lands back on it.
        if (reply.session_id !== urlSession) {
          router.replace(`/chat?session=${reply.session_id}`, { scroll: false });
        }
      } catch (caught) {
        setProgress([]);
        const text =
          caught instanceof ApiError
            ? caught.message
            : "Could not reach the planner. Please try again.";
        setError(text);
        // Kept separately from the input box so one-click Retry can resend it
        // verbatim even if the user has started typing something else.
        setRetryMessage(trimmed);
        setInput(trimmed);
        setTurns((previous) => previous.slice(0, -1));
      } finally {
        setBusy(false);
      }
    },
    [busy, focus, localOnly, sessionId, urlSession, router, onConversationSaved],
  );

  const empty = turns.length === 0 && !loadingTranscript;

  return (
    <div className="mx-auto flex min-h-dvh w-full max-w-3xl flex-col px-4 sm:px-6">
      <div className="flex-1 space-y-5 py-6">
        {loadingTranscript && (
          <div className="space-y-3">
            {[0, 1].map((n) => (
              <div key={n} className="skeleton h-20 rounded-2xl" />
            ))}
          </div>
        )}

        {empty && <Welcome onPick={(t) => void send(t)} onPrompt={insertPrompt} />}

        {turns.map((turn, index) => (
          <Message key={index} turn={turn} onPickOption={(t) => void send(t)} />
        ))}

        {busy && <Thinking progress={progress} />}
        {error && (
          <ErrorBanner
            message={error}
            onRetry={retryMessage ? () => void send(retryMessage) : undefined}
          />
        )}
        <div ref={endRef} />
      </div>

      <div className="sticky bottom-0 space-y-2.5 bg-gradient-to-t from-[var(--color-paper)] via-[var(--color-paper)] to-transparent pb-5 pt-3">
        <TripBar trip={trip} />

        <div className="flex flex-wrap items-center gap-2">
          <IncludeControl focus={focus} onToggle={toggleService} />
          <EngineControl localOnly={localOnly} onToggle={() => setLocalOnly((on) => !on)} />
        </div>
        <TripDetailsPicker onInsert={insertPrompt} />

        <form
          onSubmit={(event) => {
            event.preventDefault();
            void send(input);
          }}
          className="flex items-end gap-2 rounded-2xl border border-[var(--color-line-strong)] bg-[var(--color-surface)] p-2 shadow-[0_10px_30px_-24px_rgb(44_31_43_/_0.5)] focus-within:border-[var(--color-brand)]"
        >
          <label htmlFor="composer" className="sr-only">
            Your message
          </label>
          <textarea
            id="composer"
            ref={inputRef}
            value={input}
            rows={1}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              // Enter sends, Shift+Enter makes a new line - the convention
              // people already expect from every other chat interface.
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void send(input);
              }
            }}
            placeholder="Where would you like to go?"
            disabled={busy}
            className="max-h-40 min-h-11 flex-1 resize-none bg-transparent px-2 py-2.5 text-sm outline-none placeholder:text-[var(--color-ink-faint)] disabled:opacity-60"
          />
          <Button type="submit" disabled={busy || !input.trim()} aria-label="Send message">
            <IconSend size="1.1em" />
            <span className="hidden sm:inline">Send</span>
          </Button>
        </form>

        <p className="text-center text-[11px] text-[var(--color-ink-faint)]">
          Flight and hotel prices are simulated. Everything else is real data.
        </p>
      </div>
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
    <div className="rise-in py-4">
      <div className="flex items-center gap-2">
        <Badge tone="accent">
          <IconSparkle size="0.9em" />
          AI trip partner
        </Badge>
      </div>

      <h1 className="mt-3 font-display text-3xl font-semibold tracking-tight sm:text-4xl">
        Begin your next{" "}
        <span className="bg-gradient-to-r from-[var(--color-brand)] to-[var(--color-grape)] bg-clip-text text-transparent">
          adventure
        </span>
      </h1>
      <p className="mt-2 max-w-xl text-sm leading-relaxed text-[var(--color-ink-soft)]">
        Name a place and I&apos;ll suggest, ask the right questions, then build the
        plan — or jump straight to one specific thing.
      </p>

      <div className="mt-4 flex items-start gap-2.5 rounded-xl border border-[var(--color-line)] bg-[var(--color-brand-soft)]/60 px-4 py-3 text-sm">
        <span className="mt-0.5 shrink-0 text-[var(--color-brand-strong)]">
          <IconInfo size="1.1em" />
        </span>
        <p className="leading-relaxed">
          <strong className="font-semibold">What helps most:</strong> where you&apos;re
          going, how many days, roughly when, who&apos;s coming, and any must-haves
          (diet, budget, pace). Give what you know — I&apos;ll ask about anything
          important that&apos;s missing.
        </p>
      </div>

      <div className="mt-5 grid auto-rows-[minmax(0,1fr)] gap-3 sm:grid-cols-4">
        {CAPABILITIES.map((capability) => {
          const Glyph = capability.icon;
          const big = capability.span !== "";
          return (
            <button
              key={capability.title}
              type="button"
              onClick={() => onPrompt(capability.prompt)}
              className={`group relative overflow-hidden rounded-2xl border border-[var(--color-line)] bg-[var(--color-surface)] p-4 text-left transition-[border-color,transform] duration-200 hover:border-[var(--color-brand)] hover:-translate-y-0.5 ${capability.span}`}
            >
              <span
                className={`grid h-10 w-10 place-items-center rounded-xl ${capability.tint}`}
              >
                <Glyph />
              </span>
              <span className="mt-3 block font-display text-sm font-semibold">
                {capability.title}
              </span>
              <span className="mt-0.5 block text-xs leading-relaxed text-[var(--color-ink-soft)]">
                {capability.hint}
              </span>
              {big && (
                <PlaceImage
                  name={capability.art}
                  width={420}
                  height={260}
                  className="pointer-events-none mt-4 h-28 w-full rounded-xl opacity-90 transition-transform duration-300 group-hover:scale-[1.02] sm:h-36"
                />
              )}
            </button>
          );
        })}
      </div>

      <p className="mt-6 text-[11px] font-semibold uppercase tracking-wide text-[var(--color-ink-faint)]">
        Or try one of these
      </p>
      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            onClick={() => onPick(example)}
            className="flex min-h-11 items-center justify-between gap-2 rounded-xl border border-[var(--color-line)] bg-[var(--color-surface)] px-3.5 py-2.5 text-left text-sm transition-colors duration-200 hover:border-[var(--color-brand)]"
          >
            <span>{example}</span>
            <span className="shrink-0 text-[var(--color-ink-faint)]">
              <IconChevron size="0.95em" />
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

/**
 * The slot ledger, rendered. Empty slots simply do not appear, so the bar
 * grows as the conversation fills the trip in - a progress indicator that is
 * also an honesty check on what the agent claims to know.
 */
function TripBar({ trip }: { trip: TripState }) {
  const chips: { icon: React.ReactNode; text: string }[] = [];
  if (trip.destination) chips.push({ icon: <IconPin size="0.95em" />, text: trip.destination });
  if (trip.duration_days)
    chips.push({ icon: <IconCalendar size="0.95em" />, text: `${trip.duration_days} days` });
  if (trip.start_date)
    chips.push({
      icon: <IconClock size="0.95em" />,
      text: `${trip.start_date}${trip.end_date ? ` → ${trip.end_date}` : ""}`,
    });
  else if (trip.travel_window)
    chips.push({ icon: <IconClock size="0.95em" />, text: trip.travel_window });
  if (trip.origin) chips.push({ icon: <IconPlane size="0.95em" />, text: `from ${trip.origin}` });
  if (trip.party) chips.push({ icon: <IconUsers size="0.95em" />, text: trip.party });
  if (trip.budget_tier)
    chips.push({ icon: <IconWallet size="0.95em" />, text: trip.budget_tier });
  if (trip.priorities?.length)
    chips.push({ icon: <IconSparkle size="0.95em" />, text: trip.priorities.join(", ") });

  if (chips.length === 0) return null;

  return (
    <div className="flex flex-wrap items-center gap-1.5 rounded-xl border border-[var(--color-line)] bg-[var(--color-surface)]/80 px-3 py-2 text-xs backdrop-blur">
      {trip.destination && (
        <PlaceImage
          name={trip.destination}
          width={48}
          height={48}
          className="h-6 w-6 rounded-full"
        />
      )}
      <span className="font-medium text-[var(--color-ink-soft)]">Your trip</span>
      {chips.map((chip) => (
        <span
          key={chip.text}
          className="inline-flex items-center gap-1 rounded-full bg-[var(--color-brand-soft)] px-2.5 py-1 text-[var(--color-brand-strong)]"
        >
          {chip.icon}
          {chip.text}
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
      <div className="rise-in flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-[var(--color-brand-strong)] px-4 py-2.5 text-sm text-white">
          {turn.content}
        </div>
      </div>
    );
  }

  const meta = turn.meta;

  return (
    <div className="rise-in flex justify-start">
      <div className="w-full max-w-[94%]">
        {meta?.itinerary ? (
          <ItineraryView itinerary={meta.itinerary} />
        ) : (
          <Card className="px-4 py-3.5">
            <FormattedText text={turn.content} />
          </Card>
        )}

        {meta && meta.mode === "advise" && meta.suggested_options.length > 0 && (
          <OptionGallery
            options={meta.suggested_options}
            places={(meta.suggested_places ?? []) as {
              name: string;
              latitude: number;
              longitude: number;
            }[]}
            onPick={onPickOption}
          />
        )}

        {meta && meta.suggested_actions.length > 0 && (
          <ActionChips actions={meta.suggested_actions} onPick={onPickOption} />
        )}

        {meta && (
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
            {meta.mode === "advise" && (
              <span title="The agent is gathering what it needs before building the full plan. Say 'just plan it' to skip ahead.">
                <Badge tone="grape">shaping the trip</Badge>
              </span>
            )}
            {meta.needs_clarification && <Badge tone="warn">asked a question</Badge>}
            {meta.status === "partial" && <Badge tone="warn">partial answer</Badge>}
            {meta.destination && <Badge tone="accent">{meta.destination}</Badge>}
            {meta.detected_language !== "en" && (
              <Badge>{meta.detected_language.toUpperCase()}</Badge>
            )}
            {/* Which brain answered. Worth a badge because it is the single
                best explanation for why one reply took a second and the next
                took a minute - and "both" is a real state, when the cloud
                quota ran out part-way through the turn. */}
            {meta.llm_providers?.length > 0 && (
              <span
                title={
                  meta.llm_providers.includes("local")
                    ? "Served by llama3.2 running on this machine."
                    : "Served by Groq."
                }
              >
                <Badge tone={meta.llm_providers.includes("local") ? "grape" : undefined}>
                  {meta.llm_providers.includes("local") &&
                  meta.llm_providers.includes("groq")
                    ? "groq → local"
                    : meta.llm_providers[0]}
                </Badge>
              </span>
            )}
            <span className="text-[var(--color-ink-faint)]">
              {(meta.latency_ms / 1000).toFixed(1)}s
            </span>
            {(meta.plan.length > 0 || meta.tool_calls.length > 0) && (
              <button
                type="button"
                onClick={() => setShowTrace((value) => !value)}
                aria-expanded={showTrace}
                className="font-medium text-[var(--color-brand-strong)] underline underline-offset-2"
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
 * Which services the agent may use, as one compact control.
 *
 * This replaced a permanent row of four always-on toggles, for reasons worth
 * recording. All four defaulted to on, so in normal use they did nothing
 * while occupying a full row directly above the composer. Worse, they looked
 * uniform but were not: switching off Flights or Stays genuinely removes that
 * tool from the model's toolbox, whereas Attractions and Restaurants share
 * `find_places` with everything else and can only be enforced by instruction.
 * Presenting a strong guarantee and a weak one as identical switches is the
 * kind of small dishonesty that erodes trust in everything around it.
 *
 * So the row collapsed to a single button that states the current selection,
 * opening a popover only when someone actually wants to exclude something -
 * which is rare, and is the only case where these have any effect at all.
 * The two enforcement strengths are labelled where they are chosen.
 *
 * The everyday path for "I just want flights" is not this control at all: it
 * is the quick-action chips and the follow-up offers, which are phrased as
 * things to do rather than capabilities to switch off.
 */
/**
 * Which brain is answering, and a switch to force the local one.
 *
 * This exists because waiting is only tolerable when you know what you are
 * waiting for. A reply served by a 3B model on a laptop GPU is genuinely
 * slower than one from Groq, and without saying so the interface just looks
 * broken - the single most common complaint about this app so far.
 *
 * Off by default, deliberately. Groq answers in about a second and the server
 * already falls back to local on its own when the daily quota is spent, so
 * the honest default is "use the fast one until it runs out". The switch is
 * for choosing local on purpose: offline, or to stop burning quota while
 * testing.
 */
function EngineControl({
  localOnly,
  onToggle,
}: {
  localOnly: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={localOnly}
      title={
        localOnly
          ? "Running on llama3.2 on this machine. Slower, no quota, nothing leaves your computer."
          : "Running on Groq. Switches to the local model automatically if the daily quota runs out."
      }
      className={`inline-flex min-h-9 items-center gap-1.5 rounded-full border px-3 text-xs font-medium transition-colors duration-200 ${
        localOnly
          ? "border-[var(--color-grape)]/40 bg-[var(--color-grape-soft)] text-[var(--color-grape)]"
          : "border-[var(--color-line-strong)] bg-[var(--color-surface)] text-[var(--color-ink-soft)] hover:border-[var(--color-brand)]"
      }`}
    >
      <span
        aria-hidden
        className={`size-1.5 rounded-full ${
          localOnly ? "bg-[var(--color-grape)]" : "bg-[var(--color-mint)]"
        }`}
      />
      {localOnly ? "Local model" : "Groq (fast)"}
    </button>
  );
}

function IncludeControl({
  focus,
  onToggle,
}: {
  focus: FocusService[];
  onToggle: (id: FocusService) => void;
}) {
  const [open, setOpen] = useState(false);
  const all = focus.length === SERVICES.length;
  const excluded = SERVICES.filter((service) => !focus.includes(service.id));

  return (
    <div className="relative flex items-center gap-2">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className={`inline-flex min-h-9 items-center gap-1.5 rounded-full border px-3 text-xs transition-colors duration-200 ${
          all
            ? "border-[var(--color-line-strong)] bg-[var(--color-surface)] text-[var(--color-ink-soft)]"
            : "border-[var(--color-brand)] bg-[var(--color-brand-soft)] font-medium text-[var(--color-brand-strong)]"
        }`}
      >
        <IconSparkle size="0.95em" />
        {all ? "Including everything" : `Excluding ${excluded.length}`}
        <IconChevron
          size="0.8em"
          className={`transition-transform duration-200 ${open ? "rotate-90" : ""}`}
        />
      </button>

      {!all && (
        <span className="text-[11px] text-[var(--color-ink-faint)]">
          {excluded.map((service) => service.label).join(", ")} left out
        </span>
      )}

      {open && (
        <>
          {/* Click-away layer. Rendered behind the panel so a click anywhere
              else closes it without stealing the first click on a checkbox. */}
          <button
            type="button"
            aria-label="Close"
            onClick={() => setOpen(false)}
            className="fixed inset-0 z-20 cursor-default"
          />
          <div className="absolute bottom-full left-0 z-30 mb-2 w-64 rounded-2xl border border-[var(--color-line)] bg-[var(--color-surface)] p-3 shadow-[0_20px_50px_-24px_rgb(44_31_43_/_0.5)]">
            <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--color-ink-faint)]">
              Include in plans
            </p>
            <ul className="space-y-0.5">
              {SERVICES.map((service) => {
                const on = focus.includes(service.id);
                const Glyph = SERVICE_ICONS[service.id];
                const enforced = service.id === "flights" || service.id === "stays";
                return (
                  <li key={service.id}>
                    <button
                      type="button"
                      onClick={() => onToggle(service.id)}
                      aria-pressed={on}
                      className="flex min-h-10 w-full items-center gap-2.5 rounded-xl px-2 text-left text-xs transition-colors duration-200 hover:bg-[var(--color-surface-2)]"
                    >
                      <span
                        className={`grid h-4 w-4 shrink-0 place-items-center rounded border text-[9px] ${
                          on
                            ? "border-[var(--color-brand-strong)] bg-[var(--color-brand-strong)] text-white"
                            : "border-[var(--color-line-strong)]"
                        }`}
                        aria-hidden
                      >
                        {on ? "✓" : ""}
                      </span>
                      <Glyph size="1em" />
                      <span className="flex-1">{service.label}</span>
                      <span className="text-[9px] text-[var(--color-ink-faint)]">
                        {enforced ? "enforced" : "guided"}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
            <p className="mt-2 border-t border-[var(--color-line)] pt-2 text-[10px] leading-relaxed text-[var(--color-ink-faint)]">
              <strong>Enforced</strong> removes the tool entirely.{" "}
              <strong>Guided</strong> shares a tool with the rest of planning, so
              it is asked for rather than withheld.
            </p>
          </div>
        </>
      )}
    </div>
  );
}


/**
 * "Want me to also…" offers under a reply.
 *
 * A live five-day Geneva plan never once mentioned that the agent could also
 * find flights, a hotel or a restaurant - all three tools were available and
 * the traveller had no way to know. These are that missing prompt.
 *
 * The list is built on the server from what is known and what has not run
 * yet, so it can never offer a service the traveller switched off or one it
 * has just delivered.
 */
function ActionChips({
  actions,
  onPick,
}: {
  actions: { label: string; message: string }[];
  onPick: (message: string) => void;
}) {
  return (
    <div className="mt-2.5 flex flex-wrap items-center gap-2">
      <span className="text-[11px] text-[var(--color-ink-faint)]">Want me to also</span>
      {actions.map((action) => (
        <button
          key={action.label}
          type="button"
          onClick={() => onPick(action.message)}
          className="inline-flex min-h-9 items-center gap-1.5 rounded-full border border-[var(--color-brand)]/40 bg-[var(--color-brand-soft)] px-3 text-xs font-medium text-[var(--color-brand-strong)] transition-colors duration-200 hover:border-[var(--color-brand)]"
        >
          {action.label}
          <IconChevron size="0.85em" />
        </button>
      ))}
    </div>
  );
}

/**
 * Clickable destination options from the advisor's own retrieval - real place
 * names taken from the guide corpus, not parsed out of the prose. Clicking
 * one sends it as the next message, so picking a suggestion is a tap instead
 * of retyping a name out of a paragraph.
 */
/**
 * The places an advisory turn is offering, as pictures and pins.
 *
 * **Why this is not a row of chips.** Photographs and a map were wired only
 * to the finished itinerary, which is the rarest thing this agent produces -
 * most conversations are two or three advisory turns that never reach a full
 * plan. So both features existed and almost nobody saw them, and the most
 * frequent complaint about the app was that it had neither. It had both,
 * behind the wrong door.
 *
 * A name alone also asks too much. "The Adirondacks, the Catskills, the Hudson
 * Valley" only helps someone who already knows where those are relative to
 * one another - which is precisely what a traveller choosing between them does
 * not know yet. Three pins and three photographs answer it without a sentence.
 *
 * The whole card is the button. Picking one of several offered options is the
 * single most likely next action in this gear, so it gets the largest possible
 * target rather than a chip-sized one.
 */
function OptionGallery({
  options,
  places,
  onPick,
}: {
  options: string[];
  places: { name: string; latitude: number; longitude: number }[];
  onPick: (text: string) => void;
}) {
  const [showMap, setShowMap] = useState(true);
  const byName = new Map(places.map((place) => [place.name, place]));

  // Only pinned options are numbered - a number beside a card with no pin
  // would point at nothing on the map.
  const pinned = options.filter((option) => byName.has(option));
  const numberOf = new Map(pinned.map((name, index) => [name, index + 1]));

  const mapped: MappedItem[] = pinned.map((name, index) => ({
    name,
    kind: "neighbourhood",
    district: null,
    description: "",
    latitude: byName.get(name)!.latitude,
    longitude: byName.get(name)!.longitude,
    approx_duration: null,
    booking_note: null,
    index: index + 1,
    dayNumber: 1,
  }));

  return (
    <div className="mt-3 space-y-2.5">
      {mapped.length > 1 && (
        <>
          <button
            type="button"
            onClick={() => setShowMap((value) => !value)}
            aria-expanded={showMap}
            className="inline-flex min-h-9 items-center gap-1.5 rounded-full border border-[var(--color-brand)]/40 bg-[var(--color-brand-soft)] px-3 text-xs font-medium text-[var(--color-brand-strong)] transition-colors duration-200 hover:border-[var(--color-brand)]"
          >
            <IconPin size="0.95em" />
            {showMap ? "Hide map" : `Show these on a map (${mapped.length})`}
          </button>

          <div
            className={`grid transition-[grid-template-rows,opacity] duration-300 ease-out ${
              showMap ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0"
            }`}
          >
            <div className="overflow-hidden">
              {showMap && <PlaceMap items={mapped} className="h-56 sm:h-64" />}
            </div>
          </div>
        </>
      )}

      <div className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
        {options.map((option) => (
          <button
            key={option}
            type="button"
            onClick={() => onPick(`Let's do ${option}`)}
            className="group overflow-hidden rounded-2xl border border-[var(--color-line-strong)] bg-[var(--color-surface)] text-left transition-[border-color,transform,box-shadow] duration-200 hover:-translate-y-0.5 hover:border-[var(--color-brand)] hover:shadow-[0_12px_28px_-20px_rgb(44_31_43_/_0.55)]"
          >
            <div className="relative h-24 w-full overflow-hidden">
              <PlaceImage
                name={option}
                width={320}
                height={200}
                className="h-24 w-full object-cover transition-transform duration-500 group-hover:scale-105"
              />
              {numberOf.has(option) && (
                <span className="absolute left-2 top-2 grid size-5 place-items-center rounded-full bg-[var(--color-brand-strong)] text-[10px] font-semibold text-white">
                  {numberOf.get(option)}
                </span>
              )}
            </div>
            <span className="flex items-center justify-between gap-2 px-3 py-2 text-xs font-medium">
              {option}
              <IconChevron size="0.9em" className="-rotate-90 opacity-40" />
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}


function Trace({ meta }: { meta: ChatResponse }) {
  return (
    <Card className="mt-2 space-y-4 p-4 text-xs">
      {meta.plan.length > 0 && (
        <section>
          <h4 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--color-ink-faint)]">
            Plan
          </h4>
          <ol className="space-y-1.5">
            {meta.plan.map((step, index) => (
              <li key={index} className="flex gap-2">
                <span className="tabular-nums text-[var(--color-ink-faint)]">{index + 1}.</span>
                <span className="flex-1">{step.description}</span>
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
          <h4 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--color-ink-faint)]">
            Tools used ({meta.tool_calls.length})
          </h4>
          <ul className="space-y-1.5">
            {meta.tool_calls.map((call, index) => (
              <li key={index} className="flex items-center justify-between gap-2">
                <span className="flex items-center gap-2">
                  <code className="font-mono">{call.tool}</code>
                  {call.status === "degraded" && <Badge tone="warn">source unavailable</Badge>}
                  {call.status === "invalid" && (
                    <span title="The model's first arguments didn't validate. The tool told it exactly what was wrong, and it corrected itself on the next call — the schema working as intended, not a failure.">
                      <Badge>self-corrected</Badge>
                    </span>
                  )}
                </span>
                <span className="tabular-nums text-[var(--color-ink-faint)]">
                  {call.source} · {call.latency_ms}ms
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <p className="border-t border-[var(--color-line)] pt-3 text-[var(--color-ink-faint)]">
        Run <code className="font-mono">{meta.run_id.slice(0, 8)}</code> ·{" "}
        {meta.steps_executed} steps · {meta.total_tokens.toLocaleString()} tokens
      </p>
    </Card>
  );
}

/**
 * A clickable way to add dates and trip length without typing them out.
 * Deliberately simple - native date inputs and a stepper, not a bespoke
 * calendar - because the ask was "make it clickable", not "build a date
 * picker" for something used a handful of times per conversation.
 */
function TripDetailsPicker({ onInsert }: { onInsert: (text: string) => void }) {
  const [open, setOpen] = useState(false);
  const [days, setDays] = useState(0);
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");

  const canInsert = days > 0 || startDate !== "";

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
        className="inline-flex min-h-9 items-center gap-1.5 text-xs font-medium text-[var(--color-brand-strong)] underline underline-offset-2"
      >
        <IconCalendar size="1em" />
        Add dates or trip length
      </button>
    );
  }

  return (
    <div className="flex flex-wrap items-end gap-3 rounded-xl border border-[var(--color-line)] bg-[var(--color-surface)] p-3 text-xs">
      <div>
        <span className="mb-1 block text-[var(--color-ink-soft)]">Days</span>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setDays((v) => Math.max(0, v - 1))}
            className="h-9 w-9 rounded-lg border border-[var(--color-line-strong)] font-medium"
            aria-label="Fewer days"
          >
            −
          </button>
          <span className="w-7 text-center tabular-nums" aria-live="polite">
            {days}
          </span>
          <button
            type="button"
            onClick={() => setDays((v) => Math.min(90, v + 1))}
            className="h-9 w-9 rounded-lg border border-[var(--color-line-strong)] font-medium"
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
          onChange={(e) => setStartDate(e.target.value)}
          className="min-h-9 rounded-lg border border-[var(--color-line-strong)] bg-[var(--color-surface)] px-2"
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
          onChange={(e) => setEndDate(e.target.value)}
          className="min-h-9 rounded-lg border border-[var(--color-line-strong)] bg-[var(--color-surface)] px-2"
        />
      </div>
      <Button variant="secondary" onClick={insert} disabled={!canInsert}>
        Add to message
      </Button>
      <button
        type="button"
        onClick={() => setOpen(false)}
        className="min-h-9 px-1 text-[var(--color-ink-soft)] underline underline-offset-2"
      >
        Cancel
      </button>
    </div>
  );
}

/**
 * What the agent is doing, right now.
 *
 * This replaced a timer that advanced through four invented stages every
 * seven seconds. That was honest about being an approximation, but it was
 * still guessing - and it guessed badly on the runs that most needed
 * explaining, sitting on "Writing it up" for three minutes while the executor
 * was actually stuck re-searching hotels.
 *
 * The events are real now, streamed from the graph as each node completes and
 * each tool returns. The headline is the current stage; finished tool calls
 * accumulate underneath, most recent last, capped so the panel cannot grow
 * without bound on a long plan.
 *
 * The elapsed counter matters more than it looks. A plan legitimately takes
 * a minute or two, and a number that keeps moving is the difference between
 * "this is working" and "this has frozen" - which is what people actually
 * reported before any of this existed.
 */
function Thinking({ progress }: { progress: ProgressEvent[] }) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const id = setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const stage = progress.find((event) => event.kind === "stage");
  const tools = progress.filter((event) => event.kind === "tool");

  return (
    <div className="rise-in space-y-2 rounded-2xl border border-[var(--color-line)] bg-[var(--color-surface)] px-4 py-3">
      <div className="flex items-center gap-3">
        <span className="flex gap-1" aria-hidden>
          {[0, 1, 2].map((index) => (
            <span
              key={index}
              className="typing-dot h-1.5 w-1.5 rounded-full bg-[var(--color-brand)]"
              style={{ animationDelay: `${index * 0.16}s` }}
            />
          ))}
        </span>
        <span
          className="text-sm text-[var(--color-ink-soft)]"
          aria-live="polite"
          aria-atomic="true"
        >
          {stage?.message ?? "Getting started"}
          {stage?.detail && (
            <span className="text-[var(--color-ink-faint)]"> · {stage.detail}</span>
          )}
          …
        </span>
        <span className="ml-auto tabular-nums text-[11px] text-[var(--color-ink-faint)]">
          {elapsed}s
        </span>
      </div>

      {tools.length > 0 && (
        <ul className="space-y-0.5 border-t border-[var(--color-line)] pt-2">
          {tools.map((tool, index) => (
            <li
              key={`${tool.message}-${index}`}
              className="rise-in flex items-center gap-1.5 text-[11px] text-[var(--color-ink-faint)]"
            >
              <span className="text-[var(--color-mint)]" aria-hidden>
                ✓
              </span>
              {tool.message}
              {tool.detail && <span className="text-[var(--color-warn)]">· {tool.detail}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
