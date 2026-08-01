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
import { PlacePhoto } from "@/components/PlacePhoto";
import { TripPanel, TripPanelDrawer } from "@/components/TripPanel";
import { AuthGate } from "@/components/AuthGate";
import {
  IconBed,
  IconCalendar,
  IconChevron,
  IconClock,
  IconCompass,
  IconFork,
  IconInfo,
  IconMic,
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
  DecorativeArt,
} from "@/components/ui";
import {
  ApiError,
  api,
  type ChatResponse,
  type FocusService,
  type ProgressEvent,
  type TripFact,
  type StoredMessage,
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
    hint: "Tell it a place — it suggests, asks, then plan your perfect days.",
    prompt: "I want to go to ",
    tint: "bg-[var(--color-gold-soft)] text-[var(--color-gold)]",
    span: "sm:col-span-2 sm:row-span-2",
    image: "/images/build_itinerary.png",
  },
  {
    icon: IconPlane,
    title: "Find flights",
    hint: "Compare routes and fares",
    prompt: "Find me flights to ",
    tint: "bg-[var(--color-sky-soft)] text-[var(--color-sky)]",
    span: "",
    image: "/images/find_flights.png",
  },
  {
    icon: IconBed,
    title: "Find a stay",
    hint: "Places that fit your budget",
    prompt: "Find me a place to stay in ",
    tint: "bg-[var(--color-grape-soft)] text-[var(--color-grape)]",
    span: "",
    image: "/images/find_stay.png",
  },
  {
    icon: IconTicket,
    title: "See attractions",
    hint: "What's genuinely worth seeing",
    prompt: "What are the best things to see in ",
    tint: "bg-[var(--color-rose-soft)] text-[var(--color-rose)]",
    span: "",
    image: "/images/see_attractions.png",
  },
  {
    icon: IconFork,
    title: "Eat well",
    hint: "Food that matches your needs",
    prompt: "Where should I eat in ",
    tint: "bg-[var(--color-mint-soft)] text-[var(--color-mint)]",
    span: "",
    image: "/images/eat_well.png",
  },
] as const;

const EXAMPLES = [
  { text: "I want to go to Kerala", icon: "🌴" },
  { text: "Plan me 2 relaxed days in Kyoto. I'm vegetarian.", icon: "⛩️" },
  { text: "Will I need an umbrella in Singapore next week?", icon: "☔" },
  { text: "मुझे 3 दिन का गोवा का प्लान बनाओ (Hindi — try any language!)", icon: "🗺️" },
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

/**
 * Rebuild a reply's renderable payload from what was stored with it.
 *
 * A conversation reopened from history used to be plain text: the itinerary,
 * the map, the photographs and the clickable options all lived on the live
 * API response and nothing but the string was persisted. The reply the
 * traveller saw yesterday came back today as a wall of prose.
 *
 * Only the fields that drive rendering are reconstructed. The trace - the
 * plan, the tool calls, the timings - belongs to the run record and is not
 * duplicated onto the message, so "Show what it did" is offered on live turns
 * only. That is an honest limit rather than a gap: those numbers describe an
 * execution, and a reopened transcript is not one.
 *
 * Returns undefined for anything with no stored payload - user messages, and
 * assistant messages written before this was persisted - which the renderer
 * already handles as "text only".
 */
function rehydrate(message: StoredMessage): ChatResponse | undefined {
  const stored = message.metadata;
  if (message.role !== "assistant" || !stored || Object.keys(stored).length === 0) {
    return undefined;
  }

  return {
    session_id: "",
    run_id: "",
    response: message.content,
    status: "completed",
    mode: (stored.mode as ChatResponse["mode"]) ?? "plan",
    trip_state: {},
    suggested_options: (stored.suggested_options as string[]) ?? [],
    suggested_places: (stored.suggested_places as ChatResponse["suggested_places"]) ?? [],
    trip_facts: (stored.trip_facts as ChatResponse["trip_facts"]) ?? {},
    itinerary: (stored.itinerary as ChatResponse["itinerary"]) ?? null,
    suggested_actions: (stored.suggested_actions as ChatResponse["suggested_actions"]) ?? [],
    needs_clarification: false,
    detected_language: message.language ?? "en",
    destination: (stored.destination as string | null) ?? null,
    llm_providers: [],
    plan: [],
    tool_calls: [],
    steps_executed: 0,
    replan_count: 0,
    latency_ms: 0,
    total_tokens: 0,
  };
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
  // Carried forward rather than read off the newest reply. Asking about
  // hotels and then about the weather must not blank the hotels: the panel
  // shows the trip's current state, which is cumulative even though each
  // individual reply is not.
  const [facts, setFacts] = useState<{
    weather?: TripFact;
    stays?: TripFact;
    flights?: TripFact;
  }>({});
  const [pins, setPins] = useState<MappedItem[]>([]);
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
        setFacts({});
        setPins([]);
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
            .map((m) => ({
              role: m.role as "user" | "assistant",
              content: m.content,
              // Rebuilt from what the reply stored. Without this, reopening a
              // conversation dropped the map, the photographs and the
              // clickable options and showed a wall of text - the metadata
              // only ever existed on the live response and died with it.
              meta: rehydrate(m),
            })),
        );
        setTrip(detail.session.destination ? { destination: detail.session.destination } : {});

        // Replay the stored replies so a reopened conversation arrives with
        // its map and panels intact rather than rebuilding only on the next
        // message.
        setFacts({});
        setPins([]);
        for (const message of detail.messages) {
          const restored = rehydrate(message);
          if (restored) absorb(restored);
        }
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

  /**
   * Fold a reply into the panel's cumulative view.
   *
   * Only overwrites a panel when this turn actually produced something for
   * it, so a weather question leaves the hotels where they were.
   */
  function absorb(reply: ChatResponse) {
    const next = reply.trip_facts ?? {};
    setFacts((previous) => ({
      weather: next.weather ?? previous.weather,
      stays: next.stays ?? previous.stays,
      flights: next.flights ?? previous.flights,
    }));

    const fromItinerary: MappedItem[] = [];
    let index = 0;
    for (const day of reply.itinerary?.days ?? []) {
      for (const item of [...day.all_day, ...day.morning, ...day.afternoon, ...day.evening]) {
        if (item.latitude != null && item.longitude != null) {
          fromItinerary.push({ ...item, index: ++index, dayNumber: day.day_number });
        }
      }
    }

    const fromOptions: MappedItem[] = (reply.suggested_places ?? []).map((place, position) => ({
      name: place.name,
      kind: "neighbourhood" as const,
      district: null,
      description: "",
      latitude: place.latitude,
      longitude: place.longitude,
      approx_duration: null,
      booking_note: null,
      index: position + 1,
      dayNumber: 1,
    }));

    // An itinerary supersedes the options that led to it - showing both would
    // pin the same city twice, once as a suggestion and once as a stop.
    if (fromItinerary.length > 0) setPins(fromItinerary);
    else if (fromOptions.length > 0) setPins(fromOptions);
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
        absorb(reply);
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
    <div className="mx-auto grid w-full max-w-[100rem] grid-cols-1 gap-6 px-4 sm:px-6 lg:grid-cols-[minmax(0,1fr)_22rem] xl:grid-cols-[minmax(0,1fr)_25rem]">
      <div className="flex min-h-dvh flex-col">
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

      {/* One row of controls, not four stacked ones.
          `TripBar` used to repeat the destination and dates here; the trip
          panel now says all of that and does not scroll, so keeping it was
          duplicating the answer directly above the question. What is left is
          only the things that change what the *next* message does. On mobile
          the drawer stands in for the panel, so it stays. */}
      <div className="sticky bottom-0 space-y-2 bg-gradient-to-t from-[var(--color-paper)] via-[var(--color-paper)] to-transparent pb-4 pt-3">
        <TripPanelDrawer
          trip={trip}
          facts={facts}
          pins={pins}
          busy={busy}
          onAsk={(message) => void send(message)}
        />

        <div className="flex flex-wrap items-center gap-1.5">
          <EngineControl localOnly={localOnly} onToggle={() => setLocalOnly((on) => !on)} />
          <TripDetailsPicker onInsert={insertPrompt} />
        </div>

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
          <VoiceInput
            onTranscript={(text) => {
              setInput((prev) => (prev ? `${prev.trimEnd()} ${text}` : text));
              inputRef.current?.focus();
            }}
            disabled={busy}
          />
          <button 
            type="submit" 
            disabled={busy || !input.trim()} 
            aria-label="Send message"
            className="flex h-11 items-center gap-2 rounded-xl bg-gradient-to-r from-[var(--color-brand)] to-[var(--color-brand-strong)] px-4 font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            <IconSend size="1.2em" />
            <span className="hidden sm:inline">Send</span>
          </button>
        </form>

        <p className="text-center text-[11px] text-[var(--color-ink-faint)]">
          ⚠️ Flight and hotel prices are <strong>simulated for demo purposes</strong>. Everything else is real data.
        </p>
      </div>
      </div>

      {/* Sticky rather than scrolling with the transcript: this describes the
          state of the trip, not a moment in the conversation, so it should
          still be there after three more messages. `top` clears the shell's
          header; the panel scrolls internally when it outgrows the viewport. */}
      <aside className="hidden lg:block">
        <div className="sticky top-4 max-h-[calc(100dvh-2rem)] overflow-y-auto py-6 pr-1">
          <TripPanel
            trip={trip}
            facts={facts}
            pins={pins}
            busy={busy}
            onAsk={(message) => void send(message)}
          />
        </div>
      </aside>
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
      {/* Banner & Header Section */}
      <div className="relative mb-3 flex flex-col md:flex-row justify-between rounded-2xl bg-[var(--color-surface-2)] p-4 md:p-6 overflow-hidden isolate">
        <div className="z-10 max-w-xl md:mr-[200px]">
          <div className="flex items-center gap-2 mb-2">
            <span className="text-lg">👋</span>
            <span className="text-xs font-medium text-[var(--color-ink-soft)]">
              Hi Vinay, ready for your next adventure?
            </span>
          </div>

          <h1 className="mt-1 font-display text-2xl font-semibold tracking-tight sm:text-3xl text-[var(--color-ink)]">
            Let's plan your{" "}
            <span className="text-[var(--color-brand)]">
              perfect trip ✦
            </span>
          </h1>
          <p className="mt-2 text-xs leading-relaxed text-[var(--color-ink-soft)] max-w-sm">
            Tell me a place and I'll suggest, ask the right questions, then build the
            plan — or jump straight to one specific thing.
          </p>
        </div>
        {/* Absolute positioned background image covering the right side */}
        <div className="hidden md:block absolute right-0 top-0 bottom-0 w-[50%] opacity-80 -z-10 mask-image-to-l">
           <img src="/images/header_banner.png" alt="Tropical resort" className="w-full h-full object-cover rounded-r-2xl mask-fade-l" style={{ WebkitMaskImage: 'linear-gradient(to right, transparent 0%, black 30%)' }} />
        </div>
      </div>

      <div className="mt-2 flex items-start gap-2.5 rounded-xl border border-[var(--color-line)] bg-gradient-to-r from-[var(--color-surface)] to-[var(--color-surface-2)] px-4 py-2.5 text-xs shadow-sm">
        <span className="shrink-0 grid h-6 w-6 place-items-center rounded-full bg-[var(--color-brand-soft)] text-[var(--color-brand-strong)]">
          <IconSparkle size="0.9em" />
        </span>
        <div className="flex flex-col">
          <strong className="font-semibold text-[var(--color-ink)]">I help with what matters most.</strong>
          <p className="leading-relaxed mt-0.5 text-[var(--color-ink-soft)]">
            Tell me where you're going, how many days, roughly when, who's coming, and any must-haves
            (diet, budget, pace). I'll ask about anything important that's missing.
          </p>
        </div>
      </div>

      <div className="mt-3 grid auto-rows-[minmax(0,1fr)] gap-2 sm:grid-cols-4">
        {CAPABILITIES.map((capability) => {
          const Glyph = capability.icon;
          const big = capability.span !== "";
          return (
            <button
              key={capability.title}
              type="button"
              onClick={() => onPrompt(capability.prompt)}
              className={`group relative overflow-hidden rounded-xl border border-[var(--color-line)] bg-[var(--color-surface)] p-3 text-left transition-[border-color,transform] duration-200 hover:border-[var(--color-brand)] hover:-translate-y-0.5 ${capability.span}`}
            >
              <div className="flex items-start gap-2.5">
                <span
                  className={`grid h-8 w-8 shrink-0 place-items-center rounded-lg ${capability.tint}`}
                >
                  <Glyph size="1.1em" />
                </span>
                <div className="flex flex-col">
                  <span className="block font-display text-xs font-semibold text-[var(--color-ink)]">
                    {capability.title}
                  </span>
                  <span className="mt-0.5 block text-[10px] leading-relaxed text-[var(--color-ink-soft)] pr-1">
                    {capability.hint}
                  </span>
                </div>
              </div>
              <img
                src={capability.image}
                alt={capability.title}
                className={`pointer-events-none w-full object-cover rounded-lg opacity-90 transition-transform duration-300 group-hover:scale-[1.03] ${big ? 'mt-2 h-16 sm:h-24' : 'mt-2 h-12 sm:h-16'}`}
              />
            </button>
          );
        })}
      </div>
      <div className="mt-4 pb-12">
        <p className="px-1 text-[10px] font-semibold uppercase tracking-wide text-[var(--color-ink-faint)] mb-1.5">
          Try one of these
        </p>
        <div className="grid gap-1.5 sm:grid-cols-2">
          {EXAMPLES.map((example, i) => (
            <button
              key={i}
              type="button"
              onClick={() => onPick(example.text)}
              className="flex items-center justify-between rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-3 py-2 text-xs text-[var(--color-ink-soft)] transition-colors duration-200 hover:border-[var(--color-brand)] hover:text-[var(--color-ink)] text-left"
            >
              <span className="flex items-center gap-1.5 truncate">
                <span className="text-sm">{example.icon}</span>
                <span className="truncate">{example.text}</span>
              </span>
              <IconChevron className="shrink-0 opacity-50 -rotate-90" size="1.1em" />
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

/**
 * The slot ledger, rendered. Empty slots simply do not appear, so the bar
 * grows as the conversation fills the trip in - a progress indicator that is
 * also an honesty check on what the agent claims to know.
 */
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
        {meta?.llm_providers?.includes("local") && !meta?.llm_providers?.includes("groq") && !turn.content && (
           <div className="mb-2 rounded-md border border-[var(--color-warning)] bg-[var(--color-warning)]/10 px-3 py-2 text-xs text-[var(--color-ink-strong)]">
             <strong>Quota Exceeded:</strong> The cloud AI quota has run out. Falling back to the offline local AI model...
           </div>
        )}
        {meta?.llm_providers?.includes("local") && meta?.llm_providers?.includes("groq") && (
           <div className="mb-2 rounded-md border border-[var(--color-warning)] bg-[var(--color-warning)]/10 px-3 py-2 text-xs text-[var(--color-ink-strong)]">
             <strong>Quota Exceeded:</strong> The cloud AI quota ran out mid-conversation. The rest of this response was generated using your local offline AI model.
           </div>
        )}
        {meta?.itinerary && (!turn.content || turn.content.includes("### Day ") || /Day \d+/.test(turn.content) || turn.content.includes("### Good to know")) ? (
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
            destination={meta.destination ?? ""}
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
    <div className="flex items-center gap-2">
      <button
        type="button"
        onClick={onToggle}
        aria-pressed={localOnly}
        title={
          localOnly
            ? "Running on a local model on this machine. Slower, but no quota used."
            : "Running on the fastest available cloud model. Falls back to local if the daily quota runs out."
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
        {localOnly ? "Local AI" : "Cloud AI"}
      </button>
      <button 
        type="button" 
        onClick={() => alert("Cloud AI uses Groq to give you the fastest, smartest results, but consumes your daily API quota.\\n\\nLocal AI runs the AI 'brain' locally on your machine, using zero API quota, but may be slower and slightly less capable.\\n\\nNote: Both modes still require internet access to fetch live weather, maps, and web search results.")}
        className="w-8 h-8 rounded-full border border-[var(--color-line-strong)] bg-[var(--color-surface)] flex items-center justify-center text-[var(--color-ink-soft)] hover:border-[var(--color-brand)] hover:text-[var(--color-brand)] transition-colors"
        title="What is this?"
      >
        <IconInfo size="1em" />
      </button>
    </div>
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
                        {enforced ? "" : ""}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
            <p className="mt-2 border-t border-[var(--color-line)] pt-2 text-[10px] leading-relaxed text-[var(--color-ink-faint)]">
              Toggle services on or off. Flights and Stays are fully removed when off.
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
  destination,
  onPick,
}: {
  options: string[];
  places: { name: string; latitude: number; longitude: number }[];
  destination: string;
  onPick: (text: string) => void;
}) {
  const [zoomed, setZoomed] = useState<{
    name: string;
    place?: { name: string; latitude: number; longitude: number };
  } | null>(null);

  const byName = new Map(places.map((place) => [place.name, place]));
  const numberOf = new Map(
    options.filter((option) => byName.has(option)).map((name, index) => [name, index + 1]),
  );

  return (
    <div className="mt-3 space-y-2.5">
      {/* No map here. It lives in the trip panel, which does not scroll away -
          two maps of the same places, one of them disappearing upward as the
          conversation grows, was worse than either alone. */}
      <div className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
        {/* Staggered 45ms apart: enough that the eye reads the gallery
            assembling as a sequence, short enough that the last card has
            landed before anyone is waiting on it. */}
        {options.map((option, position) => (
          <button
            key={option}
            type="button"
            onClick={() => setZoomed({ name: option, place: byName.get(option) })}
            aria-label={`See ${option}`}
            style={{ animationDelay: `${position * 45}ms` }}
            className="rise-in group overflow-hidden rounded-2xl border border-[var(--color-line-strong)] bg-[var(--color-surface)] text-left transition-[border-color,transform,box-shadow] duration-200 hover:-translate-y-0.5 hover:border-[var(--color-brand)] hover:shadow-[0_12px_28px_-20px_rgb(44_31_43_/_0.55)]"
          >
            <span className="relative block h-28 w-full overflow-hidden">
              <PlacePhoto
                name={option}
                destination={destination}
                latitude={byName.get(option)?.latitude ?? null}
                longitude={byName.get(option)?.longitude ?? null}
                kind="neighbourhood"
                className="h-28 w-full rounded-none transition-transform duration-500 group-hover:scale-105"
              />
              {numberOf.has(option) && (
                <span className="absolute left-2 top-2 grid size-5 place-items-center rounded-full bg-[var(--color-brand-strong)] text-[10px] font-semibold text-white">
                  {numberOf.get(option)}
                </span>
              )}
            </span>
            <span className="block truncate px-3 py-2 text-xs font-medium">{option}</span>
          </button>
        ))}
      </div>

      {zoomed && (
        <PlaceLightbox
          name={zoomed.name}
          destination={destination}
          place={zoomed.place}
          onClose={() => setZoomed(null)}
          onPick={() => {
            setZoomed(null);
            onPick(
              `Let's focus on ${zoomed.name}. Plan that specifically.`,
            );
          }}
        />
      )}
    </div>
  );
}

/**
 * A place, larger, without committing to it.
 *
 * The counterpart to splitting look from choose. Escape and a click outside
 * both dismiss, because a viewer that traps you is worse than no viewer, and
 * the one action that *does* commit is spelled out on a button rather than
 * being what happens when you touch the picture.
 */
function PlaceLightbox({
  name,
  destination,
  place,
  onClose,
  onPick,
}: {
  name: string;
  destination: string;
  place?: { name: string; latitude: number; longitude: number };
  onClose: () => void;
  onPick: () => void;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);

    // The page behind must not scroll under the dialog. Without this the
    // backdrop stays put while the content slides, which is the "glitchy"
    // part - the two layers visibly disagree about where the page is.
    const { overflow } = document.body.style;
    document.body.style.overflow = "hidden";

    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
    };
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={name}
      onClick={onClose}
      className="fade-in fixed inset-0 z-50 grid place-items-center bg-black/55 p-4 backdrop-blur-sm"
    >
      <div
        onClick={(event) => event.stopPropagation()}
        className="rise-in w-full max-w-lg overflow-hidden rounded-2xl bg-[var(--color-surface)] shadow-2xl"
      >
        <PlacePhoto
          name={name}
          destination={destination}
          latitude={place?.latitude ?? null}
          longitude={place?.longitude ?? null}
          kind="neighbourhood"
          eager
          className="h-64 w-full rounded-none sm:h-80"
        />
        <div className="flex items-center justify-between gap-3 p-4">
          <div className="min-w-0">
            <p className="truncate font-display text-base font-semibold">{name}</p>
            <p className="text-xs text-[var(--color-ink-faint)]">
              {place ? "Pinned on the map above" : "No map pin for this one"}
            </p>
          </div>
          <div className="flex shrink-0 gap-2">
            <Button variant="ghost" onClick={onClose}>
              Close
            </Button>
            <Button onClick={onPick}>Plan just this</Button>
          </div>
        </div>
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

/**
 * A microphone button that transcribes speech into text using the Web Speech API.
 *
 * Falls back gracefully: if the browser does not support speech recognition
 * (Firefox, for example), the button is not rendered at all rather than showing
 * a broken control. Chrome and Edge on desktop support it natively.
 */
function VoiceInput({
  onTranscript,
  disabled = false,
}: {
  onTranscript: (text: string) => void;
  disabled?: boolean;
}) {
  const [listening, setListening] = useState(false);
  const recognitionRef = useRef<any | null>(null);
  const [supported, setSupported] = useState(false);

  useEffect(() => {
    const SR =
      typeof window !== "undefined"
        ? (window as unknown as Record<string, unknown>).SpeechRecognition ??
          (window as unknown as Record<string, unknown>).webkitSpeechRecognition
        : null;
    setSupported(!!SR);
  }, []);

  const toggle = useCallback(() => {
    if (listening) {
      recognitionRef.current?.stop();
      setListening(false);
      return;
    }

    const SR =
      (window as unknown as Record<string, unknown>).SpeechRecognition ??
      (window as unknown as Record<string, unknown>).webkitSpeechRecognition;
    if (!SR) return;

    const recognition = new (SR as new () => any)();
    recognition.lang = ""; // auto-detect language
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;

    recognition.onresult = (event: any) => {
      const transcript = event.results[0][0].transcript;
      if (transcript.trim()) onTranscript(transcript.trim());
    };

    recognition.onend = () => setListening(false);
    recognition.onerror = () => setListening(false);

    recognitionRef.current = recognition;
    recognition.start();
    setListening(true);
  }, [listening, onTranscript]);

  if (!supported) return null;

  return (
    <button
      type="button"
      onClick={toggle}
      disabled={disabled}
      title={listening ? "Stop listening" : "Speak your message"}
      aria-label={listening ? "Stop listening" : "Speak your message"}
      className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl transition-all duration-200 ${
        listening
          ? "animate-pulse bg-red-500/15 text-red-500"
          : "text-[var(--color-ink-faint)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-brand)]"
      } disabled:opacity-50`}
    >
      <IconMic size="1.15em" />
    </button>
  );
}
