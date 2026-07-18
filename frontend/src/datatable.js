// Config-driven pure pipeline shared by every admin data table (Users, Pending
// requests, Blocked users). This is the DATA LOGIC only — filter -> sort ->
// paginate -> range label; the browser behaviour around it (search input,
// click-to-sort headers, page-size <select>, aria-live, focus) lives in the
// <DataTable> component (DataTable.jsx) and is covered by Playwright. The exact
// input->output behaviour here is pinned by datatable.test.js (vitest).
//
// A "config" is `{ fields, comparators, tiebreak, nouns }`:
//   fields      — accessors to search over (see filterRows)
//   comparators — { sortKey: ascComparator } (see sortRows)
//   tiebreak    — row => uniqueStableKey (see sortRows)
//   nouns       — { one, many } for the range label (see rangeLabel)
// userlist.js builds one for the Users table; the two access-request tables
// build their own. This generalizes what userlist.js used to hardcode.

// Resolve a search field accessor to a string: a field is either a string key
// (row[key]) or a function (row => string, e.g. joining an array column).
function fieldValue(row, field) {
  const v = typeof field === "function" ? field(row) : row[field];
  return v == null ? "" : String(v);
}

// Case-insensitive substring match over ANY of `fields`, ignoring surrounding
// whitespace on the query. A blank/whitespace-only query returns every row.
export function filterRows(rows, query, fields) {
  const q = (query || "").trim().toLowerCase();
  if (!q) return rows.slice();
  return rows.filter((r) => fields.some((f) => fieldValue(r, f).toLowerCase().includes(q)));
}

// Sort by sortKey/sortDir using `comparators` (each returns the ASC ordering;
// sortRows negates for DESC), with a DETERMINISTIC tiebreak on `tiebreak(row)` —
// a UNIQUE stable key applied always-ASC (never flipped by dir). The tiebreak
// matters: many rows can tie on the primary column (a batch of CSV-imported
// users share one added_at/note; several access requests share a denial time),
// and without a total order an unstable backend/refetch order would let a
// reload reshuffle which rows land on the current page — so a row you didn't
// touch could vanish off the page after a delete. Unknown sortKey falls back to
// the first comparator. Non-mutating (`.slice()`).
export function sortRows(rows, sortKey, sortDir, { comparators, tiebreak }) {
  // Unknown sortKey falls back to the FIRST comparator in the config's insertion
  // order — so define each config's default-sort column first in `comparators`.
  const keys = Object.keys(comparators);
  const cmp = comparators[sortKey] || comparators[keys[0]];
  const dir = sortDir === "desc" ? -1 : 1;
  return rows.slice().sort((a, b) => {
    const c = cmp(a, b) * dir;
    if (c !== 0) return c;
    // localeCompare(base) on the string form: identical to the old email
    // tiebreak for string keys, and a deterministic total order for numeric ids.
    return String(tiebreak(a)).localeCompare(String(tiebreak(b)), undefined, { sensitivity: "base" });
  });
}

// Slice `rows` to one page. `page` is clamped to [1, totalPages]; this clamp is
// exactly what keeps the caller on a valid page after a removal empties the last
// page (recompute with the same `page` and it self-corrects to the previous one).
// start/end are 1-based inclusive positions for the "Showing 26–50 of 143" label
// (both 0 when there are no rows).
export function paginate(rows, page, perPage) {
  const total = rows.length;
  const size = Math.max(1, perPage);
  const totalPages = Math.max(1, Math.ceil(total / size));
  const clamped = Math.min(Math.max(1, page || 1), totalPages);
  const from = (clamped - 1) * size;
  const slice = rows.slice(from, from + size);
  const start = total === 0 ? 0 : from + 1;
  const end = from + slice.length;
  return { slice, page: clamped, totalPages, start, end, total };
}

// Human-readable range for the aria-live status line, e.g.
// "Showing 26–50 of 143 users" (en dash), "Showing 1 of 1 request", "No users".
// `nouns` is { one, many } so each table reads naturally.
export function rangeLabel({ start, end, total }, { one, many }) {
  if (total === 0) return `No ${many}`;
  const noun = total === 1 ? one : many;
  const span = start === end ? `${start}` : `${start}–${end}`;
  return `Showing ${span} of ${total} ${noun}`;
}

// Compose the mandated order — filter, THEN sort, THEN paginate — and return the
// visible page plus the metadata the component needs (clamped page, totalPages,
// range positions, filtered total, label). One call so the component stores no
// derived state.
export function viewRows(rows, { query, sortKey, sortDir, page, perPage }, config) {
  const filtered = filterRows(rows, query, config.fields);
  const sorted = sortRows(filtered, sortKey, sortDir, config);
  const paged = paginate(sorted, page, perPage);
  return { ...paged, label: rangeLabel(paged, config.nouns) };
}
