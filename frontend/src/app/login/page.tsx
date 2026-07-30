"use client";

/**
 * Sign in / create account.
 *
 * A split panel: the animated dotted world on the left, the form on the
 * right, collapsing to just the form on small screens. The layout follows the
 * reference the client supplied; the palette and the dependency budget do
 * not. That design pulled in `framer-motion` for entrance fades and
 * `lucide-react` for a single arrow - both avoidable, since the entrances are
 * CSS and the icons already exist in this project.
 *
 * The password field has a show/hide toggle and correct `autocomplete`
 * values, so browser and password-manager autofill work rather than being
 * quietly broken by custom markup.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";

import { DotGlobe } from "@/components/DotGlobe";
import { IconChevron, IconCompass, IconSparkle } from "@/components/icons";
import { Button, ErrorBanner } from "@/components/ui";
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
  const [showPassword, setShowPassword] = useState(false);
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
        router.replace("/chat");
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-dvh items-center justify-center px-4 py-10">
      <div className="rise-in grid w-full max-w-4xl overflow-hidden rounded-3xl border border-[var(--color-line)] bg-[var(--color-surface)] shadow-[0_30px_80px_-40px_rgb(44_31_43_/_0.45)] md:grid-cols-2">
        {/* -- The animated half. Hidden on phones, where it would push the
             form below the fold for no informational gain. -------------- */}
        <div className="relative hidden min-h-[560px] overflow-hidden bg-gradient-to-br from-[var(--color-brand-soft)] via-[var(--color-rose-soft)]/50 to-[var(--color-grape-soft)] md:block">
          <DotGlobe className="absolute inset-0" />

          {/* Positioned absolutely rather than with `h-full`: the panel's
              height comes from the form beside it, and a percentage height
              against an implicitly-sized grid cell left this block sitting
              low and clipping its own list. */}
          <div className="absolute inset-0 z-10 flex flex-col items-center justify-center overflow-hidden p-8 text-center">
            <span className="grid h-14 w-14 place-items-center rounded-2xl bg-[var(--color-brand)] text-white shadow-lg shadow-[var(--color-brand)]/30">
              <IconCompass size="1.7em" />
            </span>
            <h2 className="mt-4 font-display text-2xl font-semibold tracking-tight">
              <span className="bg-gradient-to-r from-[var(--color-brand-strong)] to-[var(--color-grape)] bg-clip-text text-transparent">
                Trip Planner
              </span>
            </h2>
            <p className="mt-2 max-w-xs text-sm leading-relaxed text-[var(--color-ink-soft)]">
              Real guides, real places, and a planner that remembers what
              matters to you.
            </p>

            <ul className="mt-6 space-y-2 text-left">
              {[
                "Researches actual travel guides",
                "Asks only what changes the plan",
                "Remembers your preferences",
              ].map((line) => (
                <li key={line} className="flex items-center gap-2 text-xs">
                  <span className="text-[var(--color-brand-strong)]">
                    <IconSparkle size="0.9em" />
                  </span>
                  {line}
                </li>
              ))}
            </ul>
          </div>
        </div>

        {/* -- The form half ---------------------------------------------- */}
        <div className="flex flex-col justify-center p-7 sm:p-10">
          <div className="mb-6 flex items-center gap-2.5 md:hidden">
            <span className="grid h-10 w-10 place-items-center rounded-xl bg-[var(--color-brand)] text-white">
              <IconCompass size="1.2em" />
            </span>
            <span className="font-display text-base font-semibold">Trip Planner</span>
          </div>

          <h1 className="font-display text-2xl font-semibold tracking-tight">
            {mode === "signin" ? "Welcome back" : "Create your account"}
          </h1>
          <p className="mt-1 text-sm text-[var(--color-ink-soft)]">
            {mode === "signin"
              ? "Pick up where you left off."
              : "Start planning in under a minute."}
          </p>

          <div className="mt-6 flex gap-1 rounded-xl bg-[var(--color-surface-2)] p-1">
            {(["signin", "signup"] as const).map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => {
                  setMode(option);
                  setError(null);
                }}
                aria-pressed={mode === option}
                className={`min-h-10 flex-1 rounded-lg px-3 text-sm font-medium transition-colors duration-200 ${
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
            <div className="mt-4">
              <ErrorBanner message={error} />
            </div>
          )}
          {notice && (
            <p className="mt-4 rounded-xl bg-[var(--color-mint-soft)] px-4 py-3 text-sm text-[var(--color-mint)]">
              {notice}
            </p>
          )}

          <button
            type="button"
            onClick={() => void signInWithGoogle()}
            className="mt-5 flex min-h-11 w-full items-center justify-center gap-2 rounded-xl border border-[var(--color-line-strong)] bg-[var(--color-surface)] px-4 text-sm font-medium transition-colors duration-200 hover:bg-[var(--color-surface-2)]"
          >
            <svg className="h-4 w-4" viewBox="0 0 24 24" aria-hidden>
              <path
                fill="#4285F4"
                d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.27-4.74 3.27-8.1Z"
              />
              <path
                fill="#34A853"
                d="M12 23c2.97 0 5.46-.98 7.28-2.65l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84A11 11 0 0 0 12 23Z"
              />
              <path
                fill="#FBBC05"
                d="M5.84 14.11a6.6 6.6 0 0 1 0-4.22V7.05H2.18a11 11 0 0 0 0 9.9l3.66-2.84Z"
              />
              <path
                fill="#EA4335"
                d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1a11 11 0 0 0-9.82 6.05l3.66 2.84C6.71 7.31 9.14 5.38 12 5.38Z"
              />
            </svg>
            Continue with Google
          </button>

          <div className="relative my-5">
            <span className="absolute inset-0 flex items-center" aria-hidden>
              <span className="w-full border-t border-[var(--color-line)]" />
            </span>
            <span className="relative flex justify-center">
              <span className="bg-[var(--color-surface)] px-2 text-xs text-[var(--color-ink-faint)]">
                or
              </span>
            </span>
          </div>

          <form onSubmit={handleSubmit} className="space-y-3.5">
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
                placeholder="you@example.com"
                className="min-h-11 w-full rounded-xl border border-[var(--color-line-strong)] bg-[var(--color-surface)] px-3.5 text-sm outline-none transition-colors duration-200 placeholder:text-[var(--color-ink-faint)] focus:border-[var(--color-brand)]"
              />
            </label>

            <label className="block">
              <span className="mb-1 block text-xs font-medium text-[var(--color-ink-soft)]">
                Password
              </span>
              <span className="relative block">
                <input
                  type={showPassword ? "text" : "password"}
                  required
                  minLength={6}
                  autoComplete={mode === "signup" ? "new-password" : "current-password"}
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder="At least 6 characters"
                  className="min-h-11 w-full rounded-xl border border-[var(--color-line-strong)] bg-[var(--color-surface)] px-3.5 pr-16 text-sm outline-none transition-colors duration-200 placeholder:text-[var(--color-ink-faint)] focus:border-[var(--color-brand)]"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((value) => !value)}
                  className="absolute inset-y-0 right-0 px-3 text-xs font-medium text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]"
                >
                  {showPassword ? "Hide" : "Show"}
                </button>
              </span>
            </label>

            <Button type="submit" disabled={busy} className="w-full">
              {busy
                ? "Just a moment…"
                : mode === "signin"
                  ? "Sign in"
                  : "Create account"}
              {!busy && <IconChevron size="1em" />}
            </Button>
          </form>

          <p className="mt-6 text-center text-[11px] leading-relaxed text-[var(--color-ink-faint)]">
            Your preferences are stored so future trips don&apos;t need repeating.
            You can see and delete everything at any time.
          </p>
        </div>
      </div>
    </main>
  );
}
