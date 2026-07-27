// The admin Users list = the shared datatable.js pipeline with a user-specific
// config. Only the DATA LOGIC lives here (via datatable.js); the browser
// behaviour around it (search input, click-to-sort headers, page-size <select>,
// the remove-user confirmation modal, aria-live announcements, focus) stays in
// the reusable <DataTable> component (DataTable.jsx) and is covered by
// frontend/e2e/. The exact input->output behaviour below is pinned by
// frontend/src/userlist.test.js (vitest).
//
// A user row is { email, note, is_admin, last_login, last_active } where both
// stamps are unix seconds or null. last_active is the one the table SHOWS: the
// latest of the sign-in stamp, the user's most recent conversation, and their
// most recent question (derived server-side in admin.list_allowlist), so it is
// null only for someone who was allowlisted and never came. The list arrives
// unpaginated from GET /api/admin/allowlist, so filter/sort/paginate are all
// client-side.
import {
  filterRows,
  paginate,
  rangeLabel as rangeLabelFor,
  sortRows,
  viewRows,
} from "./datatable.js";

// last_active is nullable; a null (never-active) row sorts as -Infinity so that
// DESC (most-recent-first) puts them at the END and ASC (oldest-first) puts them
// at the start — a consistent grouping either way.
//
// It reads last_active, NOT last_login: those differ for anyone whose most
// recent activity was a question rather than a sign-in, and sorting on the
// wrong one would disagree with the date the SAME cell renders.
function activeValue(r) {
  return r.last_active == null ? -Infinity : r.last_active;
}

// Comparators keyed by sort column. email/note are case-insensitive locale
// order; admin groups by the boolean; last_active is numeric with nulls handled
// above. Each returns the ASC ordering; sortRows negates for DESC.
const COMPARATORS = {
  email: (a, b) => (a.email || "").localeCompare(b.email || "", undefined, { sensitivity: "base" }),
  note: (a, b) => (a.note || "").localeCompare(b.note || "", undefined, { sensitivity: "base" }),
  admin: (a, b) => (a.is_admin ? 1 : 0) - (b.is_admin ? 1 : 0),
  last_active: (a, b) => activeValue(a) - activeValue(b),
};

// The Users table's datatable config: search email+note, the comparators above,
// a stable tiebreak on the unique email (the primary key), user nouns.
export const USER_CONFIG = {
  fields: ["email", "note"],
  comparators: COMPARATORS,
  tiebreak: (r) => r.email || "",
  nouns: { one: "user", many: "users" },
};

// Thin, named wrappers so callers (and userlist.test.js) keep the user-domain
// vocabulary while the logic lives once in datatable.js.
export function filterUsers(rows, query) {
  return filterRows(rows, query, USER_CONFIG.fields);
}

export function sortUsers(rows, sortKey, sortDir) {
  return sortRows(rows, sortKey, sortDir, USER_CONFIG);
}

export function rangeLabel(pos) {
  return rangeLabelFor(pos, USER_CONFIG.nouns);
}

export function viewUsers(rows, state) {
  return viewRows(rows, state, USER_CONFIG);
}

export { paginate };
