import React from "react";

// Small inline stroke icons (currentColor, so they inherit button text color).
function Svg({ size = 15, children, ...rest }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
         stroke="currentColor" strokeWidth="2" strokeLinecap="round"
         strokeLinejoin="round" aria-hidden="true" focusable="false" {...rest}>
      {children}
    </svg>
  );
}

export const IconTrash = (p) => (
  <Svg {...p}>
    <path d="M3 6h18" />
    <path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" />
    <path d="M19 6v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6" />
    <path d="M10 11v6M14 11v6" />
  </Svg>
);

export const IconClose = (p) => (<Svg {...p}><path d="M6 6l12 12M18 6L6 18" /></Svg>);

// Approve a pending access request (checkmark).
export const IconCheck = (p) => (<Svg {...p}><path d="M20 6 9 17l-5-5" /></Svg>);

// Allow a blocked address to request access again (open padlock).
export const IconUnlock = (p) => (
  <Svg {...p}>
    <rect x="3" y="11" width="18" height="11" rx="2" />
    <path d="M7 11V7a5 5 0 0 1 9.9-1" />
  </Svg>
);

export const IconSend = (p) => (<Svg {...p}><path d="M4 12h14M12 5l7 7-7 7" /></Svg>);

export const IconEdit = (p) => (
  <Svg {...p}>
    <path d="M12 20h9" />
    <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
  </Svg>
);

export const IconCopy = (p) => (
  <Svg {...p}>
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M5 15V5a2 2 0 0 1 2-2h8" />
  </Svg>
);

// Data-labels toggle on a chart (a price-tag glyph).
export const IconTag = (p) => (
  <Svg {...p}>
    <path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L2 12V2h10l8.6 8.6a2 2 0 0 1 0 2.8z" />
    <path d="M7 7h.01" />
  </Svg>
);

// Maximize a chart into a modal (arrows to the four corners).
export const IconMaximize = (p) => (
  <Svg {...p}>
    <path d="M8 3H5a2 2 0 0 0-2 2v3" />
    <path d="M16 3h3a2 2 0 0 1 2 2v3" />
    <path d="M8 21H5a2 2 0 0 1-2-2v-3" />
    <path d="M16 21h3a2 2 0 0 0 2-2v-3" />
  </Svg>
);

export const IconRerun = (p) => (
  <Svg {...p}>
    <path d="M21 12a9 9 0 1 1-2.64-6.36" />
    <path d="M21 3v6h-6" />
  </Svg>
);

// Shield + / shield − for promote-to-admin / remove-admin. Shields (not up/down
// arrows) so they never read as the table's sort carets.
export const IconShieldPlus = (p) => (
  <Svg {...p}>
    <path d="M12 3l7 3v5c0 4.5-3 7.4-7 9-4-1.6-7-4.5-7-9V6l7-3z" />
    <path d="M12 8.5v5M9.5 11h5" />
  </Svg>
);

export const IconShieldMinus = (p) => (
  <Svg {...p}>
    <path d="M12 3l7 3v5c0 4.5-3 7.4-7 9-4-1.6-7-4.5-7-9V6l7-3z" />
    <path d="M9.5 11h5" />
  </Svg>
);

// Plain shield for the Admin menu item (no +/- — that's the promote/demote pair).
export const IconShield = (p) => (
  <Svg {...p}>
    <path d="M12 3l7 3v5c0 4.5-3 7.4-7 9-4-1.6-7-4.5-7-9V6l7-3z" />
  </Svg>
);

// Upload glyph (tray + up-arrow) for the CSV drop target.
export const IconUpload = (p) => (
  <Svg {...p}>
    <path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
    <path d="M12 15V3M7 8l5-5 5 5" />
  </Svg>
);

// Exclamation-in-triangle for warning/danger confirmation modals.
export const IconWarning = (p) => (
  <Svg {...p}>
    <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
    <path d="M12 9v4M12 17h.01" />
  </Svg>
);

// Sun / moon for the light-dark theme toggle (replaces the old ☀️/🌙 emoji).
export const IconSun = (p) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
  </Svg>
);

export const IconMoon = (p) => (
  <Svg {...p}>
    <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
  </Svg>
);

// GitHub mark (filled). Uses its own <svg> — the shared Svg helper is stroke-only.
export const IconGitHub = ({ size = 18, ...rest }) => (
  <svg width={size} height={size} viewBox="0 0 16 16" fill="currentColor"
       aria-hidden="true" focusable="false" {...rest}>
    <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.65-.89-3.65-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.65 7.65 0 0 1 2-.27c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/>
  </svg>
);

// Info "i" in a circle for the About dialog trigger.
export const IconInfo = (p) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 11v5M12 8h.01" />
  </Svg>
);

// Leave / sign out (door + arrow).
export const IconSignOut = (p) => (
  <Svg {...p}>
    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
    <path d="M16 17l5-5-5-5M21 12H9" />
  </Svg>
);

// Question mark in a circle for the format-help popover trigger.
export const IconHelp = (p) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M9.5 9a2.5 2.5 0 0 1 4.5 1.5c0 1.5-2 2-2 3.5" />
    <path d="M12 17.5h.01" />
  </Svg>
);
