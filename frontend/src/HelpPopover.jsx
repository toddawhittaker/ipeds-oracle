import React, { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
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
/**
 * @typedef {object} HelpPopoverProps
 * @property {string} label Accessible name for the trigger button.
 * @property {React.ReactNode} children Popover body.
 * @property {React.ComponentType<{ size?: number }>} [icon] Trigger glyph. Defaults
 *   to IconHelp; pass any icon from this system.
 * @property {string} [className]
 */

/** @param {HelpPopoverProps} props */
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

  // Escape must dismiss even a HOVER-opened popover (WCAG 1.4.13 dismissable). The
  // wrapper's onKeyDown only fires when the trigger has focus; a popover opened by
  // pointer leaves focus elsewhere, so bind a document-level listener while open.
  // (When focus IS on the trigger, the wrapper handler stops propagation first, so
  // this doesn't double-fire.)
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);
  // A touch tap ends in a click, and without this the click would toggle shut
  // whatever the tap just opened — so a tap could never open the help at all.
  // Armed from POINTERDOWN rather than from focus: Chromium emits a tap as
  // pointerdown -> touchstart -> mouseenter/mousedown -> focus -> click, so by
  // the time onFocus runs, the wrapper's mouseenter has already committed
  // setOpen(true) and an `if (!open)` test there reads TRUE — the latch never
  // armed and every tap closed the popover it had just opened. pointerdown
  // lands before that compat mouseenter, so `open` is reliably still false.
  const swallowClick = useRef(false);

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
      // Focus is tracked on the WRAPPER, not on the trigger alone. The popover's
      // own CONTENT is focusable — the CSV format example is a tabIndex=0
      // role=region, added so a keyboard user can scroll it (WCAG 2.1.1) — and
      // with the tracking on the trigger, Tabbing into that region blurred the
      // trigger, fired closeSoon(), and unmounted the region out from under the
      // focused element 140ms later, dropping focus to <body>. So the single
      // control the region exists for could never actually reach it, and trying
      // stranded you at the top of the document.
      //
      // Capture phase for the same reason Login.jsx's gallery uses it: it fires
      // for focus moving anywhere inside the wrapper, so trigger -> content
      // (blur then focus, in that order) lands as closeSoon() then openNow(),
      // and openNow clears the pending timer. Focus leaving the wrapper
      // entirely gets no following focus event, so the close stands.
      onFocusCapture={() => { focus.current = true; openNow(); }}
      onBlurCapture={() => { focus.current = false; closeSoon(); }}
      onKeyDown={(e) => { if (e.key === "Escape" && open) { setOpen(false); e.stopPropagation(); } }}
    >
      <button
        type="button"
        className="help-trigger"
        aria-label={label}
        aria-describedby={id}
        onClick={() => {
          // Swallow the click that ends the tap which just opened this;
          // otherwise toggle (a second tap, or a mouse click, to dismiss).
          if (swallowClick.current) { swallowClick.current = false; return; }
          setOpen((o) => !o);
        }}
        // The wrapper above owns focus.current / openNow / closeSoon. What stays
        // here is only the tap bookkeeping, which is specific to THIS element:
        // it must not arm when focus lands on the popover's content, or a
        // subsequent click on the trigger would be wrongly swallowed.
        //
        // `pointerType !== "mouse"` keeps a genuine mouse click toggling: for a
        // mouse user, hover-opens-then-click-closes is this onClick's intent.
        // The `!open` test is what lets a SECOND tap dismiss — arming on every
        // touch pointerdown would swallow that one too, and since a second tap
        // fires no new focus, nothing would ever clear the latch and the
        // popover could never be closed by touch at all.
        onPointerDown={(e) => {
          if (e.pointerType !== "mouse" && !open) { swallowClick.current = true; }
        }}
        // A tap that drags away fires neither click nor, reliably, blur — so
        // clear on cancel too, or the latch survives to swallow a later click.
        onPointerCancel={() => { swallowClick.current = false; }}
        onBlur={() => { swallowClick.current = false; }}
      >
        <Icon />
      </button>
      <div id={id} ref={popRef} role="tooltip" className="help-popover" hidden={!open}>
        {children}
      </div>
    </span>
  );
}
