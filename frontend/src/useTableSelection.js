import { useCallback, useMemo, useState } from "react";
import { selectionCount } from "./selection.js";

// Feature-owned React hook backing the bulk row-selection UI on one admin
// DataTable (Allowlist instantiates THREE of these -- users/pending/blocked --
// independent by construction). Pure counting/string logic lives in
// selection.js (vitest); this hook only owns the STATE and its transitions,
// so it's Playwright-covered end to end (frontend/e2e/admin-bulk-actions.spec.js).
//
// SELECTION MODEL (see selection.js's header comment): a selection is
// `{ mode: "explicit" | "all", selectedIds: Set }`.
//   - explicit: selectedIds holds the ids that ARE selected.
//   - all:      selectedIds holds the ids EXCLUDED from an otherwise
//               "everything matching" selection.
// A row's effective checked state never needs the full universe of ids: it's
// `mode==="all" ? !selectedIds.has(id) : selectedIds.has(id)` (see DataTable.jsx).
export function useTableSelection() {
  const [mode, setMode] = useState("explicit");
  const [selectedIds, setSelectedIds] = useState(() => new Set());

  const selection = useMemo(() => ({ mode, selectedIds }), [mode, selectedIds]);

  // Toggle ONE id. In "all" mode, checking a row un-excludes it; unchecking
  // adds it to the excluded set (narrowing "all matching" down by one).
  const toggleRow = useCallback((id, checked) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (mode === "all") {
        if (checked) next.delete(id); else next.add(id);
      } else if (checked) {
        next.add(id);
      } else {
        next.delete(id);
      }
      return next;
    });
  }, [mode]);

  // Toggle every id on the CURRENT page at once (the tri-state header
  // checkbox). Same in/exclude logic as toggleRow, applied to the whole list.
  const togglePage = useCallback((pageEligibleIds, checked) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const id of pageEligibleIds) {
        if (mode === "all") {
          if (checked) next.delete(id); else next.add(id);
        } else if (checked) {
          next.add(id);
        } else {
          next.delete(id);
        }
      }
      return next;
    });
  }, [mode]);

  // "Select all matching (including rows on pages not currently shown)" --
  // switches to all-matching mode with nothing excluded.
  const selectAllMatching = useCallback(() => {
    setMode("all");
    setSelectedIds(new Set());
  }, []);

  // Set an EXPLICIT selection outright (e.g. re-selecting only the ids a bulk
  // action's response reported as failed). An empty array is a full clear.
  const selectExplicit = useCallback((ids) => {
    setMode("explicit");
    setSelectedIds(new Set(ids));
  }, []);

  const clear = useCallback(() => {
    setMode("explicit");
    setSelectedIds(new Set());
  }, []);

  // The effective count against the current filtered/eligible universe.
  const count = useCallback((filteredEligibleIds) => (
    selectionCount({ mode, selectedIds }, filteredEligibleIds)
  ), [mode, selectedIds]);

  // The actual set of ids currently effectively selected, restricted to
  // `filteredEligibleIds` -- what a bulk action actually acts on.
  const effectiveIds = useCallback((filteredEligibleIds) => {
    const eff = new Set();
    if (mode === "all") {
      filteredEligibleIds.forEach((id) => { if (!selectedIds.has(id)) eff.add(id); });
    } else {
      selectedIds.forEach((id) => { if (filteredEligibleIds.has(id)) eff.add(id); });
    }
    return eff;
  }, [mode, selectedIds]);

  return {
    mode, selectedIds, selection,
    toggleRow, togglePage, selectAllMatching, selectExplicit, clear,
    count, effectiveIds,
    isAllMatching: mode === "all",
  };
}
