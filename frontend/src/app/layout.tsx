import type { Metadata, Viewport } from "next";
import { Inter, Poppins } from "next/font/google";

import "./globals.css";

/*
 * Fonts are loaded through `next/font`, which self-hosts them at build time
 * and emits a `size-adjust` fallback. That matters for two of the
 * performance rules this project follows: there is no render-blocking request
 * to a third-party font host, and the fallback metrics are matched closely
 * enough that swapping in the real face does not shift layout.
 *
 * Poppins for headings and Inter for body: geometric display paired with a
 * humanist text face, warm enough to suit the palette without the rounded
 * novelty faces that would read as a children's app.
 */
const heading = Poppins({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-heading",
  display: "swap",
});

const body = Inter({
  subsets: ["latin"],
  variable: "--font-body",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Trip Planner Agent",
  description:
    "An AI travel planner that remembers your preferences, researches destinations in depth, and plans around your requirements.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // No maximum-scale: capping zoom breaks pinch-to-zoom for anyone who needs
  // it, which is an accessibility regression for a cosmetic gain.
  themeColor: "#fdf7f8",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${heading.variable} ${body.variable}`}>
      <body className="min-h-dvh antialiased">
        {/* Decorative only, and marked so: it is a colour wash with no
            information in it, so screen readers should skip it. */}
        <div className="app-backdrop" aria-hidden />
        <div className="relative z-10">{children}</div>
      </body>
    </html>
  );
}
