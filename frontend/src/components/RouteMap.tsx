"use client";

/**
 * An animated world map showing a flight route as a travelling arc.
 *
 * This is the *other* map. `PlaceMap` answers "how far apart are these two
 * neighbourhoods" and needs real tiles; this answers "I am flying from Delhi
 * to Geneva" and needs the opposite - a stylised globe where the arc is the
 * point and street detail would be noise.
 *
 * **Hand-rolled canvas, no dependencies.** The reference design for this
 * effect reached for `dotted-map`, `framer-motion` and `next-themes`. All
 * three are avoidable: the dot grid is a nested loop, the arc is a quadratic
 * curve, and the animation is one `requestAnimationFrame` loop. That keeps
 * roughly 90kb out of the bundle for an effect that is fundamentally a few
 * dozen lines of 2D drawing.
 *
 * **It respects reduced motion.** When the user has asked for less movement,
 * the arc is drawn complete and still rather than animating - the information
 * is identical, only the travel is dropped.
 */

import { useEffect, useRef } from "react";

export interface RoutePoint {
  label: string;
  latitude: number;
  longitude: number;
}

/** Equirectangular projection - the same one the dot grid is built on, so
 *  points land where the land is. */
function project(
  latitude: number,
  longitude: number,
  width: number,
  height: number,
): { x: number; y: number } {
  return {
    x: ((longitude + 180) / 360) * width,
    y: ((90 - latitude) / 180) * height,
  };
}

/**
 * Very rough landmass test, in normalised 0-1 coordinates.
 *
 * A real coastline would mean shipping a GeoJSON file; this only has to read
 * as "a world map" behind a glowing arc, and at dot resolution the difference
 * is not visible. Deliberately crude, and called out as such so nobody
 * mistakes it for real geography.
 */
function isLand(u: number, v: number): boolean {
  const boxes: [number, number, number, number][] = [
    [0.12, 0.08, 0.30, 0.34], // North America
    [0.20, 0.36, 0.32, 0.72], // South America
    [0.44, 0.10, 0.56, 0.34], // Europe
    [0.45, 0.34, 0.60, 0.70], // Africa
    [0.56, 0.12, 0.78, 0.42], // Asia
    [0.60, 0.30, 0.72, 0.48], // South Asia
    [0.78, 0.56, 0.90, 0.74], // Australia
  ];
  return boxes.some(([x0, y0, x1, y1]) => u >= x0 && u <= x1 && v >= y0 && v <= y1);
}

export function RouteMap({
  from,
  to,
  className = "",
}: {
  from: RoutePoint;
  to: RoutePoint;
  className?: string;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const element = canvas.current;
    if (!element) return;

    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const context = element.getContext("2d");
    if (!context) return;

    let frame = 0;
    let start = performance.now();
    let width = 0;
    let height = 0;

    const resize = () => {
      const rect = element.parentElement?.getBoundingClientRect();
      if (!rect) return;
      // Drawn at device resolution so the dots stay crisp on retina screens.
      const ratio = window.devicePixelRatio || 1;
      width = rect.width;
      height = rect.height;
      element.width = width * ratio;
      element.height = height * ratio;
      element.style.width = `${width}px`;
      element.style.height = `${height}px`;
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
    };

    const draw = (now: number) => {
      if (width === 0 || height === 0) resize();
      context.clearRect(0, 0, width, height);

      // -- the dotted landmass --------------------------------------------
      const gap = 9;
      context.fillStyle = "rgba(232, 93, 44, 0.22)";
      for (let x = 0; x < width; x += gap) {
        for (let y = 0; y < height; y += gap) {
          if (!isLand(x / width, y / height)) continue;
          context.beginPath();
          context.arc(x, y, 1.1, 0, Math.PI * 2);
          context.fill();
        }
      }

      const a = project(from.latitude, from.longitude, width, height);
      const b = project(to.latitude, to.longitude, width, height);
      // Lift the control point so the arc bows away from the straight line,
      // more for longer routes.
      const control = {
        x: (a.x + b.x) / 2,
        y: Math.min(a.y, b.y) - Math.abs(b.x - a.x) * 0.28 - 20,
      };

      const cycle = 4200;
      const progress = reduceMotion
        ? 1
        : Math.min(((now - start) % cycle) / (cycle * 0.72), 1);

      // -- the arc ---------------------------------------------------------
      context.strokeStyle = "rgba(194, 65, 12, 0.85)";
      context.lineWidth = 2;
      context.beginPath();
      context.moveTo(a.x, a.y);
      for (let t = 0; t <= progress; t += 0.01) {
        const point = quadratic(a, control, b, t);
        context.lineTo(point.x, point.y);
      }
      context.stroke();

      // -- endpoints -------------------------------------------------------
      for (const [point, label] of [
        [a, from.label],
        [b, to.label],
      ] as const) {
        context.beginPath();
        context.arc(point.x, point.y, 4, 0, Math.PI * 2);
        context.fillStyle = "#c2410c";
        context.fill();
        context.strokeStyle = "#fff";
        context.lineWidth = 2;
        context.stroke();

        context.font = "600 11px ui-sans-serif, system-ui, sans-serif";
        const textWidth = context.measureText(label).width;
        const boxX = Math.min(Math.max(point.x - textWidth / 2 - 5, 2), width - textWidth - 12);
        context.fillStyle = "rgba(255,255,255,0.92)";
        context.beginPath();
        context.roundRect(boxX, point.y - 26, textWidth + 10, 17, 5);
        context.fill();
        context.fillStyle = "#2c1f2b";
        context.fillText(label, boxX + 5, point.y - 14);
      }

      // -- the travelling dot ----------------------------------------------
      if (!reduceMotion && progress < 1) {
        const head = quadratic(a, control, b, progress);
        const glow = context.createRadialGradient(head.x, head.y, 0, head.x, head.y, 11);
        glow.addColorStop(0, "rgba(232,93,44,0.55)");
        glow.addColorStop(1, "rgba(232,93,44,0)");
        context.fillStyle = glow;
        context.beginPath();
        context.arc(head.x, head.y, 11, 0, Math.PI * 2);
        context.fill();

        context.beginPath();
        context.arc(head.x, head.y, 3.5, 0, Math.PI * 2);
        context.fillStyle = "#e85d2c";
        context.fill();
      }

      if (!reduceMotion) frame = requestAnimationFrame(draw);
    };

    resize();
    start = performance.now();
    frame = requestAnimationFrame(draw);

    const observer = new ResizeObserver(() => {
      resize();
      if (reduceMotion) draw(performance.now());
    });
    if (element.parentElement) observer.observe(element.parentElement);

    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [from, to]);

  return (
    <div
      className={`relative overflow-hidden rounded-2xl border border-[var(--color-line)] bg-gradient-to-br from-[var(--color-brand-soft)]/50 to-[var(--color-grape-soft)]/40 ${className}`}
      role="img"
      aria-label={`Route from ${from.label} to ${to.label}`}
    >
      <canvas ref={canvas} className="block h-full w-full" />
    </div>
  );
}

function quadratic(
  a: { x: number; y: number },
  control: { x: number; y: number },
  b: { x: number; y: number },
  t: number,
): { x: number; y: number } {
  const inverse = 1 - t;
  return {
    x: inverse * inverse * a.x + 2 * inverse * t * control.x + t * t * b.x,
    y: inverse * inverse * a.y + 2 * inverse * t * control.y + t * t * b.y,
  };
}
