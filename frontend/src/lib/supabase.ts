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
 * Return the current session, creating an anonymous one if there is none.
 *
 * **This is what replaced the login wall**, and the choice of mechanism is the
 * whole point. The obvious way to "remove login" is to drop authentication and
 * hardcode a user id, which would have quietly destroyed three things that
 * work today: the API's JWT verification, the database's row level security,
 * and per-traveller memory isolation. Every visitor would have shared one
 * profile, so one person's dietary requirement would surface in another
 * person's itinerary.
 *
 * Supabase anonymous sign-in issues a *real* JWT with a real `sub` UUID, so
 * none of that changes - the backend cannot tell an anonymous caller from a
 * registered one, and neither can RLS. The only difference is that nobody had
 * to type anything.
 *
 * The session persists in local storage, which is what makes long-term memory
 * demonstrable: close the tab, come back tomorrow, and the agent still knows
 * you are vegetarian, because it is still the same `sub`.
 *
 * Requires "Allow anonymous sign-ins" to be enabled under Authentication ->
 * Sign In / Providers in the Supabase dashboard. It is off by default, and the
 * failure is a clear `anonymous_provider_disabled` error rather than a hang.
 */
export async function ensureSession() {
  const supabase = getSupabase();

  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (session?.user) return session;

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
