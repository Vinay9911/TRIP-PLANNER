"use client";

/**
 * The application shell: one persistent sidebar, every page inside it.
 *
 * This exists because of a concrete complaint - moving between Chat, What it
 * remembers and Admin visibly re-laid-out the page each time. Each page had
 * been rendering its own header with its own container width and its own
 * spacing, so the navigation appeared to jump between routes. The fix is
 * structural rather than cosmetic: navigation is rendered once, here, and
 * every page supplies only its own content.
 *
 * It also carries **conversation history**, which previously had no interface
 * at all. Sessions were being saved correctly the whole time - the API and
 * database were fine - but nothing in the UI ever listed them, so every visit
 * looked like a blank slate and past trips were unreachable. Selecting one
 * pushes `?session=<id>`, which means a conversation is now a shareable,
 * reloadable URL rather than transient client state.
 *
 * Layout follows the adaptive-navigation rule: a permanent sidebar from the
 * `lg` breakpoint up, and a slide-over drawer below it, rather than trying to
 * squeeze a rail onto a phone.
 */

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";

import {
  IconBrain,
  IconChart,
  IconChat,
  IconClose,
  IconCompass,
  IconLogout,
  IconMenu,
  IconPlus,
  IconShield,
  IconTrash,
} from "@/components/icons";
import { ApiError, api, type SessionSummary } from "@/lib/api";
import { signOut } from "@/lib/supabase";

const NAV = [
  { href: "/chat", label: "Chat", icon: IconChat },
  { href: "/dashboard", label: "Your travel", icon: IconChart },
  { href: "/memories", label: "What it remembers", icon: IconBrain },
] as const;

/**
 * Reads `?session=` so the matching history entry can be highlighted.
 *
 * Split out and wrapped in Suspense *inside* this file rather than left to
 * each page: `useSearchParams` opts a route into client-side rendering unless
 * it sits under a Suspense boundary, and a shell used by every page should
 * not silently impose that on all of them. Discovered when a page that
 * rendered the shell outside an existing boundary failed to prerender.
 */
function useActiveSessionId(): string | null {
  const searchParams = useSearchParams();
  return searchParams.get("session");
}

function HighlightedHistory({
  render,
}: {
  render: (activeSession: string | null) => React.ReactNode;
}) {
  const activeSession = useActiveSessionId();
  return <>{render(activeSession)}</>;
}

export function AppShell({
  session,
  children,
  /** Bumping this makes the sidebar re-fetch its session list - the chat page
   *  raises it after a reply, so a brand-new conversation appears in history
   *  without a page reload. */
  historyToken = 0,
}: {
  session: { email: string | null; isAdmin: boolean };
  children: React.ReactNode;
  historyToken?: number;
}) {
  const pathname = usePathname();
  const router = useRouter();

  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const loadHistory = useCallback(async () => {
    try {
      setSessions(await api.listSessions());
    } catch {
      // History is a convenience, not the point of the page: a failure here
      // must not block someone from starting a new conversation.
      setSessions([]);
    } finally {
      setLoadingHistory(false);
    }
  }, []);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory, historyToken]);

  // Close the mobile drawer on navigation, otherwise it stays open over the
  // page the user just asked for.
  useEffect(() => {
    setDrawerOpen(false);
  }, [pathname]);

  async function startFresh() {
    if (
      !window.confirm(
        "This permanently erases everything remembered about you and deletes your " +
          "conversations. Your account stays. Continue?",
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      await api.eraseMyData();
      await loadHistory();
      router.push("/chat");
      router.refresh();
    } catch (caught) {
      window.alert(
        caught instanceof ApiError ? caught.message : "Could not erase your data.",
      );
    } finally {
      setBusy(false);
    }
  }

  const nav = [
    ...NAV,
    ...(session.isAdmin ? [{ href: "/admin", label: "Admin", icon: IconShield } as const] : []),
  ];

  const sidebar = (
    <div className="flex h-full flex-col gap-4 p-4">
      <Link href="/" className="flex items-center gap-2.5 px-1 py-1">
        <span className="grid h-9 w-9 place-items-center rounded-xl bg-[var(--color-brand)] text-white">
          <IconCompass />
        </span>
        <span className="font-display text-[15px] font-semibold tracking-tight">
          Trip Planner
        </span>
      </Link>

      <Link
        href="/chat"
        onClick={() => router.push("/chat")}
        className="flex min-h-11 items-center justify-center gap-2 rounded-xl bg-[var(--color-brand-strong)] px-4 text-sm font-medium text-white transition-opacity duration-200 hover:opacity-90"
      >
        <IconPlus size="1.1em" />
        New chat
      </Link>

      <nav className="flex flex-col gap-1" aria-label="Main">
        {nav.map((item) => {
          const active = pathname === item.href;
          const Glyph = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={active ? "page" : undefined}
              className={`flex min-h-11 items-center gap-2.5 rounded-xl px-3 text-sm transition-colors duration-200 ${
                active
                  ? "bg-[var(--color-brand-soft)] font-medium text-[var(--color-brand-strong)]"
                  : "text-[var(--color-ink-soft)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-ink)]"
              }`}
            >
              <Glyph size="1.15em" />
              {item.label}
            </Link>
          );
        })}
      </nav>

      <div className="min-h-0 flex-1">
        <p className="px-3 pb-2 pt-3 text-[11px] font-semibold uppercase tracking-wide text-[var(--color-ink-faint)]">
          Recent trips
        </p>

        <div className="scroll-slim h-full max-h-[calc(100dvh-24rem)] overflow-y-auto pr-1">
          {loadingHistory ? (
            <div className="space-y-1.5 px-2">
              {[0, 1, 2].map((n) => (
                <div key={n} className="skeleton h-9 rounded-lg" />
              ))}
            </div>
          ) : sessions.length === 0 ? (
            <p className="px-3 text-xs leading-relaxed text-[var(--color-ink-faint)]">
              Your conversations will appear here once you start planning.
            </p>
          ) : (
            <Suspense fallback={null}>
              <HighlightedHistory
                render={(activeSession) => (
                  <ul className="space-y-0.5">
                    {sessions.map((item) => {
                      const active = activeSession === item.id;
                      return (
                        <li key={item.id}>
                          <div
                            className={`group relative flex items-center justify-between rounded-lg transition-colors duration-200 ${
                              active
                                ? "bg-[var(--color-surface-2)] font-medium text-[var(--color-ink)]"
                                : "text-[var(--color-ink-soft)] hover:bg-[var(--color-surface-2)]"
                            }`}
                          >
                            <Link
                              href={`/chat?session=${item.id}`}
                              aria-current={active ? "true" : undefined}
                              className="flex min-h-11 flex-1 flex-col justify-center overflow-hidden px-3 py-1.5 text-xs"
                            >
                              <span className="truncate">{item.title || "Untitled trip"}</span>
                              {item.destination && (
                                <span className="truncate text-[10px] text-[var(--color-ink-faint)]">
                                  {item.destination}
                                </span>
                              )}
                            </Link>
                            <button
                              onClick={async (e) => {
                                e.preventDefault();
                                if (window.confirm("Delete this trip?")) {
                                  try {
                                    await api.deleteSession(item.id);
                                    if (active) router.push("/chat");
                                    await loadHistory();
                                  } catch (err) {
                                    window.alert("Could not delete trip.");
                                  }
                                }
                              }}
                              className="mr-2 rounded p-1.5 text-[var(--color-ink-faint)] opacity-0 transition-opacity hover:bg-[var(--color-surface)] hover:text-red-500 group-hover:opacity-100"
                              title="Delete this trip"
                            >
                              <IconTrash size="1.1em" />
                            </button>
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                )}
              />
            </Suspense>
          )}
        </div>
      </div>

      {/* Account actions, kept visually separated from navigation - the two
          destructive ones especially, so neither is a mis-tap away from a
          nav item. */}
      <div className="border-t border-[var(--color-line)] pt-3">
        {session.email && (
          <p className="truncate px-3 pb-2 text-[11px] text-[var(--color-ink-faint)]">
            {session.email}
          </p>
        )}
        <button
          type="button"
          onClick={() => void startFresh()}
          disabled={busy}
          className="flex min-h-11 w-full items-center gap-2.5 rounded-xl px-3 text-sm text-[var(--color-ink-soft)] transition-colors duration-200 hover:bg-[var(--color-surface-2)] hover:text-[var(--color-danger)] disabled:opacity-50"
        >
          <IconTrash size="1.1em" />
          {busy ? "Erasing…" : "Start fresh"}
        </button>
        <button
          type="button"
          onClick={() => void signOut()}
          className="flex min-h-11 w-full items-center gap-2.5 rounded-xl px-3 text-sm text-[var(--color-ink-soft)] transition-colors duration-200 hover:bg-[var(--color-surface-2)] hover:text-[var(--color-ink)]"
        >
          <IconLogout size="1.1em" />
          Sign out
        </button>
      </div>
    </div>
  );

  return (
    <div className="flex min-h-dvh">
      {/* Permanent rail from lg up. */}
      <aside className="sticky top-0 hidden h-dvh w-64 shrink-0 border-r border-[var(--color-line)] bg-[var(--color-surface)]/80 backdrop-blur lg:block">
        {sidebar}
      </aside>

      {/* Slide-over drawer below lg. */}
      {drawerOpen && (
        <div className="fixed inset-0 z-50 lg:hidden">
          {/* A scrim heavy enough to actually separate foreground from
              background, per the modal-legibility rule. */}
          <button
            type="button"
            aria-label="Close menu"
            onClick={() => setDrawerOpen(false)}
            className="absolute inset-0 bg-black/45"
          />
          <div className="absolute inset-y-0 left-0 w-72 max-w-[85vw] bg-[var(--color-surface)] shadow-xl">
            <button
              type="button"
              onClick={() => setDrawerOpen(false)}
              aria-label="Close menu"
              className="absolute right-2 top-3 grid h-11 w-11 place-items-center rounded-xl text-[var(--color-ink-soft)]"
            >
              <IconClose />
            </button>
            {sidebar}
          </div>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex min-h-14 items-center gap-2 border-b border-[var(--color-line)] bg-[var(--color-paper)]/85 px-3 backdrop-blur lg:hidden">
          <button
            type="button"
            onClick={() => setDrawerOpen(true)}
            aria-label="Open menu"
            className="grid h-11 w-11 place-items-center rounded-xl text-[var(--color-ink-soft)] hover:bg-[var(--color-surface-2)]"
          >
            <IconMenu />
          </button>
          <span className="font-display text-sm font-semibold">Trip Planner</span>
        </header>

        <main className="min-w-0 flex-1">{children}</main>
      </div>
    </div>
  );
}
