import React, { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { IconClose } from "./icons.jsx";
import Chart from "./Chart.jsx";

// Visible, enabled, focusable descendants — the focus-trap cycle set.
function focusables(root) {
  if (!root) return [];
  const sel = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';
  return [...root.querySelectorAll(sel)].filter(
    (el) => !el.disabled && el.getAttribute("aria-hidden") !== "true");
}

// The "maximize" dialog: one chart shown large, so a narrow side-by-side chart can
// be inspected at full width. Reuses the ConfirmModal a11y pattern — focus moves in,
// is trapped, Escape/overlay/Close dismiss, the background is `inert`, and focus
// returns to the opener on close. The inner <Chart inModal> hides its own maximize
// control and renders taller; initial* carry the opener's current type/trend/labels
// so the modal opens showing the same view. (Chart ↔ ChartModal is an intentional
// cyclic import — resolved at render time, never at module top level.)
export default function ChartModal({ spec, initialType, initialTrend, initialLabels, onClose }) {
  const dialogRef = useRef(null);
  const closeRef = useRef(null);
  const openerRef = useRef(null);

  // Open: remember the opener, inert + hide the background, move focus in. On unmount
  // (any dismissal path): un-inert and return focus to the opener.
  useEffect(() => {
    openerRef.current = document.activeElement;
    const appEl = document.querySelector(".app");
    appEl?.setAttribute("inert", "");
    appEl?.setAttribute("aria-hidden", "true");
    (closeRef.current || dialogRef.current)?.focus();
    return () => {
      appEl?.removeAttribute("inert");
      appEl?.removeAttribute("aria-hidden");
      const opener = openerRef.current;
      if (opener && document.contains(opener)) requestAnimationFrame(() => opener.focus?.());
    };
  }, []);

  function onKeyDown(e) {
    if (e.key === "Escape") { e.stopPropagation(); onClose(); return; }
    if (e.key !== "Tab") return;
    const items = focusables(dialogRef.current);
    if (items.length === 0) { e.preventDefault(); return; }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (!dialogRef.current.contains(active)) { e.preventDefault(); first.focus(); }
    else if (e.shiftKey && active === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && active === last) { e.preventDefault(); first.focus(); }
  }

  function onOverlayDown(e) {
    if (e.target === e.currentTarget) onClose();
  }

  return createPortal(
    <div className="modal-overlay" onMouseDown={onOverlayDown}>
      {/* aria-label (not a visible heading) names the dialog — the chart draws its
          own title, so a modal <h2> would duplicate it. */}
      <div className="modal chart-modal" role="dialog" aria-modal="true"
           aria-label={spec?.title || "Chart"} ref={dialogRef} tabIndex={-1}
           onKeyDown={onKeyDown}>
        <div className="chart-modal-head">
          <button type="button" className="chart-ico-btn modal-x" ref={closeRef}
                  onClick={onClose} title="Close" aria-label="Close">
            <IconClose />
          </button>
        </div>
        <div className="chart-modal-body">
          <Chart spec={spec} inModal initialType={initialType}
                 initialTrend={initialTrend} initialLabels={initialLabels} />
        </div>
      </div>
    </div>,
    document.body,
  );
}
