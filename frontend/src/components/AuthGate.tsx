"use client";

/**
 * Session provider for the application pages.
 *
 * There is no login wall. Opening the planner provisions an anonymous Supabase
 * session on the spot (see `ensureSession`), so a visitor goes from the landing
 * page to a working agent without an account, a password or an email.
 *
 * **What this is not is a downgrade in security.** The anonymous session is a
 * genuine Supabase identity carrying a signed JWT, so the API still verifies
 * every request and the database still applies row level security against a
 * real `auth.uid()`. This component decides what to *render*; it has never been
 * what keeps one traveller's data away from another, and that job still belongs
 * to the token verification in `core/security.py` and the policies in migration
 * 0007.
 *
 * Signing in with an email is still possible at `/login` - it is simply no
 * longer required. That route exists so an administrator can reach the trace
 * dashboard, which needs a profile the service role has marked as admin.
 */

import { useEffect, useState } from "react";

import { ensureSession, getSupabase } from "@/lib/supabase";

export interface SessionInfo {
  userId: string;
  email: string | null;
  isAdmin: boolean;
  /** True for a visitor who never signed in. Drives the account UI wording:
   *  offering "Sign out" to someone who never signed in is nonsense, and
   *  would silently discard everything the agent remembers about them. */
  isAnonymous: boolean;
}

export function useSession(): { session: SessionInfo | null; loading: boolean; error: string | null } {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const supabase = getSupabase();

    async function load(): Promise<void> {
      try {
        const authSession = await ensureSession();

        if (!authSession?.user) {
          setSession(null);
          setLoading(false);
          return;
        }

        // The admin flag is read from `profiles`, not from a token claim -
        // token claims are not authoritative for authorization. This copy is
        // only used to decide whether to show the Admin link; the API checks
        // the role again on every admin request.
        const { data: profile } = await supabase
          .from("profiles")
          .select("app_role")
          .eq("id", authSession.user.id)
          .single();

        setSession({
          userId: authSession.user.id,
          email: authSession.user.email ?? null,
          isAdmin: profile?.app_role === "admin",
          isAnonymous: authSession.user.is_anonymous === true,
        });
        setError(null);
      } catch (caught) {
        // Almost always one thing: anonymous sign-ins are still switched off
        // in the Supabase dashboard. Said plainly, because the alternative is
        // a blank screen that gives no clue what to fix.
        setError(
          caught instanceof Error
            ? caught.message
            : "Could not start a session.",
        );
      } finally {
        setLoading(false);
      }
    }

    void load();

    // Keeps the UI in step with sign-in from another tab, and with the
    // background token refresh.
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange(() => void load());

    return () => subscription.unsubscribe();
  }, []);

  return { session, loading, error };
}

export function AuthGate({
  children,
}: {
  children: (session: SessionInfo) => React.ReactNode;
}) {
  const { session, loading, error } = useSession();

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p className="text-sm text-[var(--color-ink-soft)]">
          Getting your workspace ready…
        </p>
      </div>
    );
  }

  if (error || !session) {
    return (
      <div className="flex min-h-screen items-center justify-center px-6">
        <div className="max-w-md space-y-3 text-center">
          <p className="font-display text-lg font-semibold text-[var(--color-ink)]">
            Could not start a session
          </p>
          <p className="text-sm text-[var(--color-ink-soft)]">
            {error ?? "Please reload the page and try again."}
          </p>
          <p className="text-xs text-[var(--color-ink-faint)]">
            If this persists, enable anonymous sign-ins in the Supabase
            dashboard under Authentication → Sign In / Providers.
          </p>
        </div>
      </div>
    );
  }

  return <>{children(session)}</>;
}
