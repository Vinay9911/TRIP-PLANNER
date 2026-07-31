/**
 * Inline SVG icon set.
 *
 * Hand-rolled rather than pulled from an icon package for two reasons: it
 * keeps the bundle free of a dependency used for ~20 glyphs, and it lets
 * every icon share one stroke width (1.75) and one 24-unit grid, which is
 * what actually makes an icon set look coherent.
 *
 * These replace the emoji that previously stood in for interface icons.
 * Emoji render differently on every platform, cannot inherit colour or
 * stroke weight, and cannot be sized reliably - fine inside a sentence the
 * agent writes, wrong for navigation and buttons. Emoji still appear in the
 * agent's own message text, which is content rather than chrome.
 *
 * Every icon is `aria-hidden` and sized in `em`, so it scales with the text
 * beside it and is skipped by screen readers - the accessible name belongs on
 * the button that wraps it, not on the glyph.
 */

import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number | string };

function Icon({ size = "1.25em", children, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  );
}

export const IconCompass = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="m15.5 8.5-2 5-5 2 2-5z" />
  </Icon>
);

export const IconPlus = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 5v14M5 12h14" />
  </Icon>
);

export const IconChat = (p: IconProps) => (
  <Icon {...p}>
    <path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9.5 9.5 0 0 1-3.4-.6L3 21l1.7-4.6A8.4 8.4 0 0 1 12 3.1a8.4 8.4 0 0 1 9 8.4Z" />
  </Icon>
);

export const IconSparkle = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 3.5 13.7 9l5.5 1.7-5.5 1.8L12 18l-1.7-5.5L4.8 10.7 10.3 9z" />
  </Icon>
);

export const IconPlane = (p: IconProps) => (
  <Icon {...p}>
    <path d="M3 14.5 21 5l-4 8.5 2.5 6-3 1-3.5-4.5-4 2 .5 3-1.5.5-1.5-4-4-1.5.5-1.5 3 .5z" />
  </Icon>
);

export const IconBed = (p: IconProps) => (
  <Icon {...p}>
    <path d="M3 19V7M3 12h13a4 4 0 0 1 4 4v3M3 19h18M3 16h18" />
    <circle cx="7.5" cy="9.5" r="1.6" />
  </Icon>
);

export const IconTicket = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4 9V7a1 1 0 0 1 1-1h14a1 1 0 0 1 1 1v2a2.5 2.5 0 0 0 0 5v2a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-2a2.5 2.5 0 0 0 0-5Z" />
    <path d="M12 7v1.5M12 11v2M12 15.5V17" />
  </Icon>
);

export const IconFork = (p: IconProps) => (
  <Icon {...p}>
    <path d="M6 3v6a2 2 0 0 0 4 0V3M8 11v10M17 3c-1.5 1.5-2 3-2 5.5V12h3V8.5C18 6 17.5 4.5 17 3ZM17 12v9" />
  </Icon>
);

export const IconSun = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
  </Icon>
);

export const IconBrain = (p: IconProps) => (
  <Icon {...p}>
    <path d="M9.5 4a2.5 2.5 0 0 0-2.4 3.2A2.5 2.5 0 0 0 5 12a2.5 2.5 0 0 0 1.6 4.7A2.5 2.5 0 0 0 12 18V6a2 2 0 0 0-2.5-2Z" />
    <path d="M14.5 4a2.5 2.5 0 0 1 2.4 3.2A2.5 2.5 0 0 1 19 12a2.5 2.5 0 0 1-1.6 4.7A2.5 2.5 0 0 1 12 18" />
  </Icon>
);

export const IconChart = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4 20V10M10 20V4M16 20v-6M22 20H2" />
  </Icon>
);

export const IconShield = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 3 5 6v5.5c0 4.2 2.9 8 7 9.5 4.1-1.5 7-5.3 7-9.5V6Z" />
    <path d="m9.2 12 2 2 3.6-3.8" />
  </Icon>
);

export const IconTrash = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13M10 11v6M14 11v6" />
  </Icon>
);

export const IconLogout = (p: IconProps) => (
  <Icon {...p}>
    <path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 17l-5-5 5-5M5 12h11" />
  </Icon>
);

export const IconSend = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4.5 12h15M13 5.5 19.5 12 13 18.5" />
  </Icon>
);

export const IconCalendar = (p: IconProps) => (
  <Icon {...p}>
    <rect x="3.5" y="5" width="17" height="16" rx="2" />
    <path d="M3.5 10h17M8 3v4M16 3v4" />
  </Icon>
);

export const IconPin = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 21s7-5.6 7-11a7 7 0 1 0-14 0c0 5.4 7 11 7 11Z" />
    <circle cx="12" cy="10" r="2.5" />
  </Icon>
);

export const IconClock = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5.2l3.2 2" />
  </Icon>
);

export const IconUsers = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="9" cy="8" r="3.2" />
    <path d="M3.5 20a5.5 5.5 0 0 1 11 0M16 5.2a3.2 3.2 0 0 1 0 5.9M17.5 20a5.6 5.6 0 0 0-2-4.3" />
  </Icon>
);

export const IconWallet = (p: IconProps) => (
  <Icon {...p}>
    <path d="M3.5 7.5A2 2 0 0 1 5.5 5.5H18a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2Z" />
    <path d="M3.5 9.5H20M16 14h1.5" />
  </Icon>
);

export const IconChevron = (p: IconProps) => (
  <Icon {...p}>
    <path d="m9 5 7 7-7 7" />
  </Icon>
);

export const IconMenu = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4 7h16M4 12h16M4 17h16" />
  </Icon>
);

export const IconClose = (p: IconProps) => (
  <Icon {...p}>
    <path d="M6 6l12 12M18 6 6 18" />
  </Icon>
);

export const IconRefresh = (p: IconProps) => (
  <Icon {...p}>
    <path d="M20 11a8 8 0 1 0-.6 4M20 5v6h-6" />
  </Icon>
);

export const IconInfo = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 11v5M12 7.8v.2" />
  </Icon>
);

export const IconMic = (p: IconProps) => (
  <Icon {...p}>
    <rect x="9" y="2" width="6" height="12" rx="3" />
    <path d="M5 10a7 7 0 0 0 14 0M12 17v4M8 21h8" />
  </Icon>
);

export const IconCopy = (p: IconProps) => (
  <Icon {...p}>
    <rect x="8" y="8" width="12" height="12" rx="2" />
    <path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
  </Icon>
);

/** Maps a service id to its icon, so the composer and cards stay in step. */
export const SERVICE_ICONS = {
  flights: IconPlane,
  stays: IconBed,
  attractions: IconTicket,
  restaurants: IconFork,
} as const;
