import React, { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { viewRows } from "./datatable.js";
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
const DataTable = forwardRef(function DataTable({
  rows, columns, rowKey, config,
  searchPlaceholder = "Search", searchLabel, searchId,
  emptyNoData = "Nothing here yet.", emptyNoMatch = "No matches.",
  initialSort, pageSizes = [10, 25, 50, 100], defaultPageSize = 25,
  sizeLabel = "Rows per page", ariaLabel, renderActions, sortLabels = {},
  tableClass = "grid data",
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

  useImperativeHandle(ref, () => ({
    focusSearch: () => searchRef.current?.focus?.(),
    // Focus the first enabled action button in a row that persisted through a
    // reload (e.g. after promote/demote, where only the row's icon swaps).
    focusRowAction: (key) =>
      rowActionRefs.current[key]?.querySelector("button:not(:disabled)")?.focus?.(),
  }), []);

  const hasActions = typeof renderActions === "function";
  const colCount = columns.length + (hasActions ? 1 : 0);
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
                 onChange={(e) => { setQ(e.target.value); setPage(1); }} />
          {q && (
            <button type="button" className="search-clear" aria-label="Clear search"
                    onClick={() => { setQ(""); setPage(1); searchRef.current?.focus(); }}>
              <IconClose size={14} />
            </button>
          )}
        </div>
      </div>

      <table className={tableClass} aria-label={ariaLabel}>
        <colgroup>
          {columns.map((c) => <col key={c.key} className={c.colClass} />)}
          {hasActions && <col className="col-actions" />}
        </colgroup>
        <thead>
          <tr>
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
            return (
              <tr key={key}>
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
