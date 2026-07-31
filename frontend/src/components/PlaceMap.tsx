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

import { api, type ItineraryItem } from "@/lib/api";

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

/**
 * Base layers, all keyless.
 *
 * Satellite is the one people actually want for a holiday - a street map
 * tells you where the fort is, imagery tells you what it looks like from
 * above - and Esri serves it without a key or an account. Terrain earns its
 * place for anywhere mountainous, where "two hours away" and "two hours away
 * over a pass" are the same distance on a flat map.
 */
const BASE_LAYERS = {
  Map: {
    url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}{r}.png",
    attribution: "© OpenStreetMap contributors",
    maxZoom: 19,
  },
  Satellite: {
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attribution: "Imagery © Esri, Maxar, Earthstar Geographics",
    maxZoom: 19,
  },
  Terrain: {
    url: "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
    attribution: "© OpenStreetMap contributors, SRTM · © OpenTopoMap",
    maxZoom: 17,
  },
} as const;

export type BaseLayerName = keyof typeof BASE_LAYERS;

export function PlaceMap({
  items,
  activeIndex = null,
  focusIndex = null,
  onHoverItem,
  onSelectItem,
  layer = "Map",
  destination = "",
  showPhotos = false,
  className = "",
}: {
  items: MappedItem[];
  /** Highlighted item, driven by hovering a day card. */
  activeIndex?: number | null;
  /** Fly to this stop when it changes. Set by clicking a row in the list. */
  focusIndex?: number | null;
  onHoverItem?: (index: number | null) => void;
  onSelectItem?: (index: number) => void;
  layer?: BaseLayerName;
  destination?: string;
  /** Put a photograph in the popup. Only worth the request on the big map. */
  showPhotos?: boolean;
  className?: string;
}) {
  const container = useRef<HTMLDivElement>(null);
  // Types come from a type-only import, so they cost nothing at runtime while
  // the module itself is still loaded on demand further down.
  const map = useRef<LeafletNamespace.Map | null>(null);
  const markers = useRef<Map<number, LeafletNamespace.Marker>>(new Map());
  const route = useRef<LeafletNamespace.Polyline | null>(null);
  const base = useRef<LeafletNamespace.TileLayer | null>(null);
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
        // `detectRetina` asks for @2x tiles on high-density screens, which is
        // the difference between a crisp map and a blurry one on any modern
        // laptop - and costs nothing on a display that cannot use them.
        const chosen = BASE_LAYERS[layer];
        base.current = L.tileLayer(chosen.url, {
          maxZoom: chosen.maxZoom,
          attribution: chosen.attribution,
          detectRetina: true,
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

        // The photo is fetched only when the popup opens, not for every pin
        // on the map: a twelve-stop plan would otherwise fire twelve image
        // lookups the moment it rendered, for pictures nobody had asked to see.
        marker.bindPopup(
          `<strong>${escapeHtml(item.name)}</strong>` +
            (item.district ? `<br/><small>${escapeHtml(item.district)}</small>` : "") +
            (showPhotos ? `<div data-photo="${escapeHtml(item.name)}"></div>` : ""),
          { minWidth: showPhotos ? 200 : 100 },
        );

        if (showPhotos) {
          marker.on("popupopen", () => {
            const slot = document.querySelector<HTMLElement>(
              `[data-photo="${CSS.escape(item.name)}"]`,
            );
            if (!slot || slot.dataset.loaded) return;
            slot.dataset.loaded = "1";
            void api
              .placeImage({
                name: item.name,
                destination,
                latitude: item.latitude,
                longitude: item.longitude,
                kind: item.kind,
              })
              .then((image) => {
                slot.innerHTML =
                  `<img src="${image.url}" alt="" ` +
                  `style="margin-top:6px;width:100%;height:104px;object-fit:cover;border-radius:8px" />`;
              })
              .catch(() => {
                slot.remove();
              });
          });
        }
        marker.on("mouseover", () => handleHover(item.index));
        marker.on("mouseout", () => handleHover(null));
        marker.on("click", () => {
          map.current?.flyTo([item.latitude!, item.longitude!], 16, { duration: 0.7 });
          onSelectItem?.(item.index);
        });

        markers.current.set(item.index, marker);
      }

      // A dashed line through the stops in order. Pins alone say where the
      // places are; the line says what the day actually looks like - three
      // stops in a row and one across town read very differently, and that is
      // the thing worth noticing before the trip rather than during it.
      route.current?.remove();
      if (points.length > 1) {
        route.current = L.polyline(
          points.map((p) => [p.latitude!, p.longitude!] as [number, number]),
          {
            color: "#e85d2c",
            weight: 2,
            opacity: 0.55,
            dashArray: "5 7",
            // Under the markers: the line is context, the pins are the
            // things being pointed at.
            interactive: false,
          },
        ).addTo(map.current);
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
  }, [signature, handleHover, points, layer, showPhotos, destination, onSelectItem]);

  // Fly to a stop when the list asks for it. Separate from `activeIndex`,
  // which only highlights: hovering a row should not yank the map around,
  // but clicking one should take you there.
  useEffect(() => {
    if (focusIndex == null || !map.current) return;
    const target = points.find((point) => point.index === focusIndex);
    if (!target) return;
    map.current.flyTo([target.latitude!, target.longitude!], 15, { duration: 0.7 });
    markers.current.get(focusIndex)?.openPopup();
  }, [focusIndex, points]);

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
