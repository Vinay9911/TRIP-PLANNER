"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ItineraryItem } from "@/lib/api";
import type { Map as MapboxMap, Marker, Popup } from "mapbox-gl";

// Statically imported, unlike the library itself. `await import(...)` of a CSS
// file is not a module TypeScript can resolve a type for, and it failed the
// production build outright - `next build` type-checks, so what worked in dev
// never once compiled for deployment. The library still loads dynamically
// below to keep it out of the server bundle; a stylesheet has no SSR problem
// to avoid, so there was never a reason for it to be deferred with it.
import "mapbox-gl/dist/mapbox-gl.css";

export interface MappedItem extends ItineraryItem {
  index: number;
  dayNumber: number;
}

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
  const scale = active ? 1.3 : 1;
  const shadow = active ? "0 4px 12px rgb(0 0 0 / .4)" : "0 2px 8px rgb(0 0 0 / .28)";
  const bounceClass = active ? "marker-bounce" : "";
  
  return `
    <span class="${bounceClass}" style="
      display:grid;place-items:center;
      width:28px;height:28px;border-radius:999px;
      background:${color};color:#fff;
      font:600 12px/1 ui-sans-serif,system-ui,sans-serif;
      box-shadow:${shadow};
      border:2px solid #fff;
      transform:scale(${scale});
      transition:all 200ms cubic-bezier(.22,1,.36,1);
    ">${item.index}</span>
    <style>
      .marker-bounce {
        animation: bounce 0.5s ease infinite alternate;
      }
      @keyframes bounce {
        from { transform: translateY(0) scale(${scale}); }
        to { transform: translateY(-8px) scale(${scale}); }
      }
    </style>`;
}

const BASE_LAYERS = {
  Map: "mapbox://styles/mapbox/streets-v12",
  Satellite: "mapbox://styles/mapbox/satellite-streets-v12",
  Terrain: "mapbox://styles/mapbox/outdoors-v12",
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
  activeIndex?: number | null;
  focusIndex?: number | null;
  onHoverItem?: (index: number | null) => void;
  onSelectItem?: (index: number) => void;
  layer?: BaseLayerName;
  destination?: string;
  showPhotos?: boolean;
  className?: string;
}) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<MapboxMap | null>(null);
  const markers = useRef<Map<number, Marker>>(new Map());
  const popups = useRef<Map<number, Popup>>(new Map());
  const [ready, setReady] = useState(false);
  const mapboxglRef = useRef<unknown>(null);

  const points = items.filter((item) => item.latitude != null && item.longitude != null);
  const signature = points.map((p) => `${p.index}:${p.latitude},${p.longitude}`).join("|");

  const handleHover = useCallback((index: number | null) => onHoverItem?.(index), [onHoverItem]);

  useEffect(() => {
    if (!container.current || points.length === 0) return;
    let cancelled = false;

    async function boot() {
      // Import mapbox-gl dynamically to avoid SSR issues. Its stylesheet is
      // imported statically at the top of the file - see the note there.
      const mapboxgl = (await import("mapbox-gl")).default;
      if (cancelled || !container.current) return;
      mapboxglRef.current = mapboxgl;

      // Handle multiple tokens by splitting and picking one randomly
      const tokensStr = process.env.NEXT_PUBLIC_MAPBOX_TOKEN || process.env.NEXT_PUBLIC_MAPBOX_TOKENS || "";
      const tokens = tokensStr.split(",").map(t => t.trim()).filter(Boolean);
      mapboxgl.accessToken = tokens.length > 0 ? tokens[Math.floor(Math.random() * tokens.length)] : "";

      if (!mapboxgl.accessToken) {
        console.warn("Mapbox Token missing! Map will not load. Please add NEXT_PUBLIC_MAPBOX_TOKEN to .env.local");
      }

      if (!map.current) {
        map.current = new mapboxgl.Map({
          container: container.current,
          style: BASE_LAYERS[layer],
          cooperativeGestures: true, // Requires Cmd/Ctrl to zoom, fixing scroll trap!
          attributionControl: true,
        });

        // Add controls
        map.current.addControl(new mapboxgl.NavigationControl({ showCompass: false }), "top-right");
        map.current.addControl(new mapboxgl.ScaleControl({ maxWidth: 80, unit: "metric" }), "bottom-left");

        // Fit Bounds Button Control
        class FitBoundsControl {
          onAdd(m: MapboxMap) {
            this._map = m;
            this._container = document.createElement('div');
            this._container.className = 'mapboxgl-ctrl mapboxgl-ctrl-group';
            const btn = document.createElement('button');
            btn.className = 'mapboxgl-ctrl-icon';
            btn.type = 'button';
            btn.title = 'Fit to Route';
            btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="padding:4px;width:100%;height:100%"><path d="M4 4h16v16H4z"/><path d="M8 8l8 8M16 8l-8 8"/></svg>`;
            btn.onclick = () => {
              if (points.length > 0) {
                const bounds = new mapboxgl.LngLatBounds();
                points.forEach(p => bounds.extend([p.longitude!, p.latitude!]));
                this._map?.fitBounds(bounds, { padding: 40, maxZoom: 15 });
              }
            };
            this._container.appendChild(btn);
            return this._container;
          }
          onRemove() {
            this._container.parentNode?.removeChild(this._container);
            this._map = undefined;
          }
          _map?: MapboxMap;
          _container!: HTMLDivElement;
        }
        map.current.addControl(new FitBoundsControl(), "top-right");

        map.current.on('load', () => {
          if (cancelled || !map.current) return;
          
          // Draw animated route
          if (points.length > 1) {
            map.current.addSource('route', {
              type: 'geojson',
              data: {
                type: 'Feature',
                properties: {},
                geometry: {
                  type: 'LineString',
                  coordinates: points.map(p => [p.longitude!, p.latitude!])
                }
              }
            });

            map.current.addLayer({
              id: 'route-line',
              type: 'line',
              source: 'route',
              layout: {
                'line-join': 'round',
                'line-cap': 'round'
              },
              paint: {
                'line-color': '#e85d2c',
                'line-width': 3,
                'line-dasharray': [0, 2]
              }
            });

            // Animate dasharray
            let step = 0;
            const animateDash = () => {
               if (!map.current || !map.current.getLayer('route-line')) return;
               const dashArraySequence = [
                 [0, 2], [0.5, 1.5], [1, 1], [1.5, 0.5], [2, 0]
               ];
               step = (step + 1) % dashArraySequence.length;
               map.current.setPaintProperty('route-line', 'line-dasharray', dashArraySequence[step]);
               setTimeout(() => requestAnimationFrame(animateDash), 100);
            };
            animateDash();
          }
          
          setReady(true);
        });
      } else {
        // Update style if layer changed
        map.current.setStyle(BASE_LAYERS[layer]);
      }

      // Rebuild markers
      markers.current.forEach((marker) => marker.remove());
      markers.current.clear();
      popups.current.forEach((popup) => popup.remove());
      popups.current.clear();

      for (const item of points) {
        const el = document.createElement("div");
        el.innerHTML = markerHtml(item, false);
        el.style.cursor = "pointer";
        
        const popupHtml = `<strong>${escapeHtml(item.name)}</strong>` +
          (item.district ? `<br/><small>${escapeHtml(item.district)}</small>` : "") +
          (showPhotos ? `<div data-photo="${escapeHtml(item.name)}"></div>` : "");

        const popup = new mapboxgl.Popup({ offset: 15, closeButton: false, maxWidth: showPhotos ? "240px" : "150px" })
          .setHTML(popupHtml);

        const marker = new mapboxgl.Marker({ element: el })
          .setLngLat([item.longitude!, item.latitude!])
          .setPopup(popup)
          .addTo(map.current!);

        if (showPhotos) {
          popup.on('open', () => {
            const slot = document.querySelector<HTMLElement>(`[data-photo="${CSS.escape(item.name)}"]`);
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
                slot.innerHTML = `<img src="${image.url}" alt="" style="margin-top:6px;width:100%;height:104px;object-fit:cover;border-radius:8px" />`;
              })
              .catch(() => {
                slot.remove();
              });
          });
        }

        el.addEventListener("mouseenter", () => handleHover(item.index));
        el.addEventListener("mouseleave", () => handleHover(null));
        el.addEventListener("click", () => {
          map.current?.flyTo({ center: [item.longitude!, item.latitude!], zoom: 16, duration: 1500 });
          onSelectItem?.(item.index);
        });

        markers.current.set(item.index, marker);
        popups.current.set(item.index, popup);
      }

      if (points.length > 0) {
        const bounds = new mapboxgl.LngLatBounds();
        points.forEach(p => bounds.extend([p.longitude!, p.latitude!]));
        map.current.fitBounds(bounds, { padding: 40, maxZoom: 15 });
      }

    }

    void boot();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, layer, showPhotos, destination, onSelectItem, handleHover]);

  // Handle activeIndex (hover highlights)
  useEffect(() => {
    if (!mapboxglRef.current) return;
    markers.current.forEach((marker, index) => {
      const item = points.find((p) => p.index === index);
      if (!item) return;
      const el = marker.getElement();
      el.innerHTML = markerHtml(item, index === activeIndex);
    });

    if (activeIndex != null) {
      const item = points.find((p) => p.index === activeIndex);
      if (item && map.current) {
        map.current.panTo([item.longitude!, item.latitude!], { duration: 500 });
      }
    }
  }, [activeIndex, points]);

  // Handle focusIndex (click in list)
  useEffect(() => {
    if (focusIndex == null || !map.current) return;
    const target = points.find((point) => point.index === focusIndex);
    if (!target) return;
    map.current.flyTo({ center: [target.longitude!, target.latitude!], zoom: 15, duration: 1500 });
    const popup = popups.current.get(focusIndex);
    const marker = markers.current.get(focusIndex);
    if (popup && marker) marker.togglePopup();
  }, [focusIndex, points]);

  // Teardown
  useEffect(() => () => {
    map.current?.remove();
    map.current = null;
  }, []);

  if (points.length === 0) {
    return (
      <div className={`grid place-items-center rounded-2xl border border-dashed border-[var(--color-line-strong)] p-6 text-center text-xs text-[var(--color-ink-soft)] ${className}`}>
        No mapped coordinates for this plan yet — ask for specific places and they&apos;ll appear here.
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
