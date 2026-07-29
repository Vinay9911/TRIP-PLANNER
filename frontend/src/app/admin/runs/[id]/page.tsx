"use client";

/**
 * Execution trace for one agent run.
 *
 * The most valuable page in the admin portal. Everything this project claims
 * about planning and dynamic tool selection is verifiable here rather than
 * taken on trust: the plan the agent wrote before acting, each step and
 * whether it succeeded, and every tool call with the **exact arguments the
 * model chose**.
 *
 * Those arguments are the direct evidence. A system with hardcoded routing
 * would show the same tools with the same shape of arguments every run.
 */

import Link from "next/link";
import { use, useEffect, useState } from "react";

import { AuthGate } from "@/components/AuthGate";
import { Badge, Card, ErrorBanner, Nav, PageHeader } from "@/components/ui";
import { ApiError, api, type RunTrace } from "@/lib/api";

export default function RunTracePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return <AuthGate>{(session) => <Trace runId={id} session={session} />}</AuthGate>;
}

function Trace({
  runId,
  session,
}: {
  runId: string;
  session: { email: string | null; isAdmin: boolean };
}) {
  const [trace, setTrace] = useState<RunTrace | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      try {
        setTrace(await api.admin.getRun(runId));
      } catch (caught) {
        setError(caught instanceof ApiError ? caught.message : "Could not load the trace.");
      }
    }
    void load();
  }, [runId]);

  return (
    <div className="mx-auto min-h-screen max-w-4xl px-4 py-6">
      <div className="mb-6 flex items-center justify-between">
        <Link href="/admin" className="text-sm text-[var(--color-accent)]">
          ← Admin
        </Link>
        <Nav email={session.email} isAdmin={session.isAdmin} />
      </div>

      <PageHeader
        title="Execution trace"
        subtitle={`Run ${runId.slice(0, 8)} — what the agent planned, did, and called.`}
      />

      {error && (
        <div className="mt-4">
          <ErrorBanner message={error} />
        </div>
      )}
      {!trace && !error && (
        <p className="mt-8 text-sm text-[var(--color-ink-soft)]">Loading…</p>
      )}

      {trace && (
        <div className="mt-6 space-y-8">
          <section className="flex flex-wrap gap-2 text-xs">
            <Badge tone={trace.status === "completed" ? "good" : "warn"}>
              {trace.status}
            </Badge>
            <Badge>{trace.steps.length} steps</Badge>
            <Badge>{trace.tool_calls.length} tool calls</Badge>
            {trace.replan_count > 0 && (
              <Badge tone="warn">{trace.replan_count} replans</Badge>
            )}
            {trace.rag_hops > 0 && <Badge>{trace.rag_hops} RAG hops</Badge>}
            {trace.detected_language && <Badge>{trace.detected_language}</Badge>}
            <Badge>{((trace.latency_ms ?? 0) / 1000).toFixed(1)}s</Badge>
          </section>

          {trace.initial_plan && trace.initial_plan.length > 0 && (
            <section>
              <h2 className="mb-1 text-sm font-semibold">Plan as first written</h2>
              <p className="mb-3 text-xs text-[var(--color-ink-soft)]">
                Produced before any tool ran. Compare with the steps below to see
                whether the replanner changed course.
              </p>
              <Card className="p-4">
                <ol className="space-y-1.5 text-sm">
                  {trace.initial_plan.map((step, index) => (
                    <li key={index} className="flex gap-2">
                      <span className="text-[var(--color-ink-soft)]">{index + 1}.</span>
                      <span className="flex-1">{step.description}</span>
                      <Badge>{step.kind}</Badge>
                    </li>
                  ))}
                </ol>
              </Card>
            </section>
          )}

          <section>
            <h2 className="mb-3 text-sm font-semibold">Steps executed</h2>
            <div className="space-y-2">
              {trace.steps.map((step, index) => (
                <Card key={index} className="p-4">
                  <div className="flex items-start justify-between gap-3">
                    <p className="text-sm font-medium">
                      {step.step_index + 1}. {step.description}
                    </p>
                    <div className="flex shrink-0 items-center gap-2">
                      {step.replan_cycle > 0 && (
                        <Badge tone="warn">added in replan {step.replan_cycle}</Badge>
                      )}
                      <Badge tone={step.status === "completed" ? "good" : "bad"}>
                        {step.status}
                      </Badge>
                      <span className="text-xs text-[var(--color-ink-soft)]">
                        {step.latency_ms}ms
                      </span>
                    </div>
                  </div>
                  {step.result_summary && (
                    <p className="mt-2 whitespace-pre-wrap text-xs text-[var(--color-ink-soft)]">
                      {step.result_summary.slice(0, 600)}
                    </p>
                  )}
                  {step.error_message && (
                    <p className="mt-2 text-xs text-[var(--color-danger)]">
                      {step.error_message}
                    </p>
                  )}
                </Card>
              ))}
            </div>
          </section>

          <section>
            <h2 className="mb-1 text-sm font-semibold">Tool calls</h2>
            <p className="mb-3 text-xs text-[var(--color-ink-soft)]">
              The arguments below were chosen by the model. Nothing in this system
              inspects the message for keywords and routes to a tool.
            </p>
            <div className="space-y-2">
              {trace.tool_calls.map((call, index) => (
                <Card key={index} className="p-4">
                  <div className="flex items-center justify-between gap-3">
                    <code className="font-mono text-sm">{call.tool_name}</code>
                    <div className="flex items-center gap-2">
                      {call.degraded && <Badge tone="warn">source unavailable</Badge>}
                      {!call.succeeded && !call.degraded && (
                        <Badge tone="bad">invalid arguments</Badge>
                      )}
                      <span className="text-xs text-[var(--color-ink-soft)]">
                        {call.latency_ms}ms
                      </span>
                    </div>
                  </div>
                  <pre className="mt-2 overflow-x-auto rounded-lg bg-[var(--color-line)]/30 p-3 font-mono text-xs">
                    {JSON.stringify(call.arguments, null, 2)}
                  </pre>
                  {call.result_summary && (
                    <p className="mt-2 line-clamp-3 text-xs text-[var(--color-ink-soft)]">
                      {call.result_summary.slice(0, 300)}
                    </p>
                  )}
                  {call.error_message && (
                    <p className="mt-2 text-xs text-[var(--color-danger)]">
                      {call.error_message}
                    </p>
                  )}
                </Card>
              ))}
              {trace.tool_calls.length === 0 && (
                <p className="text-sm text-[var(--color-ink-soft)]">
                  No tools were called — the agent answered from context alone.
                </p>
              )}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
