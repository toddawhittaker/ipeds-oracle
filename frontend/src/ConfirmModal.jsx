import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { IconTrash, IconWarning } from "./icons.jsx";
import { useToast } from "./Toast.jsx";

// App-wide confirmation modal. One <ConfirmProvider> wraps the app (see App.jsx,
// INSIDE <ToastProvider> so the dialog can push result toasts), exposing
// useConfirm() so any component asks for confirmation without re-implementing the
// overlay / focus-trap / inert-background / async-processing dance. This is the
// SINGLE confirmation mechanism -- never window.confirm() anywhere.
//
// Feature code supplies content, severity, the action callback, and result
// messages; the component owns dimming, focus management, dismissal, loading
// state, in-modal error + retry, and accessibility.
//
// a11y contract (WCAG):
//  - role="alertdialog" for warning/danger (immediate attention), role="dialog"
//    for neutral; aria-modal, aria-labelledby (title), aria-describedby (body).
//  - 2.4.3 Focus Order: focus moves into the modal on open, is TRAPPED while
//    active, and returns to the opening control on cancel/dismiss. On SUCCESS the
//    opener typically unmounts (its row is removed), so the feature's onSuccess
//    owns the post-reload focus target instead (the focus-restore-vs-reload race).
//  - The destructive action is NOT auto-focused (focus lands on Cancel), so an
//    incidental Enter can't fire it.
//  - Background is made `inert` + aria-hidden while the modal is open, so it's
//    unavailable to pointer, keyboard, and assistive tech.
//  - Processing exposed via aria-busy on the confirm button; a failure renders a
//    role="alert" error inside the modal (supplementing the error toast).

const ConfirmContext = createContext(null);

const DEFAULT_ICON = { danger: IconTrash, warning: IconWarning, neutral: null };

let idSeq = 0;

// Visible, enabled, focusable descendants -- the trap's cycle set.
function focusables(root) {
  if (!root) return [];
  const sel = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';
  return [...root.querySelectorAll(sel)].filter(
    (el) => !el.disabled && el.getAttribute("aria-hidden") !== "true",
  );
}

// A FastAPI error body arrives as err.message = JSON like {"detail": "..."}.
// Surface that detail in-modal when present, else a supplied fallback.
function extractError(err, fallback) {
  try {
    const detail = JSON.parse(err?.message)?.detail;
    if (detail) return detail;
  } catch { /* not a JSON body -- use the fallback */ }
  return fallback;
}

function ConfirmDialog({ req, onClose }) {
  const toast = useToast();
  const {
    variant = "neutral",
    title,
    body,
    details,
    confirmLabel,
    cancelLabel = "Cancel",
    onConfirm,
    onSuccess,
    successToast,
    errorToast,
    busyLabel = "Working…",
    dismissable = true,
    focusConfirm = false,
    role,
    icon,
  } = req;

  const [processing, setProcessing] = useState(false);
  const [error, setError] = useState("");
  const dialogRef = useRef(null);
  const cancelRef = useRef(null);
  const confirmRef = useRef(null);
  const openerRef = useRef(null);
  // Stable, unique ids for aria-labelledby/-describedby (computed once).
  const [ids] = useState(() => {
    const n = ++idSeq;
    return { title: `confirm-title-${n}`, body: `confirm-body-${n}`, error: `confirm-error-${n}` };
  });

  const dialogRole = role || (variant === "neutral" ? "dialog" : "alertdialog");
  const Icon = icon !== undefined ? icon : DEFAULT_ICON[variant];

  // Open: remember the opener, inert + hide the background, move focus in.
  // (Setting inert blurs the opener, so capture it FIRST.)
  useEffect(() => {
    openerRef.current = document.activeElement;
    const appEl = document.querySelector(".app");
    appEl?.setAttribute("inert", "");
    appEl?.setAttribute("aria-hidden", "true");
    const target = (focusConfirm ? confirmRef.current : cancelRef.current) || dialogRef.current;
    target?.focus();
    // Safety net: whatever unmounts us, un-inert the background.
    return () => {
      appEl?.removeAttribute("inert");
      appEl?.removeAttribute("aria-hidden");
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const unInert = () => {
    const appEl = document.querySelector(".app");
    appEl?.removeAttribute("inert");
    appEl?.removeAttribute("aria-hidden");
    return appEl;
  };

  function cancel() {
    if (processing) return;
    // Return focus to the opener (still mounted -- nothing mutated). Must
    // un-inert BEFORE focusing or the inert subtree refuses focus.
    unInert();
    const opener = openerRef.current;
    if (opener && document.contains(opener)) {
      requestAnimationFrame(() => opener.focus?.());
    }
    onClose();
  }

  async function confirmAction() {
    if (processing) return;
    // Move focus to the dialog container BEFORE the re-render disables the
    // buttons -- disabling the focused button would otherwise blur it to <body>.
    dialogRef.current?.focus();
    setProcessing(true);
    setError("");
    try {
      await onConfirm?.();
    } catch (err) {
      setProcessing(false);
      setError(extractError(err, errorToast || "That didn't work. Please try again."));
      if (errorToast) toast(errorToast, "error");
      // Re-enabled now -- put focus back on the confirm button so a retry is one
      // keystroke away (and never stranded on <body>).
      requestAnimationFrame(() => confirmRef.current?.focus?.());
      return; // stay open for retry/cancel
    }
    // Success: close first (un-inert), toast, then let the feature place focus.
    if (successToast) toast(successToast, "ok");
    unInert();
    onClose();
    onSuccess?.();
  }

  function onKeyDown(e) {
    if (e.key === "Escape") {
      // The modal owns Escape while open (processing or not) -- never let it leak
      // out of the trap to an ancestor handler.
      e.stopPropagation();
      if (dismissable && !processing) cancel();
      return;
    }
    if (e.key !== "Tab") return;
    const items = focusables(dialogRef.current);
    if (items.length === 0) { e.preventDefault(); return; }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (!dialogRef.current.contains(active)) {
      e.preventDefault();
      first.focus();
    } else if (e.shiftKey && active === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  }

  function onOverlayDown(e) {
    // Only a press on the scrim itself (not the panel) dismisses, and only
    // before processing begins.
    if (e.target === e.currentTarget && dismissable && !processing) cancel();
  }

  return createPortal(
    <div className="modal-overlay" onMouseDown={onOverlayDown}>
      <div
        className={`modal ${variant}`}
        role={dialogRole}
        aria-modal="true"
        aria-labelledby={ids.title}
        aria-describedby={error ? `${ids.body} ${ids.error}` : ids.body}
        ref={dialogRef}
        tabIndex={-1}
        onKeyDown={onKeyDown}
      >
        <div className="modal-head">
          {Icon && <span className={`modal-icon ${variant}`}><Icon size={20} /></span>}
          <h2 className="modal-title" id={ids.title}>{title}</h2>
        </div>
        <div className="modal-body" id={ids.body}>
          {body}
          {details && <div className="modal-details">{details}</div>}
        </div>
        {error && <div className="notice error modal-error" id={ids.error} role="alert">{error}</div>}
        {/* Polite busy announcement so a screen-reader user hears the in-flight
            state -- aria-busy on the (just-blurred) confirm button isn't a live
            message and the spinner is aria-hidden (WCAG 4.1.3). */}
        <div className="sr-only" aria-live="polite">{processing ? busyLabel : ""}</div>
        <div className="modal-actions">
          <button
            type="button"
            className="modal-cancel"
            ref={cancelRef}
            onClick={cancel}
            disabled={processing}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`modal-confirm ${variant}`}
            ref={confirmRef}
            onClick={confirmAction}
            disabled={processing}
            aria-busy={processing || undefined}
          >
            {processing && <span className="spinner" aria-hidden="true" />}
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

export function ConfirmProvider({ children }) {
  const [req, setReq] = useState(null);
  const confirm = useCallback((options) => setReq(options), []);
  const close = useCallback(() => setReq(null), []);
  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {req && <ConfirmDialog req={req} onClose={close} />}
    </ConfirmContext.Provider>
  );
}

// Returns confirm(options) -- see the header for the options contract. A no-op
// outside a provider so components (and their unit tests) don't crash without one.
export function useConfirm() {
  return useContext(ConfirmContext) ?? (() => {});
}
