"use client";

/**
 * Admin dashboard.
 *
 * Four panels, ordered by how much they actually tell you:
 *
 * 1. **Recent runs** — click through to a full execution trace. The single
 *    most useful view, because it is the only thing that lets a reviewer
 *    verify the agent genuinely plans and genuinely chooses tools rather than
 *    taking the documentation's word for it.
 * 2. **Tool analytics** — differing call counts across tools are the
 *    quantitative evidence that selection is dynamic rather than scripted.
 * 3. **Memory health** — average mentions above 1 means facts are being
 *    reinforced rather than duplicated; a non-zero superseded count means
 *    contradictions are being resolved. Both at zero would mean the
 *    consolidation pipeline is not running.
 * 4. **Users** — accounts with their activity.
 *
 * Everything read here is written to an append-only audit log by the backend.
 */

import Link from "next/link";
import { useEffect, useState } from "react";

import { AuthGate } from "@/components/AuthGate";
import {
  Badge,
  Card,
  EmptyState,
  ErrorBanner,
  Nav,
  PageHeader,
  StatusBadge,
} from "@/components/ui";
import { ApiError, api, type AdminUser } from "@/lib/api";

type Row = Record<string, unknown>;

export default function AdminPage() {
  return <AuthGate>{(session) => <Admin session={session} />}</AuthGate>;
}

function Admin({
  session,
}: {
  session: { email: string | null; isAdmin: boolean };
}) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [runs, setRuns] = useState<Row[]>([]);
  const [tools, setTools] = useState<Row[]>([]);
  const [runStats, setRunStats] = useState<Row>({});
  const [memoryStats, setMemoryStats] = useState<{ totals: Record<string, number> }>({
    totals: {},
  });
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function load() {
      try {
        // Fetched together: the panels are independent, so serialising them
        // would make the dashboard four round trips slower for no reason.
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
        setMemoryStats(ms);
      } catch (caught) {
        setError(
          caught instanceof ApiError
            ? caught.message
            : "Could not load the dashboard.",
        );
      } finally {
        setLoading(false);
      }
    }
    void load();
  }, []);

  if (!session.isAdmin) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-20">
        <EmptyState
          title="Administrator access required"
          hint="Set app_role to 'admin' on your profile row in Supabase."
        />
      </div>
    );
  }

  return (
    <div className="mx-auto min-h-screen max-w-6xl px-4 py-6">
      <div className="mb-6 flex items-center justify-between">
        <Link href="/" className="font-semibold tracking-tight">
          Trip Planner
        </Link>
        <Nav email={session.email} isAdmin />
      </div>

      <PageHeader
        title="Admin"
        subtitle="Agent behaviour, memory health and user activity. All access here is audited."
      />

      {error && (
        <div className="mt-4">
          <ErrorBanner message={error} />
        </div>
      )}
      {loading && (
        <p className="mt-8 text-sm text-[var(--color-ink-soft)]">Loading…</p>
      )}

      {!loading && (
        <div className="mt-6 space-y-8">
          <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="Runs (7d)" value={num(runStats.total_runs)} />
            <Stat
              label="Completed"
              value={`${pct(runStats.completed, runStats.total_runs)}%`}
              hint={`${num(runStats.clarifying)} asked a question`}
            />
            <Stat
              label="Median latency"
              value={`${(num(runStats.avg_latency_ms) / 1000).toFixed(1)}s`}
              hint={`p95 ${(num(runStats.p95_latency_ms) / 1000).toFixed(1)}s`}
            />
            <Stat
              label="Avg RAG hops"
              value={num(runStats.avg_rag_hops).toFixed(1)}
              hint={`${num(runStats.avg_replans).toFixed(2)} replans/run`}
            />
          </section>

          <Panel
            title="Recent runs"
            hint="Open one to see the plan, every step, and every tool call with its arguments."
          >
            {runs.length === 0 ? (
              <EmptyState title="No runs yet" hint="Send a message from the chat page." />
            ) : (
              <Table
                head={["Run", "User", "Status", "Tools", "Replans", "Lang", "Latency"]}
                rows={runs.slice(0, 15).map((run) => [
                  <Link
                    key="id"
                    href={`/admin/runs/${String(run.id)}`}
                    className="font-mono text-xs text-[var(--color-accent)] underline underline-offset-2"
                  >
                    {String(run.id).slice(0, 8)}
                  </Link>,
                  <span key="email" className="text-xs">
                    {String(run.email ?? "—")}
                  </span>,
                  <StatusBadge key="status" status={String(run.status)} />,
                  num(run.tool_call_count),
                  num(run.replan_count),
                  String(run.detected_language ?? "—"),
                  `${(num(run.latency_ms) / 1000).toFixed(1)}s`,
                ])}
              />
            )}
          </Panel>

          <div className="grid gap-6 lg:grid-cols-2">
            <Panel
              title="Tool usage"
              hint="Different questions produce different patterns — which would not happen with fixed routing."
            >
              {tools.length === 0 ? (
                <EmptyState title="No tool calls recorded yet" />
              ) : (
                <Table
                  head={["Tool", "Calls", "Degraded", "p95"]}
                  rows={tools.map((tool) => [
                    <code key="t" className="font-mono text-xs">
                      {String(tool.tool_name)}
                    </code>,
                    num(tool.calls),
                    num(tool.degraded) > 0 ? (
                      <Badge key="d" tone="warn">
                        {num(tool.degraded)}
                      </Badge>
                    ) : (
                      "0"
                    ),
                    `${num(tool.p95_latency_ms)}ms`,
                  ])}
                />
              )}
            </Panel>

            <Panel
              title="Memory health"
              hint="Superseded entries mean contradictions are being resolved, not stacked."
            >
              <div className="grid grid-cols-2 gap-3">
                <Stat label="Total memories" value={num(memoryStats.totals.total)} />
                <Stat label="Active" value={num(memoryStats.totals.active)} />
                <Stat label="Superseded" value={num(memoryStats.totals.superseded)} />
                <Stat
                  label="Users with memories"
                  value={num(memoryStats.totals.users_with_memories)}
                />
              </div>
            </Panel>
          </div>

          <Panel title="Users">
            {users.length === 0 ? (
              <EmptyState title="No users yet" />
            ) : (
              <Table
                head={["Email", "Role", "Sessions", "Messages", "Memories", "Last seen"]}
                rows={users.map((user) => [
                  <Link
                    key="e"
                    href={`/admin/users/${user.id}`}
                    className="text-[var(--color-accent)] underline underline-offset-2"
                  >
                    {user.email ?? user.display_name ?? user.id.slice(0, 8)}
                  </Link>,
                  user.app_role === "admin" ? (
                    <Badge key="r" tone="accent">
                      admin
                    </Badge>
                  ) : (
                    "user"
                  ),
                  user.session_count,
                  user.message_count,
                  user.memory_count,
                  user.last_seen_at
                    ? new Date(user.last_seen_at).toLocaleDateString()
                    : "—",
                ])}
              />
            )}
          </Panel>
        </div>
      )}
    </div>
  );
}

function Panel({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h2 className="text-sm font-semibold">{title}</h2>
      {hint && <p className="mb-3 text-xs text-[var(--color-ink-soft)]">{hint}</p>}
      {children}
    </section>
  );
}

function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: string | number;
  hint?: string;
}) {
  return (
    <Card className="px-4 py-3">
      <p className="text-xs text-[var(--color-ink-soft)]">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">{value}</p>
      {hint && <p className="text-xs text-[var(--color-ink-soft)]">{hint}</p>}
    </Card>
  );
}

function Table({
  head,
  rows,
}: {
  head: string[];
  rows: (React.ReactNode | string | number)[][];
}) {
  return (
    <Card className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-[var(--color-line)] text-left text-xs text-[var(--color-ink-soft)]">
            {head.map((heading) => (
              <th key={heading} className="px-4 py-2 font-medium">
                {heading}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr
              key={rowIndex}
              className="border-b border-[var(--color-line)] last:border-0"
            >
              {row.map((cell, cellIndex) => (
                <td key={cellIndex} className="px-4 py-2 tabular-nums">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function num(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function pct(part: unknown, whole: unknown): number {
  const total = num(whole);
  return total === 0 ? 0 : Math.round((num(part) / total) * 100);
}
