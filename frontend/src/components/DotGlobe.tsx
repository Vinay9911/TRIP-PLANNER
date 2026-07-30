"use client";

/**
 * The animated dotted world behind the sign-in panel.
 *
 * Adapted from the reference design the client supplied, with two deliberate
 * departures:
 *
 * **No dependencies.** The original reached for `framer-motion` for the fade
 * ins and `lucide-react` for one arrow. The canvas work it actually depends
 * on is plain 2D drawing, so the whole effect ships here with nothing added
 * to the bundle - the fades became CSS and the icon came from our own set.
 *
 * **Our palette, not blue.** The reference is blue-and-indigo, which would
 * have made sign-in the only cold screen in a warm application. Same motion,
 * recoloured.
 *
 * Routes are seeded but staggered and looping, so the panel always has
 * something moving without ever looking busy. Honours reduced-motion by
 * drawing the finished state once and stopping.
 */

import { useEffect, useRef } from "react";

interface Route {
  from: [number, number];
  to: [number, number];
  delay: number;
}

/** Normalised 0-1 coordinates, so the routes scale with the panel. */
const ROUTES: Route[] = [
  { from: [0.18, 0.42], to: [0.46, 0.26], delay: 0 },
  { from: [0.46, 0.26], to: [0.68, 0.38], delay: 1.1 },
  { from: [0.62, 0.3], to: [0.82, 0.62], delay: 2.2 },
  { from: [0.22, 0.6], to: [0.5, 0.7], delay: 1.7 },
];

/** The same crude landmass boxes the route map uses - enough to read as a
 *  world at dot resolution, and not pretending to be more. */
function isLand(u: number, v: number): boolean {
  const boxes: [number, number, number, number][] = [
    [0.1, 0.14, 0.3, 0.42],
    [0.18, 0.44, 0.3, 0.78],
    [0.42, 0.14, 0.56, 0.38],
    [0.44, 0.38, 0.58, 0.72],
    [0.56, 0.16, 0.8, 0.46],
    [0.78, 0.58, 0.9, 0.76],
  ];
  return boxes.some(([x0, y0, x1, y1]) => u >= x0 && u <= x1 && v >= y0 && v <= y1);
}

export function DotGlobe({ className = "" }: { className?: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const element = canvas.current;
    const context = element?.getContext("2d");
    if (!element || !context) return;

    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let frame = 0;
    let width = 0;
    let height = 0;
    const start = performance.now();

    const resize = () => {
      const rect = element.parentElement?.getBoundingClientRect();
      if (!rect) return;
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
      if (!width || !height) resize();
      context.clearRect(0, 0, width, height);

      const gap = 11;
      for (let x = 0; x < width; x += gap) {
        for (let y = 0; y < height; y += gap) {
          if (!isLand(x / width, y / height)) continue;
          // A little variation in opacity stops the grid reading as a screen
          // door; seeded from position so it does not shimmer between frames.
          const jitter = ((Math.sin(x * 12.9898 + y * 78.233) + 1) / 2) * 0.35;
          context.fillStyle = `rgba(232, 93, 44, ${0.16 + jitter * 0.4})`;
          context.beginPath();
          context.arc(x, y, 1.15, 0, Math.PI * 2);
          context.fill();
        }
      }

      const elapsed = (now - start) / 1000;
      const cycle = 9;

      for (const route of ROUTES) {
        const local = (elapsed - route.delay + cycle) % cycle;
        const progress = reduceMotion ? 1 : Math.max(0, Math.min(local / 2.6, 1));
        if (progress <= 0) continue;

        const a = { x: route.from[0] * width, y: route.from[1] * height };
        const b = { x: route.to[0] * width, y: route.to[1] * height };
        const control = {
          x: (a.x + b.x) / 2,
          y: Math.min(a.y, b.y) - Math.abs(b.x - a.x) * 0.35 - 12,
        };

        // The arc fades out in the last third of its cycle, so lines retire
        // gracefully rather than snapping off.
        const fade = reduceMotion ? 0.7 : local > cycle - 2 ? (cycle - local) / 2 : 1;

        context.strokeStyle = `rgba(194, 65, 12, ${0.55 * fade})`;
        context.lineWidth = 1.4;
        context.beginPath();
        context.moveTo(a.x, a.y);
        for (let t = 0; t <= progress; t += 0.02) {
          const inverse = 1 - t;
          context.lineTo(
            inverse * inverse * a.x + 2 * inverse * t * control.x + t * t * b.x,
            inverse * inverse * a.y + 2 * inverse * t * control.y + t * t * b.y,
          );
        }
        context.stroke();

        for (const point of [a, b]) {
          context.beginPath();
          context.arc(point.x, point.y, 3, 0, Math.PI * 2);
          context.fillStyle = `rgba(194, 65, 12, ${0.85 * fade})`;
          context.fill();
        }

        if (!reduceMotion && progress < 1) {
          const inverse = 1 - progress;
          const head = {
            x:
              inverse * inverse * a.x +
              2 * inverse * progress * control.x +
              progress * progress * b.x,
            y:
              inverse * inverse * a.y +
              2 * inverse * progress * control.y +
              progress * progress * b.y,
          };
          const glow = context.createRadialGradient(head.x, head.y, 0, head.x, head.y, 10);
          glow.addColorStop(0, "rgba(232,93,44,0.5)");
          glow.addColorStop(1, "rgba(232,93,44,0)");
          context.fillStyle = glow;
          context.beginPath();
          context.arc(head.x, head.y, 10, 0, Math.PI * 2);
          context.fill();
        }
      }

      if (!reduceMotion) frame = requestAnimationFrame(draw);
    };

    resize();
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
  }, []);

  return (
    <div className={`relative ${className}`} aria-hidden>
      <canvas ref={canvas} className="block h-full w-full" />
    </div>
  );
}
