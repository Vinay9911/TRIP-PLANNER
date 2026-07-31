"use client";

/**
 * Renders a structured itinerary as day cards.
 *
 * This replaces a wall of prose. A live five-day Geneva plan arrived as five
 * paragraphs - accurate and unreadable, with nowhere to hang a photograph, a
 * map pin or a "find me a hotel here" button. Against typed days and items,
 * all three are straightforward.
 *
 * **Photographs load lazily, per item, after the card is on screen.** Each
 * lookup is a round trip to Wikimedia, so resolving them server-side before
 * replying would have added seconds to every plan. An `IntersectionObserver`
 * means a ten-day trip only fetches photos for the days someone actually
 * scrolls to.
 *
 * **Stand-in photographs are labelled.** The resolver returns real pictures of
 * the actual place where it can, and something merely similar where it
 * cannot. Only the second kind carries a badge - marking every image would be
 * noise, and marking none would be a small lie repeated on every card.
 */

import { useEffect, useRef, useState } from "react";
import { useCallback } from "react";

import {
  IconBed,
  IconChevron,
  IconClock,
  IconCompass,
  IconFork,
  IconInfo,
  IconPin,
  IconPlane,
  IconSparkle,
  IconTicket,
} from "@/components/icons";
import { dayColor, type MappedItem } from "@/components/PlaceMap";
import { PlacePhoto } from "@/components/PlacePhoto";
import { Badge, Button, Card } from "@/components/ui";
import { api, type Itinerary, type ItineraryDay, type ItineraryItem } from "@/lib/api";
import { IconCopy } from "@/components/icons";

const KIND_ICON = {
  sight: IconTicket,
  food: IconFork,
  activity: IconSparkle,
  transport: IconPlane,
  stay: IconBed,
  neighbourhood: IconPin,
  note: IconInfo,
} as const;

const KIND_TINT = {
  sight: "bg-[var(--color-rose-soft)] text-[var(--color-rose)]",
  food: "bg-[var(--color-mint-soft)] text-[var(--color-mint)]",
  activity: "bg-[var(--color-brand-soft)] text-[var(--color-brand-strong)]",
  transport: "bg-[var(--color-sky-soft)] text-[var(--color-sky)]",
  stay: "bg-[var(--color-grape-soft)] text-[var(--color-grape)]",
  neighbourhood: "bg-[var(--color-gold-soft)] text-[var(--color-gold)]",
  note: "bg-[var(--color-surface-2)] text-[var(--color-ink-soft)]",
} as const;

const SLOTS = [
  { key: "all_day", label: "" },
  { key: "morning", label: "Morning" },
  { key: "afternoon", label: "Afternoon" },
  { key: "evening", label: "Evening" },
] as const;

/**
 * A place photograph, fetched only once its card is near the viewport.
 *
 * Renders a shimmering placeholder of the right aspect ratio while loading,
 * so the surrounding layout never jumps when the image lands.
 */
function Item({
  item,
  destination,
  stopNumber,
  dayNumber,
  active,
  onHover,
}: {
  item: ItineraryItem;
  destination: string;
  stopNumber?: number;
  dayNumber: number;
  active?: boolean;
  onHover?: (index: number | null) => void;
}) {
  const Glyph = KIND_ICON[item.kind] ?? IconPin;
  const tint = KIND_TINT[item.kind] ?? KIND_TINT.note;

  return (
    <li
      onMouseEnter={() => stopNumber != null && onHover?.(stopNumber)}
      onMouseLeave={() => onHover?.(null)}
      className={`flex gap-3 rounded-xl p-2 transition-colors duration-200 ${
        active ? "bg-[var(--color-brand-soft)]" : "hover:bg-[var(--color-surface-2)]"
      }`}
    >
      <PlacePhoto
        name={item.name}
        destination={destination}
        latitude={item.latitude}
        longitude={item.longitude}
        kind={item.kind}
        className="h-16 w-20 shrink-0 sm:h-20 sm:w-28"
      />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className={`grid h-5 w-5 place-items-center rounded-md ${tint}`}>
            <Glyph size="0.8em" />
          </span>
          {stopNumber != null && (
            <span
              className="grid h-4 w-4 place-items-center rounded-full text-[9px] font-semibold text-white"
              style={{ background: dayColor(dayNumber) }}
              aria-hidden
            >
              {stopNumber}
            </span>
          )}
          <span className="font-display text-sm font-semibold">{item.name}</span>
          {item.district && (
            <span className="text-[11px] text-[var(--color-ink-faint)]">{item.district}</span>
          )}
        </div>
        {item.description && (
          <p className="mt-1 text-xs leading-relaxed text-[var(--color-ink-soft)]">
            {item.description}
          </p>
        )}
        <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-[var(--color-ink-faint)]">
          {item.approx_duration && (
            <span className="inline-flex items-center gap-1">
              <IconClock size="0.85em" />
              {item.approx_duration}
            </span>
          )}
          {item.booking_note && (
            <span className="text-[var(--color-warn)]">{item.booking_note}</span>
          )}
        </div>
      </div>
    </li>
  );
}

function Day({
  day,
  destination,
  stopNumberOf,
  activeIndex,
  onHover,
}: {
  day: ItineraryDay;
  destination: string;
  stopNumberOf?: (item: ItineraryItem) => number | undefined;
  activeIndex?: number | null;
  onHover?: (index: number | null) => void;
}) {
  const [open, setOpen] = useState(true);
  const total =
    day.all_day.length + day.morning.length + day.afternoon.length + day.evening.length;

  return (
    <Card className="overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors duration-200 hover:bg-[var(--color-surface-2)]"
      >
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-[var(--color-brand)] font-display text-sm font-semibold text-white">
          {day.day_number}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block font-display text-sm font-semibold">{day.title}</span>
          <span className="block text-[11px] text-[var(--color-ink-faint)]">
            {day.date ? `${day.date} · ` : ""}
            {total} {total === 1 ? "stop" : "stops"}
          </span>
        </span>
        <span
          className={`shrink-0 text-[var(--color-ink-faint)] transition-transform duration-200 ${
            open ? "rotate-90" : ""
          }`}
        >
          <IconChevron size="1em" />
        </span>
      </button>

      {open && (
        <div className="border-t border-[var(--color-line)] px-3 pb-3 pt-2">
          {day.summary && (
            <p className="px-2 pb-1 text-xs leading-relaxed text-[var(--color-ink-soft)]">
              {day.summary}
            </p>
          )}
          {SLOTS.map(({ key, label }) => {
            const items = day[key];
            if (!items || items.length === 0) return null;
            return (
              <section key={key} className="mt-1.5">
                {label && (
                  <h4 className="px-2 text-[10px] font-semibold uppercase tracking-wide text-[var(--color-ink-faint)]">
                    {label}
                  </h4>
                )}
                <ul className="mt-0.5 space-y-0.5">
                  {items.map((item, index) => {
                    const stopNumber = stopNumberOf?.(item);
                    return (
                      <Item
                        key={`${item.name}-${index}`}
                        item={item}
                        destination={destination}
                        stopNumber={stopNumber}
                        dayNumber={day.day_number}
                        active={stopNumber != null && stopNumber === activeIndex}
                        onHover={onHover}
                      />
                    );
                  })}
                </ul>
              </section>
            );
          })}
        </div>
      )}
    </Card>
  );
}

export function ItineraryView({ itinerary }: { itinerary: Itinerary }) {
  const destination = itinerary.destination;
  const [activeIndex, setActiveIndex] = useState<number | null>(null);

  // Number every stop once across the whole trip, so a marker's label matches
  // the order someone would actually do them in.
  const mapped: MappedItem[] = [];
  let counter = 0;
  for (const day of itinerary.days) {
    for (const item of [...day.all_day, ...day.morning, ...day.afternoon, ...day.evening]) {
      counter += 1;
      mapped.push({ ...item, index: counter, dayNumber: day.day_number });
    }
  }
  const mappable = mapped.filter((item) => item.latitude != null && item.longitude != null);

  // Only mapped stops are numbered: a number beside a card whose pin does not
  // exist would point at nothing.
  const numberByName = new Map(mappable.map((item) => [item.name, item.index]));
  const stopNumberOf = (item: ItineraryItem) => numberByName.get(item.name);

  return (
    <div className="space-y-2.5">
      {/* The map moved to the trip panel, which stays put while the
          conversation grows. A second copy here scrolled away exactly when it
          became useful, and two maps of the same stops disagreed about which
          one to interact with. Numbering on the cards still refers to the
          panel's pins. */}
      <CopyItineraryButton itinerary={itinerary} />
      {itinerary.intro && (
        <Card className="flex gap-3 overflow-hidden">
          <PlacePhoto
            name={destination}
            destination={destination}
            latitude={null}
            longitude={null}
            kind="neighbourhood"
            className="hidden h-auto w-32 shrink-0 rounded-none sm:block"
          />
          <div className="px-4 py-3">
            <div className="flex items-center gap-1.5">
              <span className="text-[var(--color-brand)]">
                <IconCompass size="1em" />
              </span>
              <h3 className="font-display text-sm font-semibold">{destination}</h3>
              <Badge tone="accent">
                {itinerary.days.length} {itinerary.days.length === 1 ? "day" : "days"}
              </Badge>
            </div>
            <p className="mt-1.5 text-sm leading-relaxed">{itinerary.intro}</p>
          </div>
        </Card>
      )}

      {itinerary.days.map((day) => (
        <Day
          key={day.day_number}
          day={day}
          destination={destination}
          stopNumberOf={stopNumberOf}
          activeIndex={activeIndex}
          onHover={setActiveIndex}
        />
      ))}

      {itinerary.practical_notes.length > 0 && (
        <Card className="px-4 py-3">
          <h4 className="font-display text-xs font-semibold">Good to know</h4>
          <ul className="mt-1.5 space-y-1">
            {itinerary.practical_notes.map((note) => (
              <li key={note} className="flex gap-2 text-xs text-[var(--color-ink-soft)]">
                <span className="text-[var(--color-brand)]">•</span>
                {note}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {itinerary.gaps.length > 0 && (
        <div className="rounded-xl border border-[var(--color-line)] bg-[var(--color-gold-soft)]/50 px-4 py-2.5">
          {itinerary.gaps.map((gap) => (
            <p key={gap} className="flex gap-2 text-xs text-[var(--color-ink-soft)]">
              <span className="shrink-0 text-[var(--color-warn)]">
                <IconInfo size="0.95em" />
              </span>
              {gap}
            </p>
          ))}
        </div>
      )}

      {/* Stated once per plan rather than on every thumbnail. Photographs are
          real where a real one exists; the rest are labelled individually. */}
      <p className="px-1 text-[10px] leading-relaxed text-[var(--color-ink-faint)]">
        Photos come from Wikimedia Commons where a picture of the place exists. Any
        marked <span className="font-medium">similar</span> show somewhere comparable
        rather than that exact spot.
      </p>
    </div>
  );
}

function CopyItineraryButton({ itinerary }: { itinerary: Itinerary }) {
  const [copied, setCopied] = useState(false);

  const copyToClipboard = useCallback(() => {
    const lines: string[] = [];
    lines.push(`Trip to ${itinerary.destination}`);
    lines.push(`Duration: ${itinerary.days.length} days`);
    if (itinerary.intro) {
      lines.push("");
      lines.push(itinerary.intro);
    }

    itinerary.days.forEach((day) => {
      lines.push("");
      lines.push(`--- Day ${day.day_number}: ${day.title} ---`);
      if (day.date) lines.push(`Date: ${day.date}`);
      
      const parts = [
        { label: "Morning", items: day.morning },
        { label: "Afternoon", items: day.afternoon },
        { label: "Evening", items: day.evening },
        { label: "All Day", items: day.all_day },
      ];

      parts.forEach((part) => {
        if (part.items && part.items.length > 0) {
          lines.push(`\n[${part.label}]`);
          part.items.forEach((item) => {
            lines.push(`• ${item.name}`);
            if (item.description) lines.push(`  ${item.description}`);
            if (item.approx_duration) lines.push(`  Time: ${item.approx_duration}`);
          });
        }
      });
    });

    if (itinerary.practical_notes.length > 0) {
      lines.push("\n--- Practical Notes ---");
      itinerary.practical_notes.forEach((note) => lines.push(`• ${note}`));
    }

    navigator.clipboard.writeText(lines.join("\n")).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [itinerary]);

  return (
    <div className="flex justify-end pb-2">
      <Button
        type="button"
        onClick={copyToClipboard}
        className="flex min-h-9 items-center gap-1.5 rounded-xl border border-[var(--color-line-strong)] bg-[var(--color-surface)] px-3 text-xs font-medium transition-colors hover:border-[var(--color-brand)] hover:text-[var(--color-brand)]"
      >
        <IconCopy size="1em" />
        {copied ? "Copied!" : "Copy Itinerary"}
      </Button>
    </div>
  );
}
