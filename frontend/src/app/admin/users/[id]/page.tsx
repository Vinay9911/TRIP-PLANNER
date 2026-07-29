"use client";

/**
 * One user's profile, conversations and stored memories.
 *
 * The view the brief asked for: which user, what they talked about, and what
 * the system has learned about them.
 *
 * Superseded memories are shown alongside active ones because the evolution of
 * a profile is the most useful thing here when diagnosing an extractor that
 * has learned something wrong — you can see what it believed, when that
 * changed, and what replaced it.
 *
 * Opening this page writes an entry to the admin audit log naming the user
 * whose data was viewed.
 */

import Link from "next/link";
import { use, useEffect, useState } from "react";

import { AuthGate } from "@/components/AuthGate";
import {
  Badge,
  Card,
  EmptyState,
  ErrorBanner,
  Nav,
  PageHeader,
} from "@/components/ui";
import {
  ApiError,
  api,
  type AdminUser,
  type Memory,
  type SessionSummary,
  type StoredMessage,
} from "@/lib/api";

export default function AdminUserPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  return <AuthGate>{(session) => <UserDetail userId={id} session={session} />}</AuthGate>;
}

function UserDetail({
  userId,
  session,
}: {
  userId: string;
  session: { email: string | null; isAdmin: boolean };
}) {
  const [detail, setDetail] = useState<{
    profile: AdminUser;
    sessions: SessionSummary[];
    memories: Memory[];
  } | null>(null);
  const [openSession, setOpenSession] = useState<string | null>(null);
  const [messages, setMessages] = useState<StoredMessage[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      try {
        setDetail(await api.admin.getUser(userId));
      } catch (caught) {
        setError(caught instanceof ApiError ? caught.message : "Could not load the user.");
      }
    }
    void load();
  }, [userId]);

  async function openTranscript(sessionId: string) {
    if (openSession === sessionId) {
      setOpenSession(null);
      setMessages([]);
      return;
    }
    try {
      setMessages(await api.admin.getSessionMessages(sessionId));
      setOpenSession(sessionId);
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : "Could not load the transcript.",
      );
    }
  }

  const active = detail?.memories.filter((memory) => memory.status === "active") ?? [];
  const retired = detail?.memories.filter((memory) => memory.status !== "active") ?? [];

  return (
    <div className="mx-auto min-h-screen max-w-4xl px-4 py-6">
      <div className="mb-6 flex items-center justify-between">
        <Link href="/admin" className="text-sm text-[var(--color-accent)]">
          ← Admin
        </Link>
        <Nav email={session.email} isAdmin={session.isAdmin} />
      </div>

      {error && <ErrorBanner message={error} />}
      {!detail && !error && (
        <p className="text-sm text-[var(--color-ink-soft)]">Loading…</p>
      )}

      {detail && (
        <>
          <PageHeader
            title={detail.profile.email ?? detail.profile.display_name ?? "User"}
            subtitle={`${detail.sessions.length} conversations · ${active.length} active memories`}
            actions={
              detail.profile.app_role === "admin" ? (
                <Badge tone="accent">admin</Badge>
              ) : undefined
            }
          />

          <div className="mt-6 space-y-8">
            <section>
              <h2 className="mb-1 text-sm font-semibold">Long-term memory</h2>
              <p className="mb-3 text-xs text-[var(--color-ink-soft)]">
                Each row is one atomic fact with its provenance. Mention count
                shows reinforcement — the same fact restated is counted, not
                duplicated.
              </p>
              {active.length === 0 ? (
                <EmptyState title="Nothing stored for this user yet" />
              ) : (
                <div className="space-y-2">
                  {active.map((memory) => (
                    <Card key={memory.id} className="px-4 py-3">
                      <p className="text-sm">{memory.content}</p>
                      <div className="mt-1.5 flex flex-wrap gap-2 text-xs text-[var(--color-ink-soft)]">
                        <Badge
                          tone={memory.memory_type === "constraint" ? "accent" : "neutral"}
                        >
                          {memory.memory_type}
                        </Badge>
                        <span>{memory.subject}</span>
                        <span>{memory.mention_count}× mentioned</span>
                        <span>{Math.round(memory.confidence * 100)}% confident</span>
                        {memory.source_lang && <span>from {memory.source_lang}</span>}
                        {memory.last_seen_at && (
                          <span>
                            last {new Date(memory.last_seen_at).toLocaleDateString()}
                          </span>
                        )}
                      </div>
                    </Card>
                  ))}
                </div>
              )}

              {retired.length > 0 && (
                <div className="mt-4">
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-[var(--color-ink-soft)]">
                    Superseded ({retired.length})
                  </h3>
                  <ul className="space-y-1">
                    {retired.map((memory) => (
                      <li
                        key={memory.id}
                        className="rounded-lg border border-dashed border-[var(--color-line)] px-3 py-2 text-xs text-[var(--color-ink-soft)]"
                      >
                        <span className="line-through">{memory.content}</span>
                        <span className="ml-2">({memory.status})</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </section>

            <section>
              <h2 className="mb-3 text-sm font-semibold">Conversations</h2>
              {detail.sessions.length === 0 ? (
                <EmptyState title="No conversations yet" />
              ) : (
                <div className="space-y-2">
                  {detail.sessions.map((conversation) => (
                    <Card key={conversation.id} className="p-4">
                      <button
                        type="button"
                        onClick={() => void openTranscript(conversation.id)}
                        className="flex w-full items-center justify-between gap-3 text-left"
                      >
                        <span className="min-w-0 flex-1 truncate text-sm">
                          {conversation.title ?? "Untitled"}
                        </span>
                        <span className="flex shrink-0 items-center gap-2 text-xs text-[var(--color-ink-soft)]">
                          {conversation.destination && (
                            <Badge>{conversation.destination}</Badge>
                          )}
                          {conversation.message_count} messages
                          <span className="text-[var(--color-accent)]">
                            {openSession === conversation.id ? "hide" : "read"}
                          </span>
                        </span>
                      </button>

                      {openSession === conversation.id && (
                        <div className="mt-3 space-y-2 border-t border-[var(--color-line)] pt-3">
                          {messages.map((message) => (
                            <div key={message.id} className="text-sm">
                              <span className="text-xs font-medium text-[var(--color-ink-soft)]">
                                {message.role === "user" ? "Traveller" : "Assistant"}
                              </span>
                              <p className="whitespace-pre-wrap">{message.content}</p>
                            </div>
                          ))}
                        </div>
                      )}
                    </Card>
                  ))}
                </div>
              )}
            </section>
          </div>
        </>
      )}
    </div>
  );
}
