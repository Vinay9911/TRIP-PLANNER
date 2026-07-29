"use client";

/**
 * The chat page.
 *
 * One deliberate choice worth noting: each reply can expand to show **what the
 * agent actually did** - the plan it wrote and every tool it called, with
 * latencies. Most chat interfaces hide this. Showing it turns the claims in
 * the documentation into something a reviewer can verify in the UI, and it is
 * genuinely useful to a user deciding how much to trust an itinerary.
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
import { ApiError, api, type ChatResponse } from "@/lib/api";
import { signOut } from "@/lib/supabase";

interface Turn {
  role: "user" | "assistant";
  content: string;
  meta?: ChatResponse;
}

const EXAMPLES = [
  "Plan me 2 relaxed days in Kyoto. I'm vegetarian.",
  "Will I need an umbrella in Singapore next week?",
  "3 days in Lisbon on a tight budget, travelling with my dog",
  "京都で2日間の旅程を立ててください",
];

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
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, busy]);

  async function send(message: string) {
    const trimmed = message.trim();
    if (!trimmed || busy) return;

    setError(null);
    setInput("");
    setTurns((previous) => [...previous, { role: "user", content: trimmed }]);
    setBusy(true);

    try {
      const reply = await api.chat(trimmed, sessionId);
      // Reusing the session id is what gives the agent conversational
      // context - follow-ups like "make day two lighter" depend on it.
      setSessionId(reply.session_id);
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
    <div className="mx-auto flex min-h-screen max-w-3xl flex-col px-4">
      <header className="flex items-center justify-between border-b border-[var(--color-line)] py-4">
        <span className="font-semibold tracking-tight">Trip Planner</span>
        <div className="flex items-center gap-2">
          <Nav email={session.email} isAdmin={session.isAdmin} />
          <Button variant="ghost" onClick={() => void signOut()}>
            Sign out
          </Button>
        </div>
      </header>

      <main className="flex-1 space-y-4 py-6">
        {turns.length === 0 && <Welcome onPick={(text) => void send(text)} />}

        {turns.map((turn, index) => (
          <Message key={index} turn={turn} />
        ))}

        {busy && <Thinking />}
        {error && <ErrorBanner message={error} />}
        <div ref={endRef} />
      </main>

      <footer className="sticky bottom-0 bg-[var(--color-paper)] pb-6 pt-2">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void send(input);
          }}
          className="flex gap-2"
        >
          <input
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
        <p className="mt-2 text-center text-xs text-[var(--color-ink-soft)]">
          Flight and hotel prices are simulated. Everything else is real data.
        </p>
      </footer>
    </div>
  );
}

function Welcome({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="py-8">
      <h2 className="text-lg font-semibold">Where are you going?</h2>
      <p className="mt-1 text-sm text-[var(--color-ink-soft)]">
        Mention anything that matters — dietary needs, budget, pace, who you are
        travelling with. It will remember for next time.
      </p>
      <div className="mt-5 grid gap-2 sm:grid-cols-2">
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

function Message({ turn }: { turn: Turn }) {
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

        {meta && (
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
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
                  {call.status === "degraded" && <Badge tone="warn">unavailable</Badge>}
                  {call.status === "invalid" && <Badge tone="bad">retried</Badge>}
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
      Researching and planning… this usually takes 10–20 seconds.
    </div>
  );
}
