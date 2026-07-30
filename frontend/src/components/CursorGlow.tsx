"use client";

/**
 * A soft glow that trails the cursor.
 *
 * Two elements rather than one: a small dot pinned exactly to the pointer,
 * and a larger halo that eases toward it. The lag between them is what makes
 * the movement read as fluid rather than as a second cursor - matching the
 * pointer exactly would just look like a bigger arrow.
 *
 * **It never renders where it would be wrong or unwelcome.** Touch devices
 * have no persistent pointer, so a trailing glow there is a stuck artefact;
 * and anyone who has asked for reduced motion has asked for exactly this kind
 * of thing to stop. Both are checked before mounting, so the component costs
 * nothing at all in those cases.
 *
 * **It animates outside React.** Position is written straight to the DOM in a
 * `requestAnimationFrame` loop rather than through state, because a
 * re-render per mouse move would be dozens of renders a second for something
 * purely decorative. Only `transform` is touched, so it composites on the GPU
 * and cannot trigger layout.
 */

import { useEffect, useRef, useState } from "react";

export function CursorGlow() {
  const halo = useRef<HTMLDivElement>(null);
  const dot = useRef<HTMLDivElement>(null);
  const [enabled, setEnabled] = useState(false);

  useEffect(() => {
    const finePointer = window.matchMedia("(pointer: fine)").matches;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    setEnabled(finePointer && !reduceMotion);
  }, []);

  useEffect(() => {
    if (!enabled) return;

    // Start off-screen so nothing flashes in the corner before the first move.
    const target = { x: -100, y: -100 };
    const eased = { x: -100, y: -100 };
    let frame = 0;
    let scale = 1;
    let targetScale = 1;

    const onMove = (event: PointerEvent) => {
      target.x = event.clientX;
      target.y = event.clientY;
    };

    // The halo swells slightly over anything clickable, which turns the
    // decoration into a small affordance rather than pure ornament.
    const onOver = (event: PointerEvent) => {
      const element = event.target as HTMLElement | null;
      targetScale = element?.closest("a,button,[role='button'],input,textarea,summary")
        ? 1.9
        : 1;
    };

    const onDown = () => {
      targetScale = 0.75;
    };
    const onUp = () => {
      targetScale = 1;
    };

    const tick = () => {
      // Exponential smoothing: a fixed fraction of the remaining distance per
      // frame. Cheap, frame-rate independent enough for this, and it settles
      // without overshoot.
      eased.x += (target.x - eased.x) * 0.16;
      eased.y += (target.y - eased.y) * 0.16;
      scale += (targetScale - scale) * 0.18;

      if (halo.current) {
        halo.current.style.transform = `translate3d(${eased.x}px, ${eased.y}px, 0) scale(${scale})`;
      }
      if (dot.current) {
        dot.current.style.transform = `translate3d(${target.x}px, ${target.y}px, 0)`;
      }
      frame = requestAnimationFrame(tick);
    };

    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("pointerover", onOver, { passive: true });
    window.addEventListener("pointerdown", onDown, { passive: true });
    window.addEventListener("pointerup", onUp, { passive: true });
    frame = requestAnimationFrame(tick);

    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerover", onOver);
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointerup", onUp);
      cancelAnimationFrame(frame);
    };
  }, [enabled]);

  if (!enabled) return null;

  return (
    <>
      <div ref={halo} className="cursor-glow cursor-glow--halo" aria-hidden />
      <div ref={dot} className="cursor-glow cursor-glow--dot" aria-hidden />
    </>
  );
}
