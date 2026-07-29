"use client";

/**
 * "What it remembers about you."
 *
 * This page exists because a system that quietly builds a profile of someone
 * and never shows it to them is doing something users are right to distrust.
 * Everything stored is visible, correctable and deletable by its subject.
 *
 * It also happens to be the clearest demonstration of the long-term memory
 * system: hold a conversation, come here, and see the extracted facts with
 * their confidence and how many times you have mentioned each one.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { AuthGate } from "@/components/AuthGate";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorBanner,
  Nav,
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

function Memories({
  session,
}: {
  session: { email: string | null; isAdmin: boolean };
}) {
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

  async function eraseEverything() {
    if (
      !window.confirm(
        "This permanently deletes every memory, message and conversation. Your account stays. Continue?",
      )
    ) {
      return;
    }
    try {
      await api.eraseMyData();
      setMemories([]);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not erase your data.");
    }
  }

  const active = memories.filter((memory) => memory.status === "active");
  const constraints = active.filter((memory) => memory.memory_type === "constraint");
  const others = active.filter((memory) => memory.memory_type !== "constraint");
  const retired = memories.filter((memory) => memory.status !== "active");

  return (
    <div className="mx-auto min-h-screen max-w-3xl px-4 py-6">
      <div className="mb-6 flex items-center justify-between">
        <Link href="/" className="font-semibold tracking-tight">
          Trip Planner
        </Link>
        <Nav email={session.email} isAdmin={session.isAdmin} />
      </div>

      <PageHeader
        title="What it remembers about you"
        subtitle="Applied automatically to every future trip, so you never repeat yourself."
        actions={
          <Button variant="danger" onClick={() => void eraseEverything()}>
            Erase everything
          </Button>
        }
      />

      {error && (
        <div className="mt-4">
          <ErrorBanner message={error} />
        </div>
      )}

      {loading ? (
        <p className="mt-8 text-sm text-[var(--color-ink-soft)]">Loading…</p>
      ) : active.length === 0 ? (
        <div className="mt-8">
          <EmptyState
            title="Nothing remembered yet"
            hint="Mention a dietary need, budget or travel style in a conversation and it will appear here."
          />
        </div>
      ) : (
        <div className="mt-6 space-y-8">
          {constraints.length > 0 && (
            <Section
              title="Hard requirements"
              hint="Never traded off. Passed to searches as filters, not hints."
              memories={constraints}
              onForget={forget}
            />
          )}
          {others.length > 0 && (
            <Section
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
          className="text-sm text-[var(--color-accent)] underline underline-offset-2"
        >
          {showHistory ? "Hide" : "Show"} superseded memories
        </button>
        {showHistory && (
          <p className="mt-2 text-xs text-[var(--color-ink-soft)]">
            When you change your mind, the old belief is retired rather than
            deleted — so you can see how your profile has evolved.
          </p>
        )}
        {showHistory && retired.length > 0 && (
          <ul className="mt-3 space-y-2">
            {retired.map((memory) => (
              <li
                key={memory.id}
                className="rounded-lg border border-dashed border-[var(--color-line)] px-3 py-2 text-sm text-[var(--color-ink-soft)] line-through"
              >
                {memory.content}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function Section({
  title,
  hint,
  memories,
  onForget,
}: {
  title: string;
  hint: string;
  memories: Memory[];
  onForget: (id: string) => void;
}) {
  return (
    <section>
      <h2 className="text-sm font-semibold">{title}</h2>
      <p className="mb-3 text-xs text-[var(--color-ink-soft)]">{hint}</p>
      <ul className="space-y-2">
        {memories.map((memory) => (
          <li key={memory.id}>
            <Card className="flex items-start justify-between gap-4 px-4 py-3">
              <div className="min-w-0">
                <p className="text-sm">{memory.content}</p>
                <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-[var(--color-ink-soft)]">
                  <Badge tone={memory.memory_type === "constraint" ? "accent" : "neutral"}>
                    {TYPE_LABELS[memory.memory_type]}
                  </Badge>
                  <span>{memory.subject}</span>
                  {/* Mention count is the most legible answer to "why does it
                      believe this?" - it shows reinforcement rather than a
                      single stray extraction. */}
                  <span>
                    mentioned {memory.mention_count}×
                  </span>
                  <span>{Math.round(memory.confidence * 100)}% confident</span>
                  {memory.source_lang && memory.source_lang !== "en" && (
                    <span>said in {memory.source_lang.toUpperCase()}</span>
                  )}
                </div>
              </div>
              <button
                type="button"
                onClick={() => onForget(memory.id)}
                className="shrink-0 text-xs text-[var(--color-danger)] underline underline-offset-2"
              >
                Forget
              </button>
            </Card>
          </li>
        ))}
      </ul>
    </section>
  );
}
