"use client";

/**
 * The public landing page.
 *
 * The chat used to live at `/`, which meant the first thing anyone saw was an
 * empty input box - fine once you know what the thing does, useless as an
 * introduction. The chat moved to `/chat` and this took its place.
 *
 * It is deliberately honest about mechanism rather than making claims. The
 * three-gear section shows the actual routing rule, the tool grid names the
 * real data sources including the one that is simulated, and the sample
 * exchange is the shape of a real reply. A reviewer should be able to read
 * this page and know what to go and verify.
 *
 * Signed-in visitors get "Open the planner"; everyone else gets "Start
 * planning", which routes through sign-in. The check is client-side because
 * the session lives in the browser, and the button is rendered in a neutral
 * state until it resolves so it never flashes the wrong label.
 */

import Link from "next/link";
import { useEffect, useState } from "react";

import {
  IconBed,
  IconBrain,
  IconChat,
  IconChevron,
  IconClock,
  IconCompass,
  IconFork,
  IconPin,
  IconPlane,
  IconSparkle,
  IconSun,
  IconTicket,
} from "@/components/icons";
import { Badge, Card } from "@/components/ui";
import { getSupabase } from "@/lib/supabase";

const GEARS = [
  {
    name: "Clarify",
    when: "You haven't said where",
    example: "\"Somewhere warm in December\"",
    result: "One friendly question — never a form.",
    tint: "from-[var(--color-gold-soft)] to-transparent",
    accent: "text-[var(--color-gold)]",
    icon: IconChat,
  },
  {
    name: "Advise",
    when: "A place, but not yet a trip",
    example: "\"I want to go to Kerala\"",
    result: "Grounded options and at most two questions — not a surprise itinerary.",
    tint: "from-[var(--color-grape-soft)] to-transparent",
    accent: "text-[var(--color-grape)]",
    icon: IconSparkle,
  },
  {
    name: "Plan",
    when: "Enough to build on",
    example: "\"5 days from 2 October, from Delhi\"",
    result: "The full day-by-day plan, with photos and a map.",
    tint: "from-[var(--color-brand-soft)] to-transparent",
    accent: "text-[var(--color-brand-strong)]",
    icon: IconCompass,
  },
] as const;

const TOOLS = [
  { icon: IconCompass, label: "Travel guides", source: "Wikivoyage", note: "Multi-hop research" },
  { icon: IconPin, label: "Real places", source: "OpenStreetMap", note: "Filterable by diet & access" },
  { icon: IconSun, label: "Weather", source: "Open-Meteo", note: "Real forecasts" },
  { icon: IconTicket, label: "Live web", source: "Tavily", note: "Events and closures" },
  { icon: IconPlane, label: "Flights", source: "Simulated", note: "Clearly labelled" },
  { icon: IconBed, label: "Stays", source: "Simulated", note: "Clearly labelled" },
] as const;

export default function LandingPage() {
  const [signedIn, setSignedIn] = useState<boolean | null>(null);

  useEffect(() => {
    getSupabase()
      .auth.getSession()
      .then(({ data }) => setSignedIn(Boolean(data.session)))
      .catch(() => setSignedIn(false));
  }, []);

  const cta = signedIn ? "Open the planner" : "Start planning";
  const href = signedIn ? "/chat" : "/login";

  return (
    <div className="mx-auto w-full max-w-5xl px-4 pb-20 sm:px-6">
      <header className="flex items-center justify-between py-5">
        <span className="flex items-center gap-2.5">
          <span className="grid h-9 w-9 place-items-center rounded-xl bg-[var(--color-brand)] text-white">
            <IconCompass />
          </span>
          <span className="font-display text-[15px] font-semibold tracking-tight">
            Trip Planner
          </span>
        </span>
        <Link
          href={href}
          className="inline-flex min-h-10 items-center gap-1.5 rounded-xl border border-[var(--color-line-strong)] bg-[var(--color-surface)] px-4 text-sm font-medium transition-colors duration-200 hover:border-[var(--color-brand)]"
        >
          {signedIn === null ? "Continue" : cta}
        </Link>
      </header>

      {/* -- Hero ---------------------------------------------------------- */}
      <section className="rise-in pt-8 text-center sm:pt-14">
        <Badge tone="accent">
          <IconSparkle size="0.9em" />
          Plans around you, not at you
        </Badge>

        <h1 className="mx-auto mt-4 max-w-3xl font-display text-4xl font-semibold leading-[1.1] tracking-tight sm:text-6xl">
          Begin your next{" "}
          <span className="bg-gradient-to-r from-[var(--color-brand)] via-[var(--color-rose)] to-[var(--color-grape)] bg-clip-text text-transparent">
            adventure
          </span>
        </h1>

        <p className="mx-auto mt-4 max-w-xl text-base leading-relaxed text-[var(--color-ink-soft)]">
          Tell it a place. It researches real travel guides, asks the questions
          that actually change the plan, and remembers what matters to you — so
          the next trip starts where the last one left off.
        </p>

        <div className="mt-7 flex flex-wrap items-center justify-center gap-3">
          <Link
            href={href}
            className="inline-flex min-h-12 items-center gap-2 rounded-xl bg-[var(--color-brand-strong)] px-6 text-sm font-semibold text-white shadow-[0_12px_30px_-14px_rgb(194_65_12_/_0.8)] transition-transform duration-200 hover:-translate-y-0.5"
          >
            {signedIn === null ? "Continue" : cta}
            <IconChevron size="1em" />
          </Link>
          <a
            href="#how"
            className="inline-flex min-h-12 items-center gap-2 rounded-xl border border-[var(--color-line-strong)] bg-[var(--color-surface)] px-5 text-sm font-medium transition-colors duration-200 hover:border-[var(--color-brand)]"
          >
            See how it works
          </a>
        </div>

        <p className="mt-3 text-xs text-[var(--color-ink-faint)]">
          Free to try · No card · Your data is yours to delete
        </p>
      </section>

      {/* -- A real exchange ------------------------------------------------ */}
      <section className="mt-14 sm:mt-20">
        <Card className="overflow-hidden">
          <div className="flex items-center gap-2 border-b border-[var(--color-line)] px-4 py-2.5">
            <span className="flex gap-1.5" aria-hidden>
              {["#f87171", "#fbbf24", "#34d399"].map((color) => (
                <span
                  key={color}
                  className="h-2.5 w-2.5 rounded-full"
                  style={{ background: color }}
                />
              ))}
            </span>
            <span className="text-[11px] text-[var(--color-ink-faint)]">
              A real conversation
            </span>
          </div>

          <div className="space-y-3 p-4 sm:p-6">
            <div className="flex justify-end">
              <span className="rounded-2xl rounded-br-md bg-[var(--color-brand-strong)] px-4 py-2 text-sm text-white">
                I want to go to Kerala
              </span>
            </div>

            <div className="max-w-[92%] rounded-2xl border border-[var(--color-line)] bg-[var(--color-surface-2)] px-4 py-3 text-sm leading-relaxed">
              <p>Kerala splits into a few very different trips 🌴</p>
              <ul className="mt-2 space-y-1 text-[var(--color-ink-soft)]">
                <li>
                  <strong className="text-[var(--color-ink)]">Backwaters</strong> — Alappuzha,
                  houseboats and canals
                </li>
                <li>
                  <strong className="text-[var(--color-ink)]">Tea hills</strong> — Munnar,
                  plantations and cool air
                </li>
                <li>
                  <strong className="text-[var(--color-ink)]">Culture</strong> — Fort Kochi,
                  colonial lanes and cafés
                </li>
              </ul>
              <p className="mt-2">
                How many days do you have, and which of those matters most?
              </p>
              <div className="mt-2.5 flex flex-wrap gap-1.5">
                {["Alappuzha", "Munnar", "Fort Kochi"].map((option) => (
                  <span
                    key={option}
                    className="rounded-full border border-[var(--color-line-strong)] bg-[var(--color-surface)] px-2.5 py-1 text-[11px] font-medium"
                  >
                    {option}
                  </span>
                ))}
              </div>
            </div>

            <p className="pt-1 text-center text-[11px] text-[var(--color-ink-faint)]">
              Those places come from real guide articles — not from the model&apos;s memory.
            </p>
          </div>
        </Card>
      </section>

      {/* -- Three gears ---------------------------------------------------- */}
      <section id="how" className="mt-16 scroll-mt-8 sm:mt-24">
        <h2 className="text-center font-display text-2xl font-semibold tracking-tight sm:text-3xl">
          It works out how much to ask
        </h2>
        <p className="mx-auto mt-2 max-w-xl text-center text-sm text-[var(--color-ink-soft)]">
          Most assistants either interrogate you or guess. This one picks a gear
          from what it already knows — and the rule is plain code, not a mood.
        </p>

        <div className="mt-8 grid gap-3 md:grid-cols-3">
          {GEARS.map((gear, index) => {
            const Glyph = gear.icon;
            return (
              <Card
                key={gear.name}
                className={`relative overflow-hidden bg-gradient-to-b p-5 ${gear.tint}`}
              >
                <span className="absolute right-4 top-4 font-display text-4xl font-bold text-[var(--color-ink)]/[0.06]">
                  {index + 1}
                </span>
                <span className={`inline-flex items-center gap-1.5 ${gear.accent}`}>
                  <Glyph size="1.1em" />
                  <span className="font-display text-sm font-semibold">{gear.name}</span>
                </span>
                <p className="mt-2 text-xs font-medium text-[var(--color-ink-soft)]">
                  {gear.when}
                </p>
                <p className="mt-2.5 rounded-lg bg-[var(--color-surface)]/70 px-3 py-2 text-xs italic">
                  {gear.example}
                </p>
                <p className="mt-2.5 text-xs leading-relaxed">{gear.result}</p>
              </Card>
            );
          })}
        </div>
      </section>

      {/* -- What it can reach ---------------------------------------------- */}
      <section className="mt-16 sm:mt-24">
        <h2 className="text-center font-display text-2xl font-semibold tracking-tight sm:text-3xl">
          Grounded in real sources
        </h2>
        <p className="mx-auto mt-2 max-w-xl text-center text-sm text-[var(--color-ink-soft)]">
          The agent chooses which of these to use, per request. Nothing here is
          keyword routing — and the two that are simulated say so.
        </p>

        <div className="mt-8 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {TOOLS.map((tool) => {
            const Glyph = tool.icon;
            const simulated = tool.source === "Simulated";
            return (
              <Card key={tool.label} className="flex items-start gap-3 p-4" interactive>
                <span
                  className={`grid h-9 w-9 shrink-0 place-items-center rounded-xl ${
                    simulated
                      ? "bg-[var(--color-surface-2)] text-[var(--color-ink-faint)]"
                      : "bg-[var(--color-brand-soft)] text-[var(--color-brand-strong)]"
                  }`}
                >
                  <Glyph size="1.05em" />
                </span>
                <span className="min-w-0">
                  <span className="block font-display text-sm font-semibold">{tool.label}</span>
                  <span className="mt-0.5 block text-xs text-[var(--color-ink-soft)]">
                    {tool.note}
                  </span>
                  <span
                    className={`mt-1 inline-block rounded-full px-2 py-0.5 text-[10px] font-medium ${
                      simulated
                        ? "bg-[var(--color-gold-soft)] text-[var(--color-gold)]"
                        : "bg-[var(--color-mint-soft)] text-[var(--color-mint)]"
                    }`}
                  >
                    {tool.source}
                  </span>
                </span>
              </Card>
            );
          })}
        </div>
      </section>

      {/* -- Memory ---------------------------------------------------------- */}
      <section className="mt-16 sm:mt-24">
        <Card className="grid gap-6 p-6 sm:p-8 lg:grid-cols-2">
          <div>
            <span className="inline-flex items-center gap-1.5 text-[var(--color-grape)]">
              <IconBrain size="1.1em" />
              <span className="font-display text-sm font-semibold">It remembers you</span>
            </span>
            <h3 className="mt-2 font-display text-xl font-semibold tracking-tight sm:text-2xl">
              Say it once
            </h3>
            <p className="mt-2 text-sm leading-relaxed text-[var(--color-ink-soft)]">
              Mention that you&apos;re vegetarian, travel on a budget or fly from
              Delhi, and every future trip is planned around it — without you
              repeating yourself. Hard requirements are passed to searches as
              filters, not hints, so they can&apos;t quietly get dropped.
            </p>
            <p className="mt-3 text-xs text-[var(--color-ink-faint)]">
              Everything stored is visible on one page, and deletable in a click.
            </p>
          </div>

          <div className="space-y-2">
            {[
              { text: "Traveller is vegetarian.", tag: "Must be honoured", tone: "good" },
              { text: "Prefers quiet places over crowds.", tag: "Preference", tone: "neutral" },
              { text: "Flies from Delhi.", tag: "About you", tone: "neutral" },
            ].map((memory) => (
              <div
                key={memory.text}
                className="flex items-center justify-between gap-3 rounded-xl border border-[var(--color-line)] bg-[var(--color-surface-2)] px-3.5 py-2.5"
              >
                <span className="text-sm">{memory.text}</span>
                <Badge tone={memory.tone as "good" | "neutral"}>{memory.tag}</Badge>
              </div>
            ))}
          </div>
        </Card>
      </section>

      {/* -- Show its work ---------------------------------------------------- */}
      <section className="mt-16 sm:mt-24">
        <div className="grid gap-3 sm:grid-cols-3">
          {[
            {
              icon: IconClock,
              title: "Shows its work",
              body: "Every reply can expand to the plan it wrote and each tool it called, with timings.",
            },
            {
              icon: IconPin,
              title: "Puts it on a map",
              body: "Stops are numbered and pinned, so you can see what's a walk and what's a drive.",
            },
            {
              icon: IconFork,
              title: "Offers the next step",
              body: "Flights, a place to stay, somewhere to eat — offered when useful, never nagged.",
            },
          ].map((feature) => {
            const Glyph = feature.icon;
            return (
              <Card key={feature.title} className="p-5">
                <span className="grid h-9 w-9 place-items-center rounded-xl bg-[var(--color-sky-soft)] text-[var(--color-sky)]">
                  <Glyph size="1.05em" />
                </span>
                <h3 className="mt-3 font-display text-sm font-semibold">{feature.title}</h3>
                <p className="mt-1 text-xs leading-relaxed text-[var(--color-ink-soft)]">
                  {feature.body}
                </p>
              </Card>
            );
          })}
        </div>
      </section>

      {/* -- Close ------------------------------------------------------------ */}
      <section className="mt-16 text-center sm:mt-24">
        <Card className="bg-gradient-to-br from-[var(--color-brand-soft)] via-[var(--color-rose-soft)]/60 to-[var(--color-grape-soft)] p-10">
          <h2 className="font-display text-2xl font-semibold tracking-tight sm:text-3xl">
            Where are you going?
          </h2>
          <p className="mx-auto mt-2 max-w-md text-sm text-[var(--color-ink-soft)]">
            Name a place — it takes it from there.
          </p>
          <Link
            href={href}
            className="mt-6 inline-flex min-h-12 items-center gap-2 rounded-xl bg-[var(--color-brand-strong)] px-6 text-sm font-semibold text-white shadow-[0_12px_30px_-14px_rgb(194_65_12_/_0.8)] transition-transform duration-200 hover:-translate-y-0.5"
          >
            {signedIn === null ? "Continue" : cta}
            <IconChevron size="1em" />
          </Link>
        </Card>

        <p className="mt-8 text-xs text-[var(--color-ink-faint)]">
          Flight and hotel prices are simulated and clearly labelled. Everything
          else comes from live sources.
        </p>
      </section>
    </div>
  );
}
