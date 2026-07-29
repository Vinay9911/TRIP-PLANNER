"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button, Card, ErrorBanner } from "@/components/ui";
import {
  signInWithGoogle,
  signInWithPassword,
  signUpWithPassword,
} from "@/lib/supabase";

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setNotice(null);
    setBusy(true);

    try {
      if (mode === "signup") {
        const { error: signUpError } = await signUpWithPassword(email, password);
        if (signUpError) throw signUpError;
        // Supabase may require email confirmation depending on project
        // settings, so we tell the user rather than silently doing nothing.
        setNotice(
          "Account created. If your project requires email confirmation, check your inbox before signing in.",
        );
        setMode("signin");
      } else {
        const { error: signInError } = await signInWithPassword(email, password);
        if (signInError) throw signInError;
        router.replace("/");
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <h1 className="text-2xl font-semibold tracking-tight">Trip Planner</h1>
          <p className="mt-2 text-sm text-[var(--color-ink-soft)]">
            An AI travel planner that remembers what matters to you.
          </p>
        </div>

        <Card className="p-6">
          <div className="mb-5 flex gap-1 rounded-lg bg-[var(--color-line)]/40 p-1">
            {(["signin", "signup"] as const).map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => {
                  setMode(option);
                  setError(null);
                }}
                className={`flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                  mode === option
                    ? "bg-[var(--color-surface)] shadow-sm"
                    : "text-[var(--color-ink-soft)]"
                }`}
              >
                {option === "signin" ? "Sign in" : "Create account"}
              </button>
            ))}
          </div>

          {error && (
            <div className="mb-4">
              <ErrorBanner message={error} />
            </div>
          )}
          {notice && (
            <p className="mb-4 rounded-lg bg-[var(--color-accent-soft)] px-4 py-3 text-sm text-[var(--color-accent)]">
              {notice}
            </p>
          )}

          <form onSubmit={handleSubmit} className="space-y-3">
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-[var(--color-ink-soft)]">
                Email
              </span>
              <input
                type="email"
                required
                autoComplete="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                className="w-full rounded-lg border border-[var(--color-line)] bg-transparent px-3 py-2 text-sm"
              />
            </label>

            <label className="block">
              <span className="mb-1 block text-xs font-medium text-[var(--color-ink-soft)]">
                Password
              </span>
              <input
                type="password"
                required
                minLength={6}
                autoComplete={mode === "signup" ? "new-password" : "current-password"}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className="w-full rounded-lg border border-[var(--color-line)] bg-transparent px-3 py-2 text-sm"
              />
            </label>

            <Button type="submit" disabled={busy} className="w-full">
              {busy ? "Please wait…" : mode === "signin" ? "Sign in" : "Create account"}
            </Button>
          </form>

          <div className="my-5 flex items-center gap-3">
            <div className="h-px flex-1 bg-[var(--color-line)]" />
            <span className="text-xs text-[var(--color-ink-soft)]">or</span>
            <div className="h-px flex-1 bg-[var(--color-line)]" />
          </div>

          <Button
            variant="secondary"
            className="w-full"
            onClick={() => void signInWithGoogle()}
          >
            Continue with Google
          </Button>
        </Card>

        <p className="mt-6 text-center text-xs text-[var(--color-ink-soft)]">
          Your preferences are stored so future trips do not need repeating. You
          can see and delete everything at any time.
        </p>
      </div>
    </main>
  );
}
