"use client";

/**
 * An interactive map of the places in an itinerary.
 *
 * **Why a real map and not the dotted world map.** A dotted world map is the
 * right picture for a flight - two points and an arc between them. It is the
 * wrong picture for "where am I actually going in Geneva", where the whole
 * question is whether Old Town and Pâquis are a walk apart. That needs real
 * geography, so this is Leaflet over OpenStreetMap tiles: no API key, no
 * account, and the tiles are the same ones the guide data describes.
 *
 * **Leaflet is loaded on demand.** It is ~42kb plus a stylesheet, and most
 * turns never open a map, so the import happens the first time a map is
 * actually shown rather than on every page load.
 *
 * **Markers are numbered, not pinned.** A numbered circle says "stop 3 of the
 * day" at a glance, which is the thing a traveller wants from an itinerary
 * map; a generic teardrop pin says only "something is here".
 *
 * Hovering a day card highlights its pins and vice versa - the two views are
 * the same list, so they should feel like it.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type * as LeafletNamespace from "leaflet";

import type { ItineraryItem } from "@/lib/api";

export interface MappedItem extends ItineraryItem {
  /** 1-based position across the whole trip, shown inside the marker. */
  index: number;
  dayNumber: number;
}

/** Colour per day, so a multi-day trip reads as distinct clusters. */
const DAY_COLORS = [
  "#e85d2c",
  "#7c3aed",
  "#2563eb",
  "#0d9488",
  "#db2777",
  "#b45309",
];

export function dayColor(dayNumber: number): string {
  return DAY_COLORS[(dayNumber - 1) % DAY_COLORS.length];
}

function markerHtml(item: MappedItem, active: boolean): string {
  const color = dayColor(item.dayNumber);
  const scale = active ? 1.25 : 1;
  return `
    <span style="
      display:grid;place-items:center;
      width:28px;height:28px;border-radius:999px;
      background:${color};color:#fff;
      font:600 12px/1 ui-sans-serif,system-ui,sans-serif;
      box-shadow:0 2px 8px rgb(0 0 0 / .28);
      border:2px solid #fff;
      transform:scale(${scale});
      transition:transform 180ms cubic-bezier(.22,1,.36,1);
    ">${item.index}</span>`;
}

export function PlaceMap({
  items,
  activeIndex = null,
  onHoverItem,
  className = "",
}: {
  items: MappedItem[];
  /** Highlighted item, driven by hovering a day card. */
  activeIndex?: number | null;
  onHoverItem?: (index: number | null) => void;
  className?: string;
}) {
  const container = useRef<HTMLDivElement>(null);
  // Types come from a type-only import, so they cost nothing at runtime while
  // the module itself is still loaded on demand further down.
  const map = useRef<LeafletNamespace.Map | null>(null);
  const markers = useRef<Map<number, LeafletNamespace.Marker>>(new Map());
  const leaflet = useRef<typeof LeafletNamespace | null>(null);
  const [ready, setReady] = useState(false);

  const points = items.filter(
    (item) => item.latitude != null && item.longitude != null,
  );
  // Serialised so the effect re-runs when the actual coordinates change, not
  // on every re-render that happens to rebuild the array.
  const signature = points
    .map((p) => `${p.index}:${p.latitude},${p.longitude}`)
    .join("|");

  const handleHover = useCallback(
    (index: number | null) => onHoverItem?.(index),
    [onHoverItem],
  );

  useEffect(() => {
    if (!container.current || points.length === 0) return;

    let cancelled = false;

    async function boot() {
      // Loaded here rather than at module scope: Leaflet touches `window` on
      // import, and most conversations never open a map at all.
      const L = (await import("leaflet")).default;
      if (cancelled || !container.current) return;
      leaflet.current = L;

      if (!map.current) {
        map.current = L.map(container.current, {
          zoomControl: true,
          scrollWheelZoom: false, // Page scroll should not zoom the map.
          attributionControl: true,
        });
        L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
          maxZoom: 19,
          attribution: "© OpenStreetMap contributors",
        }).addTo(map.current);
      }

      // Rebuild markers from scratch: the set is small, and diffing it would
      // be more code than it saves.
      markers.current.forEach((marker) => marker.remove());
      markers.current.clear();

      for (const item of points) {
        const marker = L.marker([item.latitude!, item.longitude!], {
          icon: L.divIcon({
            html: markerHtml(item, false),
            className: "",
            iconSize: [28, 28],
            iconAnchor: [14, 14],
          }),
          title: item.name,
        }).addTo(map.current);

        marker.bindPopup(
          `<strong>${escapeHtml(item.name)}</strong>` +
            (item.district ? `<br/><small>${escapeHtml(item.district)}</small>` : ""),
        );
        marker.on("mouseover", () => handleHover(item.index));
        marker.on("mouseout", () => handleHover(null));
        marker.on("click", () => {
          map.current?.flyTo([item.latitude!, item.longitude!], 16, { duration: 0.6 });
        });

        markers.current.set(item.index, marker);
      }

      const bounds = L.latLngBounds(
        points.map((p) => [p.latitude!, p.longitude!] as [number, number]),
      );
      map.current.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });

      // Leaflet measures its container on creation; inside a panel that
      // animates open, that measurement is taken mid-transition and the tiles
      // come out misaligned until something forces a recalculation.
      setTimeout(() => map.current?.invalidateSize(), 250);
      setReady(true);
    }

    void boot();
    return () => {
      cancelled = true;
    };
  }, [signature, handleHover, points]);

  // Tear the map down only when the component truly unmounts.
  useEffect(
    () => () => {
      map.current?.remove();
      map.current = null;
    },
    [],
  );

  // Re-render markers when the highlighted item changes, and ease the map
  // over to it so the highlight is never off-screen.
  useEffect(() => {
    const L = leaflet.current;
    if (!L) return;

    markers.current.forEach((marker, index) => {
      const item = points.find((p) => p.index === index);
      if (!item) return;
      marker.setIcon(
        L.divIcon({
          html: markerHtml(item, index === activeIndex),
          className: "",
          iconSize: [28, 28],
          iconAnchor: [14, 14],
        }),
      );
    });

    if (activeIndex != null) {
      const item = points.find((p) => p.index === activeIndex);
      if (item && map.current) {
        map.current.panTo([item.latitude!, item.longitude!], {
          animate: true,
          duration: 0.5,
        });
      }
    }
  }, [activeIndex, points]);

  if (points.length === 0) {
    return (
      <div
        className={`grid place-items-center rounded-2xl border border-dashed border-[var(--color-line-strong)] p-6 text-center text-xs text-[var(--color-ink-soft)] ${className}`}
      >
        No mapped coordinates for this plan yet — ask for specific places and
        they&apos;ll appear here.
      </div>
    );
  }

  return (
    <div className={`relative overflow-hidden rounded-2xl border border-[var(--color-line)] ${className}`}>
      <div ref={container} className="h-full w-full" />
      {!ready && <div className="skeleton absolute inset-0" />}
    </div>
  );
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
