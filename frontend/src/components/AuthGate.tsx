"use client";

/**
 * Client-side auth gate.
 *
 * Renders children only for a signed-in user, redirecting to `/login`
 * otherwise, and exposes the session to the page.
 *
 * This is a **convenience, not a security boundary**. Anything a browser
 * enforces can be bypassed by the browser. Real enforcement is the API's JWT
 * verification and the database's row level security policies - both of which
 * would reject an unauthenticated request regardless of what this component
 * decides to render. It exists so users see a sensible page, not so data stays
 * private.
 */

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { getSupabase } from "@/lib/supabase";

export interface SessionInfo {
  userId: string;
  email: string | null;
  isAdmin: boolean;
}

export function useSession(): { session: SessionInfo | null; loading: boolean } {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const supabase = getSupabase();

    async function load(): Promise<void> {
      const {
        data: { session: authSession },
      } = await supabase.auth.getSession();

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
      });
      setLoading(false);
    }

    void load();

    // Keeps the UI in step with sign-out in another tab, and with the
    // background token refresh.
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange(() => void load());

    return () => subscription.unsubscribe();
  }, []);

  return { session, loading };
}

export function AuthGate({
  children,
}: {
  children: (session: SessionInfo) => React.ReactNode;
}) {
  const { session, loading } = useSession();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !session) router.replace("/login");
  }, [loading, session, router]);

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p className="text-sm text-[var(--color-ink-soft)]">Loading…</p>
      </div>
    );
  }

  if (!session) return null;

  return <>{children(session)}</>;
}
