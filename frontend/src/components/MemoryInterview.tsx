"use client";

/**
 * Two ways to teach the agent about yourself directly.
 *
 * **The interview** is a handful of one-tap questions. It exists because the
 * extraction gate is deliberately strict - it only stores facts that outlast
 * a single trip - so a new user can hold an entire conversation and still see
 * an empty profile. Correct behaviour, terrible first impression. Six taps
 * gets the agent genuinely personalised before the first trip is planned.
 *
 * **The free-text box** is for everything the questions do not cover.
 *
 * Both post through the same endpoint as extracted facts, which means a
 * manual answer still reinforces an existing belief rather than duplicating
 * it, and still supersedes one it contradicts.
 *
 * Each question is skippable and the whole thing is dismissible. A profile
 * questionnaire that cannot be escaped is a form, which is exactly what this
 * product is trying not to be.
 */

import { useState } from "react";

import { IconChevron, IconClose, IconSparkle } from "@/components/icons";
import { Button, Card } from "@/components/ui";
import { ApiError, api, type Memory } from "@/lib/api";

interface Choice {
  label: string;
  /** Written in the third person, matching how extraction phrases facts, so
   *  the stored profile reads consistently however it was populated. */
  fact: string;
  type?: "constraint" | "preference" | "identity";
}

interface Question {
  id: string;
  prompt: string;
  subject: string;
  choices: Choice[];
}

const QUESTIONS: Question[] = [
  {
    id: "diet",
    prompt: "Anything you don't eat?",
    subject: "diet",
    choices: [
      { label: "Vegetarian", fact: "Traveller is vegetarian.", type: "constraint" },
      { label: "Vegan", fact: "Traveller is vegan.", type: "constraint" },
      { label: "Halal", fact: "Traveller eats halal only.", type: "constraint" },
      { label: "No restrictions", fact: "Traveller has no dietary restrictions." },
    ],
  },
  {
    id: "pace",
    prompt: "What pace suits you?",
    subject: "pace",
    choices: [
      { label: "Slow and relaxed", fact: "Traveller prefers a slow, relaxed pace." },
      { label: "Balanced", fact: "Traveller prefers a balanced pace." },
      { label: "Pack it in", fact: "Traveller likes to fit a lot into each day." },
    ],
  },
  {
    id: "budget",
    prompt: "Roughly what budget?",
    subject: "budget",
    choices: [
      { label: "Budget", fact: "Traveller travels on a tight budget." },
      { label: "Mid-range", fact: "Traveller prefers mid-range options." },
      { label: "Comfort matters", fact: "Traveller prefers comfortable, higher-end options." },
    ],
  },
  {
    id: "crowds",
    prompt: "Crowds or quiet?",
    subject: "interests",
    choices: [
      { label: "Avoid crowds", fact: "Traveller prefers to avoid crowded tourist sites." },
      { label: "Don't mind", fact: "Traveller does not mind busy tourist sites." },
    ],
  },
  {
    id: "companions",
    prompt: "Who normally travels with you?",
    subject: "companions",
    choices: [
      { label: "Just me", fact: "Traveller usually travels solo." },
      { label: "A partner", fact: "Traveller usually travels with a partner." },
      { label: "Family", fact: "Traveller usually travels as a family." },
      { label: "Friends", fact: "Traveller usually travels with friends." },
    ],
  },
  {
    id: "access",
    prompt: "Any access needs?",
    subject: "accessibility",
    choices: [
      {
        label: "Step-free access",
        fact: "Traveller needs step-free, wheelchair-accessible places.",
        type: "constraint",
      },
      { label: "Not much walking", fact: "Traveller prefers to avoid a lot of walking." },
      { label: "None", fact: "Traveller has no accessibility requirements." },
    ],
  },
];

export function MemoryInterview({
  onAdded,
}: {
  /** Lets the page fold the new memory into its list without a refetch. */
  onAdded: (memory: Memory) => void;
}) {
  const [step, setStep] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const question = QUESTIONS[step];
  const finished = step >= QUESTIONS.length;

  async function choose(choice: Choice) {
    setSaving(choice.label);
    setError(null);
    try {
      const stored = await api.addMemory({
        content: choice.fact,
        memory_type: choice.type ?? "preference",
        subject: question.subject,
      });
      onAdded(stored);
      setStep((value) => value + 1);
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : "Could not save that just now.",
      );
    } finally {
      setSaving(null);
    }
  }

  if (dismissed) return null;

  if (finished) {
    return (
      <Card className="flex items-center gap-3 bg-gradient-to-r from-[var(--color-mint-soft)] to-transparent px-4 py-3">
        <span className="text-[var(--color-mint)]">
          <IconSparkle size="1.1em" />
        </span>
        <p className="flex-1 text-sm">
          That&apos;s enough to personalise your trips. Everything above can be
          edited or deleted.
        </p>
        <Button variant="ghost" onClick={() => setDismissed(true)} aria-label="Dismiss">
          <IconClose size="1em" />
        </Button>
      </Card>
    );
  }

  return (
    <Card className="overflow-hidden">
      <div className="flex items-center gap-2 border-b border-[var(--color-line)] px-4 py-2.5">
        <span className="text-[var(--color-brand)]">
          <IconSparkle size="1em" />
        </span>
        <span className="flex-1 font-display text-xs font-semibold">
          Quick questions — {step + 1} of {QUESTIONS.length}
        </span>
        <button
          type="button"
          onClick={() => setDismissed(true)}
          aria-label="Dismiss these questions"
          className="grid h-8 w-8 place-items-center rounded-lg text-[var(--color-ink-faint)] hover:bg-[var(--color-surface-2)]"
        >
          <IconClose size="0.95em" />
        </button>
      </div>

      {/* Progress. Width-only transition on a fixed-height bar, so it cannot
          shift anything around it. */}
      <div className="h-1 bg-[var(--color-surface-2)]">
        <div
          className="h-full bg-[var(--color-brand)] transition-[width] duration-500 ease-out"
          style={{ width: `${(step / QUESTIONS.length) * 100}%` }}
        />
      </div>

      <div className="p-4">
        <p className="font-display text-sm font-semibold">{question.prompt}</p>

        <div className="mt-3 flex flex-wrap gap-2">
          {question.choices.map((choice) => (
            <button
              key={choice.label}
              type="button"
              disabled={saving !== null}
              onClick={() => void choose(choice)}
              className="min-h-10 rounded-xl border border-[var(--color-line-strong)] bg-[var(--color-surface)] px-3.5 text-xs font-medium transition-[border-color,transform] duration-200 hover:-translate-y-0.5 hover:border-[var(--color-brand)] disabled:opacity-50"
            >
              {saving === choice.label ? "Saving…" : choice.label}
            </button>
          ))}
        </div>

        {error && <p className="mt-2 text-xs text-[var(--color-danger)]">{error}</p>}

        <button
          type="button"
          onClick={() => setStep((value) => value + 1)}
          className="mt-3 inline-flex min-h-8 items-center gap-1 text-xs text-[var(--color-ink-faint)] hover:text-[var(--color-ink)]"
        >
          Skip this
          <IconChevron size="0.85em" />
        </button>
      </div>
    </Card>
  );
}

/** A free-text box for anything the questions do not cover. */
export function AddMemory({ onAdded }: { onAdded: (memory: Memory) => void }) {
  const [content, setContent] = useState("");
  const [type, setType] = useState<"preference" | "constraint">("preference");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const text = content.trim();
    if (text.length < 3 || busy) return;

    setBusy(true);
    setError(null);
    try {
      onAdded(await api.addMemory({ content: text, memory_type: type }));
      setContent("");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not save that.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="p-4">
      <h3 className="font-display text-sm font-semibold">Tell it something yourself</h3>
      <p className="mt-0.5 text-xs text-[var(--color-ink-soft)]">
        Anything that would still be true on a completely different trip.
      </p>

      <form onSubmit={submit} className="mt-3 space-y-2.5">
        <label className="block">
          <span className="sr-only">The fact to remember</span>
          <input
            value={content}
            onChange={(event) => setContent(event.target.value)}
            maxLength={280}
            placeholder="e.g. I don't drink alcohol"
            className="min-h-11 w-full rounded-xl border border-[var(--color-line-strong)] bg-[var(--color-surface)] px-3.5 text-sm outline-none transition-colors duration-200 placeholder:text-[var(--color-ink-faint)] focus:border-[var(--color-brand)]"
          />
        </label>

        <div className="flex flex-wrap items-center gap-2">
          <div className="flex gap-1 rounded-lg bg-[var(--color-surface-2)] p-1">
            {(
              [
                ["preference", "Preference"],
                ["constraint", "Must be honoured"],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => setType(value)}
                aria-pressed={type === value}
                className={`min-h-8 rounded-md px-2.5 text-xs font-medium transition-colors duration-200 ${
                  type === value
                    ? "bg-[var(--color-surface)] shadow-sm"
                    : "text-[var(--color-ink-soft)]"
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          <Button type="submit" disabled={busy || content.trim().length < 3} className="ml-auto">
            {busy ? "Saving…" : "Remember this"}
          </Button>
        </div>

        {error && <p className="text-xs text-[var(--color-danger)]">{error}</p>}
      </form>
    </Card>
  );
}
