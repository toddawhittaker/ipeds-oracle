import React, { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { filterRows, viewRows } from "./datatable.js";
import { pageHeaderState } from "./selection.js";
import { IconClose } from "./icons.jsx";

// Reusable admin data table: search + sortable headers + pagination + a polite
// aria-live status + focus management, driven by a column config and the
// datatable.js pipeline. The pure data logic lives in datatable.js (vitest); the
// browser behaviour here (focus, aria-sort announce, pager focus, filler rows)
// is covered by Playwright (users-table.spec.js and the access-request specs).
//
// Props:
//   rows            already-fetched array (filter/sort/paginate are client-side)
//   columns         [{ key, label, sortable, colClass, thClass, cellClass,
//                      cellTitle(row), render(row) }]  (render defaults to row[key])
//   rowKey(row)     stable unique key — React key AND focus targeting
//   config          datatable.js config { fields, comparators, tiebreak, nouns }
//   searchPlaceholder / searchLabel / searchId
//   emptyNoData     shown when there are NO rows at all
//   emptyNoMatch    shown when a search filters everything out
//   initialSort     { key, dir }   (defaults to the first sortable column, asc)
//   pageSizes=[10,25,50,100]  defaultPageSize=25   sizeLabel (sr-only)
//   ariaLabel       accessible name for the <table>
//   renderActions(row)  content of the trailing actions <td> (omit for no actions)
//   sortLabels      { key: spoken-name } for the live "Sorted by …" announcement
//
// Imperative handle (ref): focusSearch(), focusRowAction(rowKey) — the latter
// focuses the first enabled button in that row's actions cell, so features never
// manage per-row refs. Focus targets OUTSIDE the table stay owned by the feature.

/**
 * NOTE ON SHAPE: prop sub-types are written INLINE, not as named @typedefs. The
 * design-sync converter fully resolves types into the published contract and
 * prints a named alias by name — so a named typedef here emits as a reference to
 * something the published .d.ts never defines, and the design agent sees an
 * unresolvable type. Keep these inline.
 *
 * Per-prop doc comments are capped at 120 chars downstream, so lead with the
 * actionable half of any warning.
 *
 * @typedef {object} DataTableProps
 * @property {Array<Record<string, unknown>>} rows All rows, unpaginated — this component owns search, sort and paging client-side.
 * @property {Array<{ key: string, label: string, sortable?: boolean, render?: (row: any) => React.ReactNode, cellTitle?: (row: any) => string, colClass?: string, thClass?: string, cellClass?: string }>} columns
 *   `render` defaults to row[key].
 * @property {(row: any) => string | number} rowKey A FUNCTION returning a row's unique key, e.g. `(r) => r.email` — NOT a field name. Also the focus target.
 * @property {{ fields: string[], comparators: Record<string, (a: any, b: any) => number>, tiebreak: (row: any) => string | number, nouns: { one: string, many: string } }} config
 *   `comparators` + `tiebreak` are REQUIRED — sortRows does Object.keys(comparators), so omitting them renders a blank table.
 * @property {string} [searchPlaceholder]
 * @property {string} [searchLabel]
 * @property {string} [searchId]
 * @property {string} [emptyNoData] Shown when there are no rows at all — keep it DISTINCT from a load failure, which must never look like an empty result.
 * @property {string} [emptyNoMatch] Shown when the search filtered everything out.
 * @property {{ key: string, dir?: "asc" | "desc" }} [initialSort] Defaults to the first sortable column, ascending.
 * @property {number[]} [pageSizes]
 * @property {number} [defaultPageSize]
 * @property {string} [sizeLabel]
 * @property {string} [ariaLabel]
 * @property {(row: any) => React.ReactNode} [renderActions]
 * @property {Record<string, string>} [sortLabels]
 * @property {string} [tableClass]
 * @property {(q: string) => void} [onSearchChange]
 * @property {boolean} [selectable] Opt-in bulk selection. When falsy NONE of the selection UI renders and the table behaves exactly as it did before selection existed.
 * @property {(row: any) => string | number} [selectionId] Selection-id accessor. Defaults to `rowKey`.
 * @property {"page" | "all"} [selectionMode] "all" INVERTS the meaning of `selectedIds`: it then holds the EXCLUDED ids, not the selected ones.
 * @property {Set<string | number>} [selectedIds]
 * @property {(row: any) => boolean} [rowSelectable] Rows this returns false for cannot be selected (e.g. your own account).
 * @property {(row: any) => string} [rowSelectLabel]
 * @property {(id: string | number) => void} [onToggleRow]
 * @property {() => void} [onTogglePage]
 * @property {(ctx: any) => React.ReactNode} [renderSelectionBar] Renders the contextual selection toolbar (normally BulkBar). Called only while at least one row is selected.
 */

/**
 * @typedef {object} DataTableHandle
 * @property {() => void} focusSearch
 * @property {(rowKey: string | number) => void} focusRowAction
 */

/** @type {React.ForwardRefExoticComponent<DataTableProps & React.RefAttributes<DataTableHandle>>} */
const DataTable = forwardRef(function DataTable({
  rows, columns, rowKey, config,
  searchPlaceholder = "Search", searchLabel, searchId,
  emptyNoData = "Nothing here yet.", emptyNoMatch = "No matches.",
  initialSort, pageSizes = [10, 25, 50, 100], defaultPageSize = 25,
  sizeLabel = "Rows per page", ariaLabel, renderActions, sortLabels = {},
  tableClass = "grid data",
  // Opt-in bulk row-selection (all optional; when `selectable` is falsy none
  // of this renders and the component is byte-for-byte the same as before —
  // see the header comment). selectionId defaults to rowKey.
  selectable, selectionId, selectionMode, selectedIds,
  rowSelectable, rowSelectLabel, onToggleRow, onTogglePage,
  renderSelectionBar, onSearchChange,
}, ref) {
  const firstSortable = columns.find((c) => c.sortable)?.key;
  const [q, setQ] = useState("");
  const [sortKey, setSortKey] = useState(initialSort?.key ?? firstSortable);
  const [sortDir, setSortDir] = useState(initialSort?.dir ?? "asc");
  const [perPage, setPerPage] = useState(defaultPageSize);
  const [page, setPage] = useState(1);
  const [liveLabel, setLiveLabel] = useState("");

  const searchRef = useRef(null);
  const prevRef = useRef(null);
  const nextRef = useRef(null);
  const rowActionRefs = useRef({}); // rowKey -> actions <td>

  const view = viewRows(rows, { query: q, sortKey, sortDir, page, perPage }, config);

  // Selection is entirely opt-in: guarded behind `selectable` (code review
  // #5 / L1) so a non-selectable table (e.g. Logs, up to 2000 rows) never
  // pays for the extra filterRows() pass over every row on every render —
  // only a DataTable that actually renders selection UI computes any of this.
  const selId = selectable ? (selectionId || rowKey) : null;
  const elig = (r) => (rowSelectable ? rowSelectable(r).ok : true);
  const pageEligibleRows = selectable ? view.slice.filter(elig) : [];
  const filteredEligibleRows = selectable ? filterRows(rows, q, config.fields).filter(elig) : [];
  const pageEligibleIds = selectable ? pageEligibleRows.map(selId) : [];
  // A row's effective checked state: in "all" (all-matching) mode,
  // `selectedIds` holds the EXCLUDED ids, so checked = NOT excluded; in
  // "explicit" mode selectedIds holds the selected ids directly. Ineligible
  // rows (e.g. the signed-in admin's own row) are never checked, regardless
  // of mode — an "all matching" selection never actually covers them.
  const isRowChecked = (r) => {
    if (!elig(r)) return false;
    const id = selId(r);
    return selectionMode === "all" ? !selectedIds?.has(id) : !!selectedIds?.has(id);
  };
  const checkedOnPage = selectable
    ? new Set(pageEligibleRows.filter(isRowChecked).map(selId)) : new Set();
  const headerState = selectable ? pageHeaderState(pageEligibleIds, checkedOnPage) : "none";

  useImperativeHandle(ref, () => ({
    focusSearch: () => searchRef.current?.focus?.(),
    // Focus the first enabled action button in a row that persisted through a
    // reload (e.g. after promote/demote, where only the row's icon swaps).
    focusRowAction: (key) =>
      rowActionRefs.current[key]?.querySelector("button:not(:disabled)")?.focus?.(),
  }), []);

  const hasActions = typeof renderActions === "function";
  // See the wrapper below: a scroll region needs a focusable child, or
  // it must become focusable itself.
  const needsRegion = !hasActions || columns.some((c) => !c.sortable);
  const colCount = columns.length + (hasActions ? 1 : 0) + (selectable ? 1 : 0);
  // Pad short pages up to a full page's height with structurally-identical
  // spacer rows (only when there's more than one page) so the pager below never
  // jumps as you move between pages. Transparent borders keep them invisible.
  const fillerRows = view.totalPages > 1 ? Math.max(0, perPage - view.slice.length) : 0;

  // Clicking a header toggles asc/desc on the active column, else switches to it
  // ascending; any sort change returns to page 1 (preserving search + page size).
  // aria-sort on a <th> only surfaces on re-navigation, so announce the new order.
  function sortBy(key) {
    const dir = key === sortKey ? (sortDir === "asc" ? "desc" : "asc") : "asc";
    setSortKey(key); setSortDir(dir); setPage(1);
    // Spoken name: an explicit sortLabels override, else the column's own visible
    // label (never the raw key, which would read out as e.g. "last underscore login").
    const name = sortLabels[key] || columns.find((c) => c.key === key)?.label || key;
    setLiveLabel(`Sorted by ${name}, ${dir === "asc" ? "ascending" : "descending"}.`);
  }

  // A page move that lands on an end DISABLES the button under focus (Prev on
  // page 1, Next on the last page), dropping focus to <body>; hand it to the
  // sibling instead.
  function goPage(next) {
    setPage(next);
    if (next <= 1) requestAnimationFrame(() => nextRef.current?.focus());
    else if (next >= view.totalPages) requestAnimationFrame(() => prevRef.current?.focus());
  }

  // Debounce the range read-out (skip the initial mount) so search-as-you-type
  // doesn't enqueue a status announcement on every keystroke (WCAG 4.1.3).
  const didAnnounce = useRef(false);
  useEffect(() => {
    if (!didAnnounce.current) { didAnnounce.current = true; return; }
    const id = setTimeout(() => setLiveLabel(view.label), 450);
    return () => clearTimeout(id);
  }, [view.label]);

  return (
    <>
      <div className="row usersearch">
        <div className="searchwrap">
          <input id={searchId} ref={searchRef} type="search" className="logsearch"
                 placeholder={searchPlaceholder} value={q}
                 aria-label={searchLabel || searchPlaceholder}
                 onChange={(e) => {
                   setQ(e.target.value); setPage(1); onSearchChange?.(e.target.value);
                 }}
                 onKeyDown={(e) => {
                   // Escape clears an active search in place (standard search-
                   // field affordance); focus stays in the box.
                   if (e.key === "Escape" && q) {
                     e.preventDefault();
                     setQ(""); setPage(1); onSearchChange?.("");
                   }
                 }} />
          {q && (
            <button type="button" className="search-clear" aria-label="Clear search"
                    onClick={() => {
                      setQ(""); setPage(1); searchRef.current?.focus(); onSearchChange?.("");
                    }}>
              <IconClose size={14} />
            </button>
          )}
        </div>
      </div>

      {selectable && typeof renderSelectionBar === "function"
        && renderSelectionBar({ pageEligibleRows, filteredEligibleRows, query: q })}

      {/* WCAG 1.4.10 Reflow. The fixed columns come to 478px before Email/Note
          get any width at all, `html, body { overflow: hidden }` means the page
          cannot scroll sideways, and the nearest scroller was the whole `.admin`
          column — so at 320px the only way to reach the Actions button was to
          scroll the entire page in two directions. This wrapper gives the table
          a scroll region of its own; `min-width` on `.grid.data` is what it
          scrolls.
          Whether it needs `tabIndex={0} role="region"` is DERIVED, not
          decided once in a comment. A scroll region is only keyboard-reachable
          if something inside it can take focus: with a sort button in every
          header and action buttons in every row, both horizontal extremes are
          already reachable and focusing one scrolls it into view, so a tab stop
          would be noise and a region sharing the table's own aria-label would
          announce the name twice.
          That held for all three current consumers -- and it held by
          COINCIDENCE, in a shared component, with nothing enforcing it. The
          proof is one file over: Usage.jsx copied this wrapper onto a table of
          plain <th>/<td> and shipped a scroll region no keyboard user could
          reach (WCAG 2.1.1). So the precondition is now a predicate. A table
          with no actions column, or with any non-sortable column, gets a named
          focusable region; one whose extremes are already reachable does not. */}
      <div className={"table-scroll" + (needsRegion ? " table-scroll-region" : "")}
           {...(needsRegion
             ? { tabIndex: 0, role: "region",
                 "aria-label": `${ariaLabel || "Table"}, scrollable` }
             : {})}>
      <table className={tableClass} aria-label={ariaLabel}>
        <colgroup>
          {selectable && <col className="col-select" />}
          {columns.map((c) => <col key={c.key} className={c.colClass} />)}
          {hasActions && <col className="col-actions" />}
        </colgroup>
        <thead>
          <tr>
            {selectable && (
              <th scope="col" className="col-select-head">
                <input
                  type="checkbox"
                  aria-label={`Select all ${config.nouns.many} on this page`}
                  checked={headerState === "all"}
                  disabled={pageEligibleRows.length === 0}
                  ref={(el) => { if (el) el.indeterminate = headerState === "some"; }}
                  onChange={(e) => onTogglePage?.(pageEligibleRows, e.target.checked)}
                />
              </th>
            )}
            {columns.map((c) => {
              if (!c.sortable) {
                return <th key={c.key} scope="col" className={c.thClass}>{c.label}</th>;
              }
              const active = sortKey === c.key;
              return (
                <th key={c.key} scope="col" className={c.thClass}
                    aria-sort={active ? (sortDir === "asc" ? "ascending" : "descending") : "none"}>
                  <button type="button" className={"sortbtn" + (active ? " active" : "")}
                          onClick={() => sortBy(c.key)}>
                    {c.label}
                    <span className="caret" aria-hidden="true">
                      {active ? (sortDir === "asc" ? "▲" : "▼") : ""}
                    </span>
                  </button>
                </th>
              );
            })}
            {hasActions && <th scope="col" className="actions-head">Actions</th>}
          </tr>
        </thead>
        <tbody>
          {view.slice.length === 0 ? (
            <tr>
              <td colSpan={colCount} className="empty">
                {q.trim() ? emptyNoMatch : emptyNoData}
              </td>
            </tr>
          ) : view.slice.map((r) => {
            const key = rowKey(r);
            const canSelect = selectable ? (rowSelectable ? rowSelectable(r) : { ok: true }) : null;
            return (
              <tr key={key}>
                {selectable && (
                  // A plain <td>, NOT <th scope="row"> (a11y H1, WCAG 1.3.1):
                  // a selection checkbox is not a row header — promoting it to
                  // one would falsely claim every other cell in the row is
                  // "headed by" the checkbox, which no data column actually is.
                  <td className="col-select-cell">
                    <input
                      type="checkbox"
                      checked={isRowChecked(r)}
                      disabled={!canSelect.ok}
                      // `title` mirrors the disabled reason for sighted
                      // non-AT users on hover (a11y L1) -- aria-label already
                      // carries it for assistive tech.
                      title={canSelect.ok ? undefined : canSelect.reason}
                      aria-label={canSelect.ok
                        ? (rowSelectLabel ? rowSelectLabel(r) : "Select row")
                        : canSelect.reason}
                      onChange={(e) => onToggleRow?.(r, e.target.checked)}
                    />
                  </td>
                )}
                {columns.map((c) => (
                  <td key={c.key} className={c.cellClass}
                      title={c.cellTitle ? c.cellTitle(r) : undefined}>
                    {c.render ? c.render(r) : r[c.key]}
                  </td>
                ))}
                {hasActions && (
                  <td className="actions"
                      ref={(el) => {
                        // Delete on unmount so the map doesn't accumulate dead keys.
                        if (el) rowActionRefs.current[key] = el;
                        else delete rowActionRefs.current[key];
                      }}>
                    {renderActions(r)}
                  </td>
                )}
              </tr>
            );
          })}
          {Array.from({ length: fillerRows }).map((_, i) => (
            <tr key={`filler-${i}`} className="filler" aria-hidden="true">
              <td colSpan={colCount} />
            </tr>
          ))}
        </tbody>
      </table>
      </div>

      <div className="pager">
        <span className="pager-range">{view.label}</span>
        {/* Table status (range + sort) announced politely — a bare aria-live div
            (NOT role="status") fed the DEBOUNCED label, so it stays distinct from
            the app-wide action-outcome toast and search-as-you-type can't spam it. */}
        <div className="sr-only" aria-live="polite">{liveLabel}</div>
        <label className="pager-size">
          <span className="sr-only">{sizeLabel}</span>
          <select value={perPage}
                  onChange={(e) => { setPerPage(Number(e.target.value)); setPage(1); }}>
            {pageSizes.map((n) => <option key={n} value={n}>{n} / page</option>)}
          </select>
        </label>
        <div className="pager-nav">
          <button type="button" className="pgbtn" aria-label="Previous page" ref={prevRef}
                  disabled={view.page <= 1} onClick={() => goPage(view.page - 1)}>
            ‹ Prev
          </button>
          <span className="pager-page">Page {view.page} of {view.totalPages}</span>
          <button type="button" className="pgbtn" aria-label="Next page" ref={nextRef}
                  disabled={view.page >= view.totalPages} onClick={() => goPage(view.page + 1)}>
            Next ›
          </button>
        </div>
      </div>
    </>
  );
});

export default DataTable;
