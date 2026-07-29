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
