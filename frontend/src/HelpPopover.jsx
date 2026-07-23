import React, { useId, useLayoutEffect, useRef, useState } from "react";
import { IconHelp } from "./icons.jsx";

// A small hoverable/focusable help popover (WCAG 1.4.13: content is hoverable,
// persistent, and dismissable). The plain `.tip` CSS tooltip can't serve here —
// it's a pointer-events:none ::after that vanishes the instant the pointer leaves
// and can't hold multi-line content. This renders a REAL popover node the pointer
// can move into (trigger + popover share one wrapper, so travelling between them
// never leaves the hover region), and it opens on hover, keyboard focus, and tap.
//
// `label` is the trigger's accessible name; `children` is the help content (also
// exposed to screen readers via the trigger's aria-describedby). `icon` overrides
// the trigger glyph (defaults to the "?" help mark; the Usage stats pass the "ⓘ"
// info mark), and `className` adds a wrapper modifier (e.g. "help-compact" for a
// smaller inline trigger). State/close behaviour lives here; the visual styling is
// `.help`/`.help-popover` in styles.css.
export default function HelpPopover({ label, children, icon: Icon = IconHelp, className = "" }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const timer = useRef(null);
  const hover = useRef(false);
  const focus = useRef(false);
  // The popover is anchored right:0 (grows leftward), which runs off-screen for a
  // trigger near the LEFT edge — e.g. the left-column Usage stats. On open, measure
  // it and nudge horizontally so it stays inside the viewport, whichever edge it
  // would have overflowed. This is a pure visual DOM correction, so it writes
  // transform straight to the node (no state → no cascading render); the transform
  // is cleared to "" before measuring so each pass reads the natural position.
  const popRef = useRef(null);
  useLayoutEffect(() => {
    const el = popRef.current;
    if (!el) return;
    el.style.transform = "";
    if (!open) return;
    const rect = el.getBoundingClientRect();
    const m = 8; // keep this gap from the viewport edge
    let dx = 0;
    if (rect.left < m) dx = m - rect.left;
    else if (rect.right > window.innerWidth - m) dx = window.innerWidth - m - rect.right;
    if (dx) el.style.transform = `translateX(${Math.round(dx)}px)`;
  }, [open]);
  // On a touch tap the button fires focus THEN click; without this, focus opens
  // it and the very next click toggles it right back shut, so a tap never opens
  // the help. Track a focus-open so the click that follows it is a no-op.
  const openedByFocus = useRef(false);

  const clear = () => { if (timer.current) { clearTimeout(timer.current); timer.current = null; } };
  const openNow = () => { clear(); setOpen(true); };
  // A short delay before closing so a transient hover/focus gap (e.g. the pointer
  // grazing the popover's edge) doesn't flicker it shut; only actually close if
  // neither hover nor focus is still holding it open.
  const closeSoon = () => {
    clear();
    timer.current = setTimeout(() => {
      if (!hover.current && !focus.current) setOpen(false);
    }, 140);
  };

  return (
    <span
      className={"help" + (className ? " " + className : "")}
      onMouseEnter={() => { hover.current = true; openNow(); }}
      onMouseLeave={() => { hover.current = false; closeSoon(); }}
      onKeyDown={(e) => { if (e.key === "Escape" && open) { setOpen(false); e.stopPropagation(); } }}
    >
      <button
        type="button"
        className="help-trigger"
        aria-label={label}
        aria-describedby={id}
        onClick={() => {
          // Swallow the click that immediately follows a focus-open (touch tap);
          // otherwise toggle (an explicit second tap / mouse click to dismiss).
          if (openedByFocus.current) { openedByFocus.current = false; return; }
          setOpen((o) => !o);
        }}
        onFocus={() => { focus.current = true; if (!open) { openedByFocus.current = true; } openNow(); }}
        onBlur={() => { focus.current = false; openedByFocus.current = false; closeSoon(); }}
      >
        <Icon />
      </button>
      <div id={id} ref={popRef} role="tooltip" className="help-popover" hidden={!open}>
        {children}
      </div>
    </span>
  );
}
