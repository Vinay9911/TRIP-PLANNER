/**
 * Supabase browser client.
 *
 * Only the anon key is used here. It is designed to be public: every table it
 * can reach is protected by row level security policies, so possessing the key
 * grants nothing beyond what the signed-in user is already entitled to. The
 * service_role key, which bypasses RLS, exists only on the backend and must
 * never appear in a browser bundle.
 *
 * Auth is handled entirely by Supabase - email/password and Google OAuth. The
 * FastAPI backend never sees a password; it only verifies the signed token
 * Supabase issues.
 */

import { createBrowserClient } from "@supabase/ssr";
import type { SupabaseClient } from "@supabase/supabase-js";

let client: SupabaseClient | null = null;

/**
 * Return the shared browser Supabase client.
 *
 * Memoised because each instance registers its own auth state listener and
 * token-refresh timer; creating one per component would multiply both.
 */
export function getSupabase(): SupabaseClient {
  if (client) return client;

  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const anonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

  if (!url || !anonKey) {
    throw new Error(
      "NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY must be set. " +
        "Copy .env.example to .env.local and fill them in.",
    );
  }

  client = createBrowserClient(url, anonKey);
  return client;
}

/**
 * Return the current session, signing in automatically if there is none.
 *
 * **Everyone who opens this deployment lands in the same workspace**, and that
 * is the point rather than an oversight. This is a demonstration: the trips,
 * the remembered preferences and the execution traces are the thing being
 * shown, so a reviewer arriving at the link needs to find them already there.
 * A per-visitor identity would give each of them a blank slate and nothing to
 * look at - which is exactly what the first version did.
 *
 * So a single shared account is signed into with credentials that ship in the
 * browser bundle. Two things make that defensible rather than careless:
 *
 * - **Nothing is being protected.** There is one account, so "another user's
 *   data" does not exist. The credentials grant exactly what the interface
 *   already offers anyone who opens the page.
 * - **The security machinery is untouched.** This is a real Supabase sign-in
 *   returning a real signed JWT, so the API still verifies every request and
 *   row level security still scopes every query to `auth.uid()`. Had the login
 *   been removed by hardcoding a user id server-side, all of that would have
 *   been bypassed instead of satisfied.
 *
 * For anything with real users this is wrong and the fallback below is right:
 * with no demo credentials configured, each visitor gets their own anonymous
 * identity and sees only their own trips.
 *
 * Signing in is idempotent, which also fixes a real problem the anonymous path
 * had: React StrictMode invokes effects twice in development, and two
 * `signInAnonymously()` calls create two accounts. Two duplicate users were
 * observed 1.3ms apart before this changed.
 */
export async function ensureSession() {
  const supabase = getSupabase();

  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (session?.user) return session;

  const email = process.env.NEXT_PUBLIC_DEMO_EMAIL;
  const password = process.env.NEXT_PUBLIC_DEMO_PASSWORD;

  if (email && password) {
    const { data, error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) throw error;
    return data.session;
  }

  // No shared account configured: fall back to a private identity per visitor.
  // Requires "Allow anonymous sign-ins" under Authentication -> Sign In /
  // Providers, which is off by default in new Supabase projects; the failure
  // is a clear `anonymous_provider_disabled` error rather than a hang.
  const { data, error } = await supabase.auth.signInAnonymously();
  if (error) throw error;
  return data.session;
}

/** Sign in with an email and password. */
export async function signInWithPassword(email: string, password: string) {
  return getSupabase().auth.signInWithPassword({ email, password });
}

/** Create an account with an email and password. */
export async function signUpWithPassword(email: string, password: string) {
  return getSupabase().auth.signUp({
    email,
    password,
    options: { emailRedirectTo: `${window.location.origin}/login` },
  });
}

/** Begin the Google OAuth flow. */
export async function signInWithGoogle() {
  return getSupabase().auth.signInWithOAuth({
    provider: "google",
    options: { redirectTo: `${window.location.origin}/` },
  });
}

/** Sign out and clear the local session. */
export async function signOut() {
  return getSupabase().auth.signOut();
}
