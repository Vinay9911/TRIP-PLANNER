"use client";

/**
 * "What it remembers about you."
 *
 * This page exists because a system that quietly builds a profile of someone
 * and never shows it to them is doing something users are right to distrust.
 * Everything stored is visible, correctable and deletable by its subject.
 *
 * The **empty state** had to be rewritten after a real report that this page
 * "wasn't working". It was working: extraction had correctly declined to
 * store anything, because the messages in question ("bali", "tell me about
 * india", "Let's do Mumbai") contain no fact that would still be true on a
 * different trip. But an empty page with no explanation looks identical to a
 * broken one, so it now says what the agent stores, what it deliberately does
 * not, and gives an example that would actually populate it.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { AuthGate } from "@/components/AuthGate";
import { IconBrain, IconInfo, IconShield } from "@/components/icons";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorBanner,
  PageHeader,
} from "@/components/ui";
import { ApiError, api, type Memory } from "@/lib/api";

const TYPE_LABELS: Record<Memory["memory_type"], string> = {
  constraint: "Must be honoured",
  preference: "Preference",
  identity: "About you",
  experience: "Past travel",
};

export default function MemoriesPage() {
  return <AuthGate>{(session) => <Memories session={session} />}</AuthGate>;
}

function Memories({ session }: { session: { email: string | null; isAdmin: boolean } }) {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setMemories(await api.listMemories(showHistory));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load memories.");
    } finally {
      setLoading(false);
    }
  }, [showHistory]);

  useEffect(() => {
    void load();
  }, [load]);

  async function forget(id: string) {
    try {
      await api.deleteMemory(id);
      setMemories((previous) => previous.filter((memory) => memory.id !== id));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not delete that.");
    }
  }

  const active = memories.filter((memory) => memory.status === "active");
  const constraints = active.filter((memory) => memory.memory_type === "constraint");
  const others = active.filter((memory) => memory.memory_type !== "constraint");
  const retired = memories.filter((memory) => memory.status !== "active");

  return (
    <AppShell session={session}>
      <div className="mx-auto w-full max-w-3xl px-4 py-6 sm:px-6">
        <PageHeader
          title="What it remembers about you"
          subtitle="Applied automatically to every future trip, so you never repeat yourself. Delete anything you'd rather it forgot."
        />

        {error && <ErrorBanner message={error} onRetry={() => void load()} />}

        {loading ? (
          <div className="space-y-2">
            {[0, 1, 2].map((n) => (
              <div key={n} className="skeleton h-16 rounded-2xl" />
            ))}
          </div>
        ) : active.length === 0 ? (
          <EmptyState
            title="Nothing remembered yet — and that's expected"
            hint={
              <div className="space-y-3 text-left">
                <p>
                  The agent only stores things that would still be true on a{" "}
                  <em>completely different trip</em>. Asking about a destination
                  doesn&apos;t create a memory — otherwise your profile would fill up
                  with places you merely looked at once.
                </p>
                <div className="grid gap-2 sm:grid-cols-2">
                  <div className="rounded-xl border border-[var(--color-line)] bg-[var(--color-mint-soft)]/50 p-3">
                    <p className="text-xs font-semibold text-[var(--color-mint)]">
                      Gets remembered
                    </p>
                    <p className="mt-1 text-xs">
                      &ldquo;I&apos;m vegetarian&rdquo; · &ldquo;I travel on a tight
                      budget&rdquo; · &ldquo;I hate crowds&rdquo; · &ldquo;I fly from
                      Delhi&rdquo;
                    </p>
                  </div>
                  <div className="rounded-xl border border-[var(--color-line)] bg-[var(--color-surface-2)] p-3">
                    <p className="text-xs font-semibold text-[var(--color-ink-soft)]">
                      Deliberately not
                    </p>
                    <p className="mt-1 text-xs">
                      &ldquo;Tell me about India&rdquo; · &ldquo;5 days in
                      March&rdquo; · &ldquo;Let&apos;s do Mumbai&rdquo;
                    </p>
                  </div>
                </div>
              </div>
            }
            action={
              <Link
                href="/"
                className="inline-flex min-h-11 items-center gap-2 rounded-xl bg-[var(--color-brand-strong)] px-4 text-sm font-medium text-white"
              >
                <IconBrain size="1.1em" />
                Tell it something about you
              </Link>
            }
          />
        ) : (
          <div className="space-y-7">
            {constraints.length > 0 && (
              <Section
                icon={<IconShield size="1.05em" />}
                title="Hard requirements"
                hint="Never traded off. Passed to searches as filters, not hints."
                memories={constraints}
                onForget={forget}
              />
            )}
            {others.length > 0 && (
              <Section
                icon={<IconInfo size="1.05em" />}
                title="Preferences and history"
                hint="Used to personalise. Anything you say now takes precedence."
                memories={others}
                onForget={forget}
              />
            )}
          </div>
        )}

        <div className="mt-10 border-t border-[var(--color-line)] pt-4">
          <button
            type="button"
            onClick={() => setShowHistory((value) => !value)}
            aria-expanded={showHistory}
            className="min-h-9 text-sm font-medium text-[var(--color-brand-strong)] underline underline-offset-2"
          >
            {showHistory ? "Hide" : "Show"} superseded memories
          </button>
          {showHistory && (
            <p className="mt-2 text-xs leading-relaxed text-[var(--color-ink-soft)]">
              When you change your mind, the old belief is retired rather than deleted
              — so you can see how your profile has evolved.
            </p>
          )}
          {showHistory && retired.length > 0 && (
            <ul className="mt-3 space-y-2">
              {retired.map((memory) => (
                <li
                  key={memory.id}
                  className="rounded-xl border border-dashed border-[var(--color-line-strong)] px-3 py-2 text-sm text-[var(--color-ink-faint)] line-through"
                >
                  {memory.content}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </AppShell>
  );
}

function Section({
  icon,
  title,
  hint,
  memories,
  onForget,
}: {
  icon: React.ReactNode;
  title: string;
  hint: string;
  memories: Memory[];
  onForget: (id: string) => void;
}) {
  return (
    <section>
      <h2 className="flex items-center gap-2 font-display text-sm font-semibold">
        <span className="text-[var(--color-brand)]">{icon}</span>
        {title}
      </h2>
      <p className="mb-3 mt-0.5 text-xs text-[var(--color-ink-soft)]">{hint}</p>
      <ul className="space-y-2">
        {memories.map((memory) => (
          <li key={memory.id}>
            <Card className="flex items-start justify-between gap-4 px-4 py-3">
              <div className="min-w-0">
                <p className="text-sm">{memory.content}</p>
                <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-[var(--color-ink-faint)]">
                  <Badge tone={memory.memory_type === "constraint" ? "good" : "neutral"}>
                    {TYPE_LABELS[memory.memory_type]}
                  </Badge>
                  <span>{memory.subject}</span>
                  {/* Mention count is the most legible answer to "why does it
                      believe this?" - it shows reinforcement rather than a
                      single stray extraction. */}
                  <span className="tabular-nums">mentioned {memory.mention_count}×</span>
                  <span className="tabular-nums">
                    {Math.round(memory.confidence * 100)}% confident
                  </span>
                  {memory.source_lang && memory.source_lang !== "en" && (
                    <span>said in {memory.source_lang.toUpperCase()}</span>
                  )}
                </div>
              </div>
              <Button variant="ghost" onClick={() => onForget(memory.id)} className="shrink-0 px-2">
                <span className="text-xs text-[var(--color-danger)]">Forget</span>
              </Button>
            </Card>
          </li>
        ))}
      </ul>
    </section>
  );
}
