"use client";

/**
 * Small, dependency-free SVG charts.
 *
 * Hand-rolled rather than pulling in a charting library: the dashboards here
 * need three chart types over at most a few dozen points, and Recharts or
 * Chart.js would add more JavaScript than the entire rest of this app.
 *
 * Accessibility is the part worth reading. Charts are one of the easiest
 * things to make unusable, so each of these:
 *
 * - carries `role="img"` and a summary `aria-label` stating the actual
 *   takeaway, because "a chart" tells a screen-reader user nothing;
 * - ships a real `<table>` alternative behind a disclosure, since an SVG is
 *   not navigable and colour alone is not an accessible encoding;
 * - labels values directly on the mark wherever there is room, so the legend
 *   is a convenience rather than the only way to read the thing;
 * - draws grid lines well below the data in contrast so they never compete
 *   with it.
 *
 * There is deliberately no pie chart. Slice comparison fails for colour-blind
 * users and is imprecise for everyone; the donut here is used only for a
 * single-value completion gauge, where there is one arc and a number in the
 * middle rather than a set of slices to compare.
 */

import { useId, useState } from "react";

export interface Point {
  label: string;
  value: number;
}

/** The series palette. Ordered so adjacent entries stay distinguishable for
 *  the common forms of colour blindness, and every one clears 3:1 on white. */
export const SERIES_COLORS = [
  "var(--color-brand)",
  "var(--color-grape)",
  "var(--color-sky)",
  "var(--color-mint)",
  "var(--color-gold)",
  "var(--color-rose)",
];

function DataTable({ points, unit }: { points: Point[]; unit?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="mt-2"
      open={open}
      onToggle={(event) => setOpen((event.target as HTMLDetailsElement).open)}
    >
      <summary className="cursor-pointer text-xs text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
        {open ? "Hide" : "Show"} the numbers
      </summary>
      <div className="mt-2 max-h-52 overflow-auto scroll-slim">
        <table className="w-full text-left text-xs">
          <thead className="text-[var(--color-ink-soft)]">
            <tr>
              <th scope="col" className="py-1 pr-3 font-medium">
                Item
              </th>
              <th scope="col" className="py-1 font-medium">
                Value{unit ? ` (${unit})` : ""}
              </th>
            </tr>
          </thead>
          <tbody>
            {points.map((point) => (
              <tr key={point.label} className="border-t border-[var(--color-line)]">
                <th scope="row" className="py-1 pr-3 font-normal">
                  {point.label}
                </th>
                {/* Tabular figures stop the column jittering as values change. */}
                <td className="py-1 tabular-nums">{point.value.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function EmptyChart({ hint }: { hint: string }) {
  return (
    <div className="flex h-40 items-center justify-center rounded-xl border border-dashed border-[var(--color-line-strong)] px-4 text-center text-xs text-[var(--color-ink-soft)]">
      {hint}
    </div>
  );
}

/**
 * Horizontal bars. Chosen over vertical columns because the labels here are
 * words (tool names, destinations) - horizontally they read straight, and
 * vertically they would need rotating, which is unreadable on a phone.
 */
export function BarChart({
  points,
  unit,
  emptyHint = "No data yet.",
  summary,
}: {
  points: Point[];
  unit?: string;
  emptyHint?: string;
  summary?: string;
}) {
  if (points.length === 0) return <EmptyChart hint={emptyHint} />;

  const max = Math.max(...points.map((p) => p.value), 1);
  const top = points[0];

  return (
    <div>
      <div
        role="img"
        aria-label={
          summary ??
          `Bar chart. Highest: ${top.label} at ${top.value.toLocaleString()}${
            unit ? ` ${unit}` : ""
          }. ${points.length} items shown.`
        }
        className="space-y-2"
      >
        {points.map((point, index) => (
          <div key={point.label} className="grid grid-cols-[minmax(0,9rem)_1fr_auto] items-center gap-3">
            <span className="truncate text-xs text-[var(--color-ink-soft)]" title={point.label}>
              {point.label}
            </span>
            <span className="h-2.5 overflow-hidden rounded-full bg-[var(--color-surface-2)]">
              <span
                className="block h-full rounded-full transition-[width] duration-500"
                style={{
                  width: `${Math.max((point.value / max) * 100, 2)}%`,
                  background: SERIES_COLORS[index % SERIES_COLORS.length],
                }}
              />
            </span>
            {/* Value labelled directly: no hunting between mark and legend. */}
            <span className="w-12 text-right text-xs font-medium tabular-nums">
              {point.value.toLocaleString()}
            </span>
          </div>
        ))}
      </div>
      <DataTable points={points} unit={unit} />
    </div>
  );
}

/**
 * A filled line chart for a value over time. Falls back to a bar chart below
 * four points, where a "trend" is not a meaningful reading of the data.
 */
export function TrendChart({
  points,
  unit,
  emptyHint = "No activity recorded yet.",
}: {
  points: Point[];
  unit?: string;
  emptyHint?: string;
}) {
  const gradientId = useId();

  if (points.length === 0) return <EmptyChart hint={emptyHint} />;
  if (points.length < 4) return <BarChart points={points} unit={unit} />;

  const width = 560;
  const height = 160;
  const pad = { top: 12, right: 8, bottom: 22, left: 8 };
  const max = Math.max(...points.map((p) => p.value), 1);
  const stepX = (width - pad.left - pad.right) / (points.length - 1);
  const plotHeight = height - pad.top - pad.bottom;

  const coords = points.map((point, index) => ({
    x: pad.left + index * stepX,
    y: pad.top + plotHeight - (point.value / max) * plotHeight,
    ...point,
  }));

  const line = coords.map((c, i) => `${i === 0 ? "M" : "L"}${c.x},${c.y}`).join(" ");
  const area = `${line} L${coords[coords.length - 1].x},${pad.top + plotHeight} L${coords[0].x},${
    pad.top + plotHeight
  } Z`;

  const total = points.reduce((sum, p) => sum + p.value, 0);
  const first = points[0].value;
  const last = points[points.length - 1].value;
  const direction = last > first ? "rising" : last < first ? "falling" : "flat";

  return (
    <div>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="h-40 w-full"
        role="img"
        aria-label={`Trend chart, ${direction}. ${total.toLocaleString()} total across ${
          points.length
        } points, from ${points[0].label} to ${points[points.length - 1].label}.`}
        preserveAspectRatio="none"
      >
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--color-brand)" stopOpacity="0.28" />
            <stop offset="100%" stopColor="var(--color-brand)" stopOpacity="0" />
          </linearGradient>
        </defs>

        {/* Grid kept low-contrast so it never competes with the data. */}
        {[0, 0.5, 1].map((fraction) => (
          <line
            key={fraction}
            x1={pad.left}
            x2={width - pad.right}
            y1={pad.top + plotHeight * fraction}
            y2={pad.top + plotHeight * fraction}
            stroke="var(--color-line)"
            strokeWidth="1"
          />
        ))}

        <path d={area} fill={`url(#${gradientId})`} />
        <path d={line} fill="none" stroke="var(--color-brand)" strokeWidth="2.5" />

        {coords.map((c) => (
          <circle key={c.label} cx={c.x} cy={c.y} r="3" fill="var(--color-brand-strong)">
            <title>{`${c.label}: ${c.value.toLocaleString()}${unit ? ` ${unit}` : ""}`}</title>
          </circle>
        ))}
      </svg>

      <div className="flex justify-between px-1 text-[10px] text-[var(--color-ink-faint)]">
        <span>{points[0].label}</span>
        <span>{points[points.length - 1].label}</span>
      </div>
      <DataTable points={points} unit={unit} />
    </div>
  );
}

/**
 * A single-value gauge. One arc and a number, not a set of slices - see the
 * module note on why there is no pie chart here.
 */
export function Gauge({
  value,
  max,
  label,
  caption,
}: {
  value: number;
  max: number;
  label: string;
  caption?: string;
}) {
  const safeMax = Math.max(max, 1);
  const fraction = Math.min(value / safeMax, 1);
  const radius = 42;
  const circumference = 2 * Math.PI * radius;

  return (
    <div className="flex items-center gap-4">
      <svg
        viewBox="0 0 100 100"
        className="h-24 w-24 shrink-0 -rotate-90"
        role="img"
        aria-label={`${label}: ${value.toLocaleString()} of ${safeMax.toLocaleString()}, ${Math.round(
          fraction * 100,
        )} percent.`}
      >
        <circle cx="50" cy="50" r={radius} fill="none" stroke="var(--color-surface-2)" strokeWidth="10" />
        <circle
          cx="50"
          cy="50"
          r={radius}
          fill="none"
          stroke="var(--color-brand)"
          strokeWidth="10"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={circumference * (1 - fraction)}
          className="transition-[stroke-dashoffset] duration-700"
        />
      </svg>
      <div className="min-w-0">
        <p className="font-display text-2xl font-semibold tabular-nums">
          {value.toLocaleString()}
          <span className="text-base font-normal text-[var(--color-ink-faint)]">
            /{safeMax.toLocaleString()}
          </span>
        </p>
        <p className="text-sm font-medium">{label}</p>
        {caption && <p className="mt-0.5 text-xs text-[var(--color-ink-soft)]">{caption}</p>}
      </div>
    </div>
  );
}

/** A headline number with an optional supporting line. */
export function StatTile({
  icon,
  label,
  value,
  hint,
  tone = "brand",
}: {
  icon: React.ReactNode;
  label: string;
  value: string | number;
  hint?: string;
  tone?: "brand" | "grape" | "sky" | "mint" | "gold";
}) {
  const tones = {
    brand: "bg-[var(--color-brand-soft)] text-[var(--color-brand-strong)]",
    grape: "bg-[var(--color-grape-soft)] text-[var(--color-grape)]",
    sky: "bg-[var(--color-sky-soft)] text-[var(--color-sky)]",
    mint: "bg-[var(--color-mint-soft)] text-[var(--color-mint)]",
    gold: "bg-[var(--color-gold-soft)] text-[var(--color-gold)]",
  } as const;

  return (
    <div className="rounded-2xl border border-[var(--color-line)] bg-[var(--color-surface)] p-4">
      <div className="flex items-center gap-2">
        <span className={`grid h-8 w-8 place-items-center rounded-lg ${tones[tone]}`}>{icon}</span>
        <span className="text-xs font-medium text-[var(--color-ink-soft)]">{label}</span>
      </div>
      <p className="mt-3 font-display text-2xl font-semibold tabular-nums">{value}</p>
      {hint && <p className="mt-1 text-xs text-[var(--color-ink-soft)]">{hint}</p>}
    </div>
  );
}
