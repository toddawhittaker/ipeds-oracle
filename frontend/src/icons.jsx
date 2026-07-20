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
