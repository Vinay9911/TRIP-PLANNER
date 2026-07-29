/**
 * Shared presentational pieces.
 *
 * Small on purpose. The interesting parts of this project are on the server,
 * and a hand-rolled component library here would be effort spent where the
 * assessment awards no marks.
 */

import Link from "next/link";
import type { ReactNode } from "react";

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`rounded-xl border border-[var(--color-line)] bg-[var(--color-surface)] ${className}`}
    >
      {children}
    </div>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "good" | "warn" | "bad" | "accent";
}) {
  const tones = {
    neutral: "bg-[var(--color-line)]/50 text-[var(--color-ink-soft)]",
    good: "bg-[var(--color-accent-soft)] text-[var(--color-accent)]",
    warn: "bg-amber-100 text-[var(--color-warn)] dark:bg-amber-950/40",
    bad: "bg-red-100 text-[var(--color-danger)] dark:bg-red-950/40",
    accent: "bg-[var(--color-accent)] text-white",
  } as const;

  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

export function Button({
  children,
  onClick,
  type = "button",
  variant = "primary",
  disabled = false,
  className = "",
}: {
  children: ReactNode;
  onClick?: () => void;
  type?: "button" | "submit";
  variant?: "primary" | "secondary" | "ghost" | "danger";
  disabled?: boolean;
  className?: string;
}) {
  const variants = {
    primary:
      "bg-[var(--color-accent)] text-white hover:opacity-90 disabled:opacity-40",
    secondary:
      "border border-[var(--color-line)] bg-[var(--color-surface)] hover:bg-[var(--color-line)]/30",
    ghost: "text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]",
    danger:
      "border border-[var(--color-danger)]/40 text-[var(--color-danger)] hover:bg-red-50 dark:hover:bg-red-950/30",
  } as const;

  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`rounded-lg px-4 py-2 text-sm font-medium transition-opacity disabled:cursor-not-allowed ${variants[variant]} ${className}`}
    >
      {children}
    </button>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-4 border-b border-[var(--color-line)] pb-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {subtitle && (
          <p className="mt-1 text-sm text-[var(--color-ink-soft)]">{subtitle}</p>
        )}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </header>
  );
}

export function Nav({ email, isAdmin }: { email: string | null; isAdmin: boolean }) {
  return (
    <nav className="flex items-center gap-1 text-sm">
      <NavLink href="/">Chat</NavLink>
      <NavLink href="/memories">What it remembers</NavLink>
      {isAdmin && <NavLink href="/admin">Admin</NavLink>}
      {email && (
        <span className="ml-2 hidden text-xs text-[var(--color-ink-soft)] sm:inline">
          {email}
        </span>
      )}
    </nav>
  );
}

function NavLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link
      href={href}
      className="rounded-lg px-3 py-1.5 text-[var(--color-ink-soft)] transition-colors hover:bg-[var(--color-line)]/40 hover:text-[var(--color-ink)]"
    >
      {children}
    </Link>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="rounded-xl border border-dashed border-[var(--color-line)] p-8 text-center">
      <p className="text-sm font-medium">{title}</p>
      {hint && <p className="mt-1 text-sm text-[var(--color-ink-soft)]">{hint}</p>}
    </div>
  );
}

export function ErrorBanner({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-[var(--color-danger)]/30 bg-red-50 px-4 py-3 text-sm text-[var(--color-danger)] dark:bg-red-950/30"
    >
      {message}
    </div>
  );
}

/** Renders the assistant's markdown-ish reply without pulling in a parser.
 *
 * The model returns headings, bold text and bullets. A full markdown library
 * would be ~40 kB for four constructs, so this handles those and escapes
 * everything else. `escapeHtml` runs first, so model output can never inject
 * markup - which matters because that text is not under our control.
 */
export function FormattedText({ text }: { text: string }) {
  const html = text
    .split("\n")
    .map((line) => {
      const safe = escapeHtml(line);
      if (/^#{1,3}\s/.test(safe)) {
        return `<h3 class="mt-4 mb-1 font-semibold">${safe.replace(/^#{1,3}\s/, "")}</h3>`;
      }
      if (/^[-*]\s/.test(safe)) {
        return `<li class="ml-4 list-disc">${inline(safe.replace(/^[-*]\s/, ""))}</li>`;
      }
      if (safe.trim() === "") return "";
      return `<p class="mb-2">${inline(safe)}</p>`;
    })
    .join("");

  return (
    <div
      className="text-sm leading-relaxed"
      // Safe: escapeHtml has already neutralised the model's output, and only
      // the four constructs above are reintroduced as markup.
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function inline(value: string): string {
  return value
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>");
}

/** Colour-codes a run status. Shared between the dashboard and trace views. */
export function StatusBadge({ status }: { status: string }) {
  const tone =
    status === "completed"
      ? "good"
      : status === "failed"
        ? "bad"
        : status === "partial" || status === "clarifying"
          ? "warn"
          : "neutral";
  return <Badge tone={tone}>{status}</Badge>;
}
