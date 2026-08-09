// Does a DataTable's horizontal scroll wrapper need to be a focusable region?
//
// Extracted as a pure predicate for one reason: as an inline expression in
// DataTable.jsx it was UNTESTABLE in practice. No current consumer trips it
// (all three Allowlist tables pass `renderActions` and mark every column
// sortable), so an e2e assertion that "the Users table has no region" is also
// what the pre-fix code did — replacing the derivation with `false` left the
// whole suite green. The derivation is a guard for the NEXT consumer, so the
// thing worth pinning is the rule itself, not today's rendering of it.
//
// The rule: a scroll region is only reachable by keyboard if something inside
// it can take focus and scroll it into view. Rows with action buttons and
// sortable column headers give that at both horizontal extremes, so a region
// there is a redundant tab stop whose name duplicates the table's. A table
// with no actions column, or with any non-sortable column, has a stretch a
// keyboard cannot reach — Usage.jsx's Top users shipped exactly that.

/**
 * @param {boolean} hasActions whether the table renders a trailing actions cell
 * @param {Array<{sortable?: boolean}>} columns the column definitions
 * @returns {boolean} true when the scroll wrapper must be focusable
 */
export function needsScrollRegion(hasActions, columns) {
  if (!hasActions) return true;
  return (columns || []).some((c) => !c || !c.sortable);
}
