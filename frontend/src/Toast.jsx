import React, { createContext, useCallback, useContext, useRef, useState } from "react";
import { IconClose } from "./icons.jsx";

// App-wide toast notifications. One <ToastProvider> wraps the app (see App.jsx),
// exposing useToast() so any component can push a transient confirmation without
// re-implementing the clear-then-set + focus + timer dance per component (that
// focus dance is the focus-restore-vs-reload race that kept biting). Toasts
// overlay (fixed), auto-dismiss with a fade, pause on hover, and can be dismissed
// manually. A single polite live region announces each one to screen readers;
// the visual stack is NOT itself a live region, so appearing toasts announce
// exactly once while their dismiss buttons stay operable.
//
// This is the transient-confirmation channel ONLY. Persistent status, errors that
// must be read/acted on, and inline/onboarding notices stay as in-flow .notice
// elements (they must not vanish on a timer or stack in a corner out of context).

const ToastContext = createContext(null);

const DURATIONS = { error: 8000, default: 4500 }; // errors linger longer to be read
const RESUME_MS = 2500; // after un-hovering, dismiss again this soon
const FADE_MS = 300;

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const [live, setLive] = useState(""); // sr-only announcement mirror
  const timers = useRef({});
  const idRef = useRef(0);

  const remove = useCallback((id) => {
    clearTimeout(timers.current[id]);
    delete timers.current[id];
    // Fade out first, then unmount.
    setToasts((ts) => ts.map((t) => (t.id === id ? { ...t, leaving: true } : t)));
    setTimeout(() => setToasts((ts) => ts.filter((t) => t.id !== id)), FADE_MS);
  }, []);

  const arm = useCallback((id, ms) => {
    clearTimeout(timers.current[id]);
    timers.current[id] = setTimeout(() => remove(id), ms);
  }, [remove]);

  const push = useCallback((message, kind = "") => {
    if (!message) return;
    const id = ++idRef.current;
    setToasts((ts) => [...ts, { id, message, kind }]);
    arm(id, kind === "error" ? DURATIONS.error : DURATIONS.default);
    // Re-announce even identical text (React bails on an unchanged value):
    // clear, then set next frame to force a real live-region mutation.
    setLive("");
    requestAnimationFrame(() => setLive(message));
    return id;
  }, [arm]);

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toast-host" aria-label="Notifications">
        {toasts.map((t) => (
          <div key={t.id}
               className={"toast" + (t.kind ? " " + t.kind : "") + (t.leaving ? " leaving" : "")}
               onMouseEnter={() => clearTimeout(timers.current[t.id])}
               onMouseLeave={() => arm(t.id, RESUME_MS)}>
            <span className="toast-msg">{t.message}</span>
            <button type="button" className="toast-close" aria-label="Dismiss"
                    onClick={() => remove(t.id)}>
              <IconClose size={14} />
            </button>
          </div>
        ))}
      </div>
      {/* Bare aria-live, NOT role="status": several e2e specs assert an unscoped
          getByRole("status") expecting exactly one match (see App.jsx's route-
          announcer comment). aria-live alone is still a real live region. */}
      <div className="sr-only" aria-live="polite">{live}</div>
    </ToastContext.Provider>
  );
}

// Returns push(message, kind?) — kind is "" | "ok" | "error". A no-op outside a
// provider so components (and their unit tests) don't crash without one.
export function useToast() {
  return useContext(ToastContext) ?? (() => {});
}
