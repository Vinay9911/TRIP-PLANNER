"use client";

/**
 * A real photograph of a real place, fetched lazily.
 *
 * This lived inside `Itinerary.tsx` as a private helper, while a component in
 * `ui.tsx` called `PlaceImage` served a *random* picture from picsum seeded on
 * the name. Both looked like "the place image component" at an import site,
 * and the option gallery picked the wrong one - which is why a card labelled
 * Lucknow showed a cactus and Varanasi showed a suspension bridge.
 *
 * Naming was the whole bug, so the decorative one is now `DecorativeArt` and
 * this is the only thing called a photo. If it says photo, it is one.
 *
 * Fetched on approach rather than on mount: a ten-day plan names fifty places
 * and only the visible ones are worth a request.
 */

import { useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";

export function PlacePhoto({
  name,
  destination,
  latitude,
  longitude,
  kind,
  className = "",
  eager = false,
}: {
  name: string;
  destination: string;
  latitude: number | null;
  longitude: number | null;
  kind: string;
  className?: string;
  /** Skip the viewport wait. For anything opened deliberately - a lightbox -
   *  where the observer's "is it near the viewport" question has already been
   *  answered by the user clicking on it, and waiting on it just makes the
   *  panel appear empty for a beat. */
  eager?: boolean;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [representative, setRepresentative] = useState(false);
  const [failed, setFailed] = useState(false);
  const holder = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = holder.current;
    if (!node) return;

    let cancelled = false;

    const fetchNow = () =>
      api
        .placeImage({ name, destination, latitude, longitude, kind })
        .then((image) => {
          if (cancelled) return;
          setUrl(image.url);
          setRepresentative(image.is_representative);
        })
        .catch(() => {
          if (!cancelled) setFailed(true);
        });

    if (eager) {
      void fetchNow();
      return () => {
        cancelled = true;
      };
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries[0]?.isIntersecting) return;
        observer.disconnect();
        // A missing photo is a cosmetic loss - leave the space blank rather
        // than surfacing an error next to a perfectly good plan.
        void fetchNow();
      },
      { rootMargin: "200px" },
    );

    observer.observe(node);
    return () => {
      cancelled = true;
      observer.disconnect();
    };
  }, [name, destination, latitude, longitude, kind, eager]);

  if (failed) return null;

  return (
    <div ref={holder} className={`relative overflow-hidden rounded-xl ${className}`}>
      {url ? (
        <>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={url}
            alt=""
            aria-hidden
            loading="lazy"
            className="h-full w-full object-cover"
            onError={() => setFailed(true)}
          />
          {representative && (
            <span
              title="A similar place, not a photo of this exact one."
              className="absolute bottom-1 right-1 rounded-md bg-black/55 px-1.5 py-0.5 text-[9px] font-medium text-white"
            >
              similar
            </span>
          )}
        </>
      ) : (
        <div className="skeleton h-full w-full" />
      )}
    </div>
  );
}
