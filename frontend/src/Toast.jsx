import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { IconClose } from "./icons.jsx";

// App-wide toast notifications. One <ToastProvider> wraps the app (see App.jsx),
// exposing useToast() so any component can push a transient confirmation without
// re-implementing the clear-then-set + focus + timer dance per component (that
// focus dance is the focus-restore-vs-reload race that kept biting). Toasts
// overlay (fixed), pause on hover AND focus, and can be dismissed manually.
//
// a11y contract (WCAG):
//  - 2.2.1 Timing Adjustable: success toasts auto-dismiss but pause on hover and
//    keyboard focus; ERROR toasts carry actionable text and never auto-dismiss —
//    they persist until manually dismissed.
//  - 2.4.3 Focus Order: dismissing a toast by keyboard hands focus to a sibling
//    toast (never dropping it to <body> mid-stack).
//  - 4.1.3 Status Messages: each message is queued into a live region (polite for
//    success/info, assertive for errors) so rapid toasts each announce exactly
//    once; the visual stack is NOT itself a live region and is NOT role="status"
//    (several specs assert a single unscoped getByRole("status")).
//
// This is the transient-confirmation channel ONLY. Persistent status, and inline/
// onboarding notices, stay as in-flow .notice elements.

const ToastContext = createContext(null);

const AUTO_MS = 4500;     // success/info auto-dismiss; errors persist
const RESUME_MS = 2500;   // re-arm this soon after un-hover / blur
const FADE_MS = 300;      // fade-out before unmount
const ANNOUNCE_MS = 1200; // how long a queued announcement lingers in the live region

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const [polite, setPolite] = useState([]);       // queued {id,text} announcements
  const [assertive, setAssertive] = useState([]);
  const timers = useRef({});
  const closeRefs = useRef({});
  const idRef = useRef(0);

  useEffect(() => () => { Object.values(timers.current).forEach(clearTimeout); }, []);

  const dismiss = useCallback((id, { refocus = false } = {}) => {
    clearTimeout(timers.current[id]);
    delete timers.current[id];
    setToasts((ts) => {
      // Keyboard dismissal: move focus to a sibling toast so it doesn't drop to
      // <body> mid-stack (2.4.3). Auto-dismiss can't hit a focused toast because
      // focus pauses its timer, so this only fires for a deliberate dismissal.
      if (refocus) {
        const idx = ts.findIndex((t) => t.id === id);
        const next = ts[idx + 1] ?? ts[idx - 1];
        if (next) requestAnimationFrame(() => closeRefs.current[next.id]?.focus?.());
      }
      return ts.map((t) => (t.id === id ? { ...t, leaving: true } : t));
    });
    setTimeout(() => {
      setToasts((ts) => ts.filter((t) => t.id !== id));
      delete closeRefs.current[id];
    }, FADE_MS);
  }, []);

  const arm = useCallback((id) => {
    clearTimeout(timers.current[id]);
    timers.current[id] = setTimeout(() => dismiss(id), RESUME_MS);
  }, [dismiss]);

  const push = useCallback((message, kind = "") => {
    if (!message) return;
    const id = ++idRef.current;
    setToasts((ts) => [...ts, { id, message, kind }]);
    if (kind !== "error") {
      timers.current[id] = setTimeout(() => dismiss(id), AUTO_MS); // errors persist
    }
    // Queue the announcement (assertive for errors) so concurrent toasts each
    // announce; auto-batching can't collapse distinct keyed children.
    const aid = ++idRef.current;
    const set = kind === "error" ? setAssertive : setPolite;
    set((q) => [...q, { id: aid, text: message }]);
    setTimeout(() => set((q) => q.filter((e) => e.id !== aid)), ANNOUNCE_MS);
    return id;
  }, [dismiss]);

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toast-host">
        {toasts.map((t) => (
          <div key={t.id}
               className={"toast" + (t.kind ? " " + t.kind : "") + (t.leaving ? " leaving" : "")}
               onMouseEnter={() => clearTimeout(timers.current[t.id])}
               onMouseLeave={() => { if (t.kind !== "error") arm(t.id); }}
               onFocus={() => clearTimeout(timers.current[t.id])}
               onBlur={() => { if (t.kind !== "error") arm(t.id); }}>
            <span className="toast-msg" id={`toast-msg-${t.id}`}>{t.message}</span>
            <button type="button" className="toast-close" aria-label="Dismiss"
                    aria-describedby={`toast-msg-${t.id}`}
                    ref={(el) => { closeRefs.current[t.id] = el; }}
                    onClick={() => dismiss(t.id, { refocus: true })}>
              <IconClose size={14} />
            </button>
          </div>
        ))}
      </div>
      {/* Separate live regions (bare, NOT role="status"): polite for success/info,
          assertive so an actionable error interrupts rather than waiting. */}
      <div className="sr-only" aria-live="polite">
        {polite.map((e) => <div key={e.id}>{e.text}</div>)}
      </div>
      <div className="sr-only" aria-live="assertive">
        {assertive.map((e) => <div key={e.id}>{e.text}</div>)}
      </div>
    </ToastContext.Provider>
  );
}

// Returns push(message, kind?) — kind is "" | "ok" | "error". A no-op outside a
// provider so components (and their unit tests) don't crash without one.
export function useToast() {
  return useContext(ToastContext) ?? (() => {});
}
