import React from "react";
import {
  allMatchingLabel,
  reducedMatchingLabel,
  selectAllMatchingLabel,
  selectedCountLabel,
} from "./selection.js";

// Generic bulk-action bar, rendered via <DataTable renderSelectionBar>.
// Deliberately dumb/reusable: the feature (Admin.jsx's Allowlist component)
// computes the per-action eligible/skipped counts (via selection.js's
// partitionEligibility) and hands over fully-built `actions` descriptors —
// this component only renders the shared selected-count / select-all-
// matching / clear-selection controls plus whatever actions it's given, so
// it's reused unchanged across the three Allowlist tables (Users, Pending
// requests, Blocked users).
//
// `actions`: [{ key, label, icon: Component, variant: "danger"|"warning"|
//   "neutral", disabled, onClick }] — always TEXT+ICON, never icon-only
//   (WCAG-friendly and matches the rest of the admin UI's action buttons).
// The selected-count status line is itself the aria-live region (WCAG
// 4.1.3) — no separate sr-only echo needed since it's already the visible copy.
//
// `onFocusFallback` (a11y H2): both "Select all matching" and "Clear
// selection" can disable THEMSELVES as a direct result of their own click --
// Select-all-matching flips the selection to "all" mode, which disables it;
// Clear always empties the selection, which puts it right back at count===0
// (explicit mode), disabling it too. A disabled control that was the one
// just clicked drops focus to <body> (WCAG 2.4.3). `onFocusFallback` is a
// stable, ALWAYS-enabled neighbor OUTSIDE this bar (Admin.jsx wires it to the
// table's DataTable.focusSearch()) that both handlers move focus to BEFORE
// triggering the state change that disables the just-clicked control -- the
// same before-not-after ordering ConfirmModal uses ("Move focus to the
// dialog container BEFORE the re-render disables the buttons"). A target
// INSIDE this bar (e.g. handing Select-all-matching's focus to the Clear
// button) isn't reliable: Select-all-matching is commonly clicked with
// nothing individually checked yet, in which case Clear is ITSELF disabled
// (count===0) and can't receive focus at all.
export default function BulkBar({
  nouns, mode, count, totalEligible, onSelectAllMatching, onClear, actions,
  onFocusFallback,
}) {
  const countLabel = mode === "all"
    ? (count === totalEligible
      ? allMatchingLabel(totalEligible, nouns)
      : reducedMatchingLabel(count, totalEligible, nouns))
    : (count > 0 ? selectedCountLabel(count, nouns) : "");

  function handleSelectAllMatching() {
    onFocusFallback?.();
    onSelectAllMatching();
  }
  function handleClear() {
    onFocusFallback?.();
    onClear();
  }

  return (
    <div className="bulk-bar">
      <div className="bulk-bar-status" aria-live="polite">{countLabel}</div>
      <div className="bulk-bar-actions">
        {actions.map((a) => (
          <button
            key={a.key}
            type="button"
            className={`bulk-action ${a.variant}`}
            disabled={a.disabled}
            onClick={a.onClick}
          >
            <a.icon size={15} />
            {a.label}
          </button>
        ))}
        {mode !== "all" && totalEligible > 0 && (
          <button type="button" className="bulk-select-all link" onClick={handleSelectAllMatching}>
            {selectAllMatchingLabel(totalEligible, nouns)}
          </button>
        )}
        <button
          type="button"
          className="bulk-clear link"
          disabled={mode === "explicit" && count === 0}
          onClick={handleClear}
        >
          Clear selection
        </button>
      </div>
    </div>
  );
}
