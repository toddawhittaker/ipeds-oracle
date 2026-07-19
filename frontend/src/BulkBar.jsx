import React from "react";
import {
  allMatchingLabel,
  pageSelectedNotice,
  reducedMatchingLabel,
  selectAllMatchingLabel,
  selectedCountLabel,
} from "./selection.js";
import { IconClose } from "./icons.jsx";

// Contextual bulk-action toolbar, rendered via <DataTable renderSelectionBar>.
// STANDARD BULK-SELECTION PATTERN (Gmail/Linear/GitHub): the toolbar only
// EXISTS while at least one row is selected — it returns null otherwise, so a
// table with nothing selected shows no strip of disabled buttons (the old
// always-visible bar was the main complaint). When it appears:
//   • left anchor: a Clear (✕) button + one live "N selected" count (the count
//     is the sole aria-live region, WCAG 4.1.3 — no separate sr-only echo);
//   • right cluster: stable-verb action buttons (never counts baked into the
//     label — the per-action breakdown lives in the confirm dialog), with any
//     DESTRUCTIVE action (variant "danger") separated from the constructive
//     ones by a hairline divider and colored with the semantic danger token;
//   • an optional second-row BANNER — the canonical "select all matching"
//     escalation once the whole current page is selected but more rows match
//     than fit one page, and the "all N matching are selected" confirmation
//     once that escalation is active.
//
// Deliberately dumb/reusable: the feature (Admin.jsx's Allowlist) computes the
// per-action eligible/disabled state (via selection.js's partitionEligibility)
// and hands over fully-built `actions` descriptors; this only renders the
// shared count / select-all-matching / clear controls plus those actions, so
// it's reused unchanged across the three Allowlist tables.
//
// `actions`: [{ key, label, icon: Component, variant: "danger"|"warning"|
//   "neutral", disabled, title, onClick }] — always TEXT+ICON, never icon-only.
//
// `onFocusFallback` (a11y H2): Clear and Select-all-matching can BOTH disable or
// unmount THEMSELVES as the direct result of their own click — Clear empties the
// selection, which makes this whole toolbar return null (the ✕ unmounts);
// Select-all-matching flips to "all" mode, which swaps the escalation banner out
// for the confirmation banner (its own button unmounts). A control that removes
// itself drops focus to <body> (WCAG 2.4.3). `onFocusFallback` is a stable,
// always-present neighbor OUTSIDE this toolbar (Admin.jsx wires it to the
// table's DataTable.focusSearch()) that both handlers move focus to BEFORE
// triggering the state change — the same before-not-after ordering ConfirmModal
// uses.
export default function BulkBar({
  nouns, mode, count, totalEligible, pageSelectedCount, pageEligibleCount,
  onSelectAllMatching, onClear, actions, onFocusFallback,
}) {
  // Contextual: no selection, no toolbar. (In "all" mode `count` is the whole
  // matching set minus exclusions, so it's only ever 0 if every row was
  // excluded — which also means nothing is selected.)
  if (count <= 0) return null;

  function handleSelectAllMatching() {
    onFocusFallback?.();
    onSelectAllMatching();
  }
  function handleClear() {
    onFocusFallback?.();
    onClear();
  }

  // The whole current page is selected but the filtered set has more rows than
  // are selected -> offer to select every matching row (the Gmail escalation).
  const pageFullySelected = pageEligibleCount > 0 && pageSelectedCount === pageEligibleCount;
  const canEscalate = mode !== "all" && pageFullySelected && count < totalEligible;

  // Divider goes immediately before the first destructive action, splitting the
  // constructive cluster from the danger one.
  const firstDangerIdx = actions.findIndex((a) => a.variant === "danger");

  return (
    <div className="bulk-toolbar">
      <div className="bulk-toolbar-left">
        <button type="button" className="bulk-clear-icon" onClick={handleClear}
                aria-label="Clear selection" title="Clear selection">
          <IconClose size={16} />
        </button>
        <span className="bulk-count" aria-live="polite">{selectedCountLabel(count, nouns)}</span>
      </div>

      <div className="bulk-toolbar-actions">
        {actions.map((a, i) => (
          <React.Fragment key={a.key}>
            {i === firstDangerIdx && firstDangerIdx > 0
              && <span className="bulk-divider" aria-hidden="true" />}
            <button
              type="button"
              className={`bulk-action ${a.variant}`}
              disabled={a.disabled}
              title={a.disabled ? a.title : undefined}
              onClick={a.onClick}
            >
              <a.icon size={15} />
              {a.label}
            </button>
          </React.Fragment>
        ))}
      </div>

      {(canEscalate || mode === "all") && (
        <div className="bulk-banner">
          {mode === "all" ? (
            <span>
              {count === totalEligible
                ? allMatchingLabel(totalEligible, nouns)
                : reducedMatchingLabel(count, totalEligible, nouns)}
            </span>
          ) : (
            <>
              <span>{pageSelectedNotice(pageSelectedCount, nouns)}</span>
              <button type="button" className="bulk-banner-link" onClick={handleSelectAllMatching}>
                {selectAllMatchingLabel(totalEligible, nouns)}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
