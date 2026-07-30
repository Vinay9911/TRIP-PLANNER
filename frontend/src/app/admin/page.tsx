"use client";

/**
 * Admin dashboard.
 *
 * Ordered by how much each panel actually tells you:
 *
 * 1. **Health at a glance** — run outcomes, latency and token cost. On a free
 *    tier the token number is the one that predicts failures, so it is on the
 *    front page rather than buried.
 * 2. **Tool analytics** — differing call counts across tools are the
 *    quantitative evidence that selection is dynamic rather than scripted. A
 *    flat distribution would suggest something is routing by rule.
 * 3. **Recent runs** — click through to a full execution trace. The only
 *    thing that lets a reviewer verify the agent genuinely plans and genuinely
 *    chooses tools rather than taking the documentation's word for it.
 * 4. **Memory health** — average mentions above 1 means facts are being
 *    reinforced rather than duplicated; a non-zero superseded count means
 *    contradictions are being resolved. Both at zero would mean the
 *    consolidation pipeline is not running.
 *
 * Everything read here is written to an append-only audit log by the backend.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { AuthGate } from "@/components/AuthGate";
import { BarChart, StatTile, type Point } from "@/components/charts";
import { IconBrain, IconChart, IconClock, IconSparkle, IconUsers } from "@/components/icons";
import {
  Badge,
  Card,
  EmptyState,
  ErrorBanner,
  PageHeader,
  StatusBadge,
} from "@/components/ui";
import { ApiError, api, type AdminUser } from "@/lib/api";

type Row = Record<string, unknown>;

const num = (row: Row | undefined, key: string): number => Number(row?.[key] ?? 0);
const str = (row: Row, key: string): string => String(row[key] ?? "");

export default function AdminPage() {
  return <AuthGate>{(session) => <Admin session={session} />}</AuthGate>;
}

function Admin({ session }: { session: { email: string | null; isAdmin: boolean } }) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [runs, setRuns] = useState<Row[]>([]);
  const [tools, setTools] = useState<Row[]>([]);
  const [runStats, setRunStats] = useState<Row>({});
  const [memoryStats, setMemoryStats] = useState<{
    totals: Record<string, number>;
    breakdown: Row[];
  }>({ totals: {}, breakdown: [] });
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // Fetched together: the panels are independent, so serialising them
      // would make the dashboard five round trips slower for no reason.
      const [u, r, t, rs, ms] = await Promise.all([
        api.admin.listUsers(),
        api.admin.recentRuns(),
        api.admin.toolAnalytics(),
        api.admin.runAnalytics(),
        api.admin.memoryAnalytics(),
      ]);
      setUsers(u);
      setRuns(r);
      setTools(t);
      setRunStats(rs);
      setMemoryStats(ms as { totals: Record<string, number>; breakdown: Row[] });
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not load the dashboard.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (!session.isAdmin) {
    return (
      <AppShell session={session}>
        <div className="mx-auto max-w-2xl px-4 py-20">
          <EmptyState
            title="Administrator access required"
            hint="Set app_role to 'admin' on your profile row in Supabase."
          />
        </div>
      </AppShell>
    );
  }

  const totalRuns = num(runStats, "total_runs");
  const completed = num(runStats, "completed");
  const successRate = totalRuns > 0 ? Math.round((completed / totalRuns) * 100) : 0;

  const outcomes: Point[] = [
    { label: "completed", value: completed },
    { label: "clarifying", value: num(runStats, "clarifying") },
    { label: "partial", value: num(runStats, "partial") },
    { label: "failed", value: num(runStats, "failed") },
  ].filter((point) => point.value > 0);

  const toolCalls: Point[] = tools
    .map((row) => ({ label: str(row, "tool_name"), value: num(row, "calls") }))
    .sort((a, b) => b.value - a.value);

  const toolLatency: Point[] = tools
    .map((row) => ({ label: str(row, "tool_name"), value: num(row, "avg_latency_ms") }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 6);

  const memoryByType: Point[] = Object.values(
    memoryStats.breakdown.reduce<Record<string, Point>>((acc, row) => {
      const key = str(row, "memory_type");
      acc[key] = { label: key, value: (acc[key]?.value ?? 0) + num(row, "count") };
      return acc;
    }, {}),
  ).sort((a, b) => b.value - a.value);

  return (
    <AppShell session={session}>
      <div className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6">
        <PageHeader
          title="Admin"
          subtitle="Run health, tool-selection evidence, and memory pipeline status. Every read here is written to the audit log."
        />

        {error && <ErrorBanner message={error} onRetry={() => void load()} />}

        {loading ? (
          <div className="grid gap-3 sm:grid-cols-4">
            {[0, 1, 2, 3].map((n) => (
              <div key={n} className="skeleton h-28 rounded-2xl" />
            ))}
          </div>
        ) : (
          <div className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <StatTile
                icon={<IconChart size="1.05em" />}
                label="Runs (7 days)"
                value={totalRuns}
                hint={`${successRate}% completed`}
                tone="brand"
              />
              <StatTile
                icon={<IconClock size="1.05em" />}
                label="Average latency"
                value={`${(num(runStats, "avg_latency_ms") / 1000).toFixed(1)}s`}
                hint={`${num(runStats, "avg_replans")} replans avg`}
                tone="sky"
              />
              <StatTile
                icon={<IconSparkle size="1.05em" />}
                label="Tokens per run"
                value={num(runStats, "avg_tokens_per_run").toLocaleString()}
                hint={`peak ${num(runStats, "max_tokens_per_run").toLocaleString()}`}
                tone="gold"
              />
              <StatTile
                icon={<IconUsers size="1.05em" />}
                label="Users"
                value={users.length}
                hint={`${memoryStats.totals.active ?? 0} memories stored`}
                tone="grape"
              />
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <Card className="p-5">
                <h2 className="font-display text-sm font-semibold">Tool selection</h2>
                <p className="mb-3 text-xs text-[var(--color-ink-soft)]">
                  Calls per tool over 7 days. An uneven spread is the evidence that the
                  model chooses tools per request rather than following a script.
                </p>
                <BarChart
                  points={toolCalls}
                  unit="calls"
                  emptyHint="No tool calls recorded in the last 7 days."
                />
              </Card>

              <Card className="p-5">
                <h2 className="font-display text-sm font-semibold">Run outcomes</h2>
                <p className="mb-3 text-xs text-[var(--color-ink-soft)]">
                  How runs ended. &ldquo;Clarifying&rdquo; and &ldquo;partial&rdquo; are
                  designed behaviours, not errors.
                </p>
                <BarChart
                  points={outcomes}
                  unit="runs"
                  emptyHint="No runs recorded in the last 7 days."
                />
              </Card>
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <Card className="p-5">
                <h2 className="font-display text-sm font-semibold">Slowest tools</h2>
                <p className="mb-3 text-xs text-[var(--color-ink-soft)]">
                  Average latency in milliseconds — where a slow reply actually comes from.
                </p>
                <BarChart points={toolLatency} unit="ms" emptyHint="No latency data yet." />
              </Card>

              <Card className="p-5">
                <div className="mb-1 flex items-center gap-2">
                  <span className="text-[var(--color-grape)]">
                    <IconBrain size="1.05em" />
                  </span>
                  <h2 className="font-display text-sm font-semibold">Memory pipeline</h2>
                </div>
                <p className="mb-3 text-xs text-[var(--color-ink-soft)]">
                  Stored facts by type.{" "}
                  {(memoryStats.totals.superseded ?? 0) > 0
                    ? `${memoryStats.totals.superseded} superseded — contradictions are being resolved.`
                    : "Nothing superseded yet."}
                </p>
                <BarChart
                  points={memoryByType}
                  unit="facts"
                  emptyHint="No memories extracted yet. The gate only stores facts that outlast a single trip."
                />
              </Card>
            </div>

            <Card className="p-5">
              <h2 className="font-display text-sm font-semibold">Recent runs</h2>
              <p className="mb-3 text-xs text-[var(--color-ink-soft)]">
                Open one to see the plan it wrote and every tool call it made.
              </p>
              {runs.length === 0 ? (
                <EmptyState title="No runs yet" hint="Runs appear here as soon as someone chats." />
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[36rem] text-left text-xs">
                    <thead className="text-[var(--color-ink-faint)]">
                      <tr className="border-b border-[var(--color-line)]">
                        <th scope="col" className="py-2 pr-3 font-medium">Run</th>
                        <th scope="col" className="py-2 pr-3 font-medium">Status</th>
                        <th scope="col" className="py-2 pr-3 font-medium">Latency</th>
                        <th scope="col" className="py-2 pr-3 font-medium">Replans</th>
                        <th scope="col" className="py-2 font-medium">Hops</th>
                      </tr>
                    </thead>
                    <tbody>
                      {runs.slice(0, 12).map((run) => (
                        <tr key={str(run, "id")} className="border-b border-[var(--color-line)]">
                          <td className="py-2 pr-3">
                            <Link
                              href={`/admin/runs/${str(run, "id")}`}
                              className="font-mono font-medium text-[var(--color-brand-strong)] underline underline-offset-2"
                            >
                              {str(run, "id").slice(0, 8)}
                            </Link>
                          </td>
                          <td className="py-2 pr-3">
                            <StatusBadge status={str(run, "status")} />
                          </td>
                          <td className="py-2 pr-3 tabular-nums">
                            {(num(run, "latency_ms") / 1000).toFixed(1)}s
                          </td>
                          <td className="py-2 pr-3 tabular-nums">{num(run, "replan_count")}</td>
                          <td className="py-2 tabular-nums">{num(run, "rag_hops")}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>

            <Card className="p-5">
              <h2 className="font-display text-sm font-semibold">Users</h2>
              <p className="mb-3 text-xs text-[var(--color-ink-soft)]">
                Accounts and their activity.
              </p>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[34rem] text-left text-xs">
                  <thead className="text-[var(--color-ink-faint)]">
                    <tr className="border-b border-[var(--color-line)]">
                      <th scope="col" className="py-2 pr-3 font-medium">Email</th>
                      <th scope="col" className="py-2 pr-3 font-medium">Role</th>
                      <th scope="col" className="py-2 pr-3 font-medium">Chats</th>
                      <th scope="col" className="py-2 pr-3 font-medium">Messages</th>
                      <th scope="col" className="py-2 font-medium">Memories</th>
                    </tr>
                  </thead>
                  <tbody>
                    {users.map((user) => (
                      <tr key={user.id} className="border-b border-[var(--color-line)]">
                        <td className="py-2 pr-3">
                          <Link
                            href={`/admin/users/${user.id}`}
                            className="font-medium text-[var(--color-brand-strong)] underline underline-offset-2"
                          >
                            {user.email ?? user.id.slice(0, 8)}
                          </Link>
                        </td>
                        <td className="py-2 pr-3">
                          <Badge tone={user.app_role === "admin" ? "accent" : "neutral"}>
                            {user.app_role}
                          </Badge>
                        </td>
                        <td className="py-2 pr-3 tabular-nums">{user.session_count}</td>
                        <td className="py-2 pr-3 tabular-nums">{user.message_count}</td>
                        <td className="py-2 tabular-nums">{user.memory_count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          </div>
        )}
      </div>
    </AppShell>
  );
}
