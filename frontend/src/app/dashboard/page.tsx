"use client";

/**
 * "Your travel" - the traveller's own dashboard.
 *
 * Everything here is derived from data the user already owns (their
 * conversations and their stored memories) rather than from a separate
 * analytics pipeline. That keeps it honest: there is no number on this page
 * that is not a direct count of something they can go and look at.
 *
 * It also does a job the chat interface cannot. A conversation shows one trip
 * at a time; this shows the shape of everything - where they have been
 * planning, how much the agent has learned about them, and which requirements
 * it now treats as non-negotiable. For an assessment reviewer it is also the
 * quickest proof that long-term memory is real and accumulating.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { AuthGate } from "@/components/AuthGate";
import { BarChart, Gauge, StatTile, TrendChart, type Point } from "@/components/charts";
import {
  IconBrain,
  IconChat,
  IconCompass,
  IconPin,
  IconShield,
  IconSparkle,
} from "@/components/icons";
import {
  Badge,
  Card,
  EmptyState,
  ErrorBanner,
  PageHeader,
  PlaceImage,
} from "@/components/ui";
import { ApiError, api, type Memory, type SessionSummary } from "@/lib/api";

export default function DashboardPage() {
  return <AuthGate>{(session) => <Dashboard session={session} />}</AuthGate>;
}

function Dashboard({ session }: { session: { email: string | null; isAdmin: boolean } }) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [memories, setMemories] = useState<Memory[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [s, m] = await Promise.all([api.listSessions(), api.listMemories()]);
      setSessions(s);
      setMemories(m.filter((memory) => memory.status === "active"));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load your travel data.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const totalMessages = sessions.reduce((sum, s) => sum + (s.message_count ?? 0), 0);
  const constraints = memories.filter((m) => m.memory_type === "constraint");

  // Destinations by how often they were planned.
  const destinationCounts = new Map<string, number>();
  for (const s of sessions) {
    if (!s.destination) continue;
    destinationCounts.set(s.destination, (destinationCounts.get(s.destination) ?? 0) + 1);
  }
  const destinations: Point[] = [...destinationCounts.entries()]
    .map(([label, value]) => ({ label, value }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 8);

  // Planning activity over the last 14 days.
  const byDay = new Map<string, number>();
  const today = new Date();
  for (let i = 13; i >= 0; i -= 1) {
    const day = new Date(today);
    day.setDate(today.getDate() - i);
    byDay.set(day.toISOString().slice(0, 10), 0);
  }
  for (const s of sessions) {
    if (!s.updated_at) continue;
    const key = s.updated_at.slice(0, 10);
    if (byDay.has(key)) byDay.set(key, (byDay.get(key) ?? 0) + 1);
  }
  const activity: Point[] = [...byDay.entries()].map(([iso, value]) => ({
    label: new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" }),
    value,
  }));

  // What the agent has learned, by subject.
  const subjectCounts = new Map<string, number>();
  for (const m of memories) {
    subjectCounts.set(m.subject, (subjectCounts.get(m.subject) ?? 0) + 1);
  }
  const subjects: Point[] = [...subjectCounts.entries()]
    .map(([label, value]) => ({ label, value }))
    .sort((a, b) => b.value - a.value);

  const recentDestinations = sessions.filter((s) => s.destination).slice(0, 6);

  return (
    <AppShell session={session}>
      <div className="mx-auto w-full max-w-5xl px-4 py-6 sm:px-6">
        <PageHeader
          title="Your travel"
          subtitle="Everything the planner has worked on with you, and what it has learned along the way."
        />

        {error && <ErrorBanner message={error} onRetry={() => void load()} />}

        {loading ? (
          <div className="grid gap-3 sm:grid-cols-4">
            {[0, 1, 2, 3].map((n) => (
              <div key={n} className="skeleton h-28 rounded-2xl" />
            ))}
          </div>
        ) : sessions.length === 0 && memories.length === 0 ? (
          <EmptyState
            title="Nothing planned yet"
            hint="Start a conversation and this page fills in — the places you've explored, how often you plan, and everything the agent remembers about how you like to travel."
            action={
              <Link
                href="/chat"
                className="inline-flex min-h-11 items-center gap-2 rounded-xl bg-[var(--color-brand-strong)] px-4 text-sm font-medium text-white"
              >
                <IconCompass size="1.1em" />
                Plan your first trip
              </Link>
            }
          />
        ) : (
          <div className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <StatTile
                icon={<IconChat size="1.05em" />}
                label="Conversations"
                value={sessions.length}
                hint={`${totalMessages} messages exchanged`}
                tone="brand"
              />
              <StatTile
                icon={<IconPin size="1.05em" />}
                label="Destinations"
                value={destinationCounts.size}
                hint={destinations[0] ? `Most planned: ${destinations[0].label}` : "None yet"}
                tone="sky"
              />
              <StatTile
                icon={<IconBrain size="1.05em" />}
                label="Things remembered"
                value={memories.length}
                hint="Applied to every future trip"
                tone="grape"
              />
              <StatTile
                icon={<IconShield size="1.05em" />}
                label="Hard requirements"
                value={constraints.length}
                hint="Never traded off when planning"
                tone="mint"
              />
            </div>

            <div className="grid gap-4 lg:grid-cols-3">
              <Card className="p-5 lg:col-span-2">
                <h2 className="font-display text-sm font-semibold">Planning activity</h2>
                <p className="mb-3 text-xs text-[var(--color-ink-soft)]">
                  Conversations you touched, last 14 days.
                </p>
                <TrendChart
                  points={activity}
                  unit="conversations"
                  emptyHint="No planning activity in the last two weeks."
                />
              </Card>

              <Card className="p-5">
                <h2 className="font-display text-sm font-semibold">Memory strength</h2>
                <p className="mb-4 text-xs text-[var(--color-ink-soft)]">
                  How much the agent can personalise for you.
                </p>
                <Gauge
                  value={memories.length}
                  max={Math.max(memories.length, 12)}
                  label="facts learned"
                  caption={
                    memories.length === 0
                      ? "Mention a preference and it starts here."
                      : `${constraints.length} of them are hard requirements.`
                  }
                />
              </Card>
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <Card className="p-5">
                <h2 className="font-display text-sm font-semibold">Where you plan</h2>
                <p className="mb-3 text-xs text-[var(--color-ink-soft)]">
                  Destinations by number of conversations.
                </p>
                <BarChart
                  points={destinations}
                  unit="conversations"
                  emptyHint="No destination has been settled in a conversation yet."
                />
              </Card>

              <Card className="p-5">
                <h2 className="font-display text-sm font-semibold">What it knows about you</h2>
                <p className="mb-3 text-xs text-[var(--color-ink-soft)]">
                  Stored facts grouped by subject.{" "}
                  <Link
                    href="/memories"
                    className="font-medium text-[var(--color-brand-strong)] underline underline-offset-2"
                  >
                    Review or delete
                  </Link>
                </p>
                <BarChart
                  points={subjects}
                  unit="facts"
                  emptyHint="Nothing stored yet — mention a dietary need, budget or travel style in a conversation."
                />
              </Card>
            </div>

            {recentDestinations.length > 0 && (
              <Card className="p-5">
                <h2 className="font-display text-sm font-semibold">Pick up where you left off</h2>
                <p className="mb-3 text-xs text-[var(--color-ink-soft)]">
                  Photos are illustrative, not pictures of the actual places.
                </p>
                <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
                  {recentDestinations.map((s) => (
                    <Link
                      key={s.id}
                      href={`/chat?session=${s.id}`}
                      className="group overflow-hidden rounded-xl border border-[var(--color-line)] bg-[var(--color-surface)] transition-[border-color,transform] duration-200 hover:-translate-y-0.5 hover:border-[var(--color-brand)]"
                    >
                      <PlaceImage
                        name={s.destination ?? s.id}
                        width={240}
                        height={160}
                        className="h-20 w-full"
                      />
                      <span className="block truncate px-2.5 py-2 text-xs font-medium">
                        {s.destination}
                      </span>
                    </Link>
                  ))}
                </div>
              </Card>
            )}

            {constraints.length > 0 && (
              <Card className="p-5">
                <div className="mb-3 flex items-center gap-2">
                  <span className="text-[var(--color-mint)]">
                    <IconSparkle size="1.05em" />
                  </span>
                  <h2 className="font-display text-sm font-semibold">
                    Always applied to your trips
                  </h2>
                </div>
                <div className="flex flex-wrap gap-2">
                  {constraints.map((memory) => (
                    <Badge key={memory.id} tone="good">
                      {memory.content}
                    </Badge>
                  ))}
                </div>
              </Card>
            )}
          </div>
        )}
      </div>
    </AppShell>
  );
}
