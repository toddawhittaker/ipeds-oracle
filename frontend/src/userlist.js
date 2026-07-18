// Pure filter -> sort -> paginate pipeline for the admin Users list table.
//
// Only the DATA LOGIC lives here; the browser behaviour around it (the search
// input, the click-to-sort headers, the page-size <select>, the remove-user
// confirmation modal, aria-live announcements, focus) stays in Admin.jsx and is
// covered by frontend/e2e/. The exact input->output behaviour below is pinned by
// frontend/src/userlist.test.js (vitest) — no browser needed.
//
// A user row is { email, note, is_admin, last_login } where last_login is unix
// seconds or null (never signed in). The list arrives unpaginated from
// GET /api/admin/allowlist, so filter/sort/paginate are all client-side.

// Case-insensitive substring match over email AND note, ignoring surrounding
// whitespace on the query. A blank/whitespace-only query returns every row.
export function filterUsers(rows, query) {
  const q = (query || "").trim().toLowerCase();
  if (!q) return rows.slice();
  return rows.filter((r) => {
    const email = (r.email || "").toLowerCase();
    const note = (r.note || "").toLowerCase();
    return email.includes(q) || note.includes(q);
  });
}

// last_login is nullable; a null (never-logged-in) row sorts as -Infinity so
// that DESC (most-recent-first) puts them at the END and ASC (oldest-first) puts
// them at the start — a consistent grouping either way.
function loginValue(r) {
  return r.last_login == null ? -Infinity : r.last_login;
}

// Comparators keyed by sort column. email/note are case-insensitive locale
// order; admin groups by the boolean; last_login is numeric with nulls handled
// above. Each returns the ASC ordering; sortUsers negates for DESC.
const COMPARATORS = {
  email: (a, b) => (a.email || "").localeCompare(b.email || "", undefined, { sensitivity: "base" }),
  note: (a, b) => (a.note || "").localeCompare(b.note || "", undefined, { sensitivity: "base" }),
  admin: (a, b) => (a.is_admin ? 1 : 0) - (b.is_admin ? 1 : 0),
  last_login: (a, b) => loginValue(a) - loginValue(b),
};

// Sort by sortKey/sortDir with a DETERMINISTIC tiebreak on the unique email.
// Unknown keys fall back to email. Tiebreaking on email (not the incoming array
// index) matters: many rows can tie on note/admin/last_login — e.g. a batch of
// CSV-imported users all share one added_at and one "Imported on …" note — and if
// the tiebreak deferred to fetch order, an unstable backend ordering would let a
// refetch (after a delete) reshuffle which rows land on the current page, so a row
// you didn't touch could vanish off the page. email is the primary key, so this
// makes the order total and stable. The tiebreak is always email ASC (not flipped
// by dir) so the secondary order stays consistent.
export function sortUsers(rows, sortKey, sortDir) {
  const cmp = COMPARATORS[sortKey] || COMPARATORS.email;
  const dir = sortDir === "desc" ? -1 : 1;
  return rows.slice().sort((a, b) => {
    const c = cmp(a, b) * dir;
    if (c !== 0) return c;
    return (a.email || "").localeCompare(b.email || "", undefined, { sensitivity: "base" });
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
// "Showing 26–50 of 143 users" (en dash), "Showing 1 of 1 user", "No users".
export function rangeLabel({ start, end, total }) {
  if (total === 0) return "No users";
  const noun = total === 1 ? "user" : "users";
  const span = start === end ? `${start}` : `${start}–${end}`;
  return `Showing ${span} of ${total} ${noun}`;
}

// Compose the mandated order — filter, THEN sort, THEN paginate — and return the
// visible page plus the metadata the component needs (clamped page, totalPages,
// range positions, filtered total). One call so the component stores no derived
// state.
export function viewUsers(rows, { query, sortKey, sortDir, page, perPage }) {
  const filtered = filterUsers(rows, query);
  const sorted = sortUsers(filtered, sortKey, sortDir);
  const paged = paginate(sorted, page, perPage);
  return { ...paged, label: rangeLabel(paged) };
}
