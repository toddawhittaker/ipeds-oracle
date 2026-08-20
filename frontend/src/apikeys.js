// Pure display + table logic for MCP API keys, shared by a user's own /keys page
// (Keys.jsx) and the admin Keys tab (admin/Keys.jsx). Only the DATA LOGIC lives
// here; the browser behaviour around it — the one-shot reveal dialog, the revoke
// confirmation, focus — stays in those components and is covered by Playwright.
// The exact input->output behaviour below is pinned by apikeys.test.js (vitest).
//
// A key row is { id, last4, label, created_at, created_by, last_used_at,
// revoked_at } (plus `email` on the admin list). It NEVER carries the secret or
// its hash — app/apikeys.py stores only a SHA-256 of the key and returns the raw
// value exactly once, at mint time.
import { sortRows } from "./datatable.js";

// The prefix app/apikeys.py puts on every raw key. Repeated here rather than
// fetched: it is the literal a user reads back off a config file to recognise
// what the value is, and it changes only in a release that also rewrites this UI.
export const KEY_PREFIX = "ipeds_mcp_";

// What a row shows in place of the key. The last four characters are all the
// server ever hands back, and showing more than that is the one presentation bug
// on this screen that would be a real leak — hence a function with a test rather
// than a template literal inlined at two call sites.
export function maskedKey(row) {
  return `${KEY_PREFIX}…${row?.last4 || ""}`;
}

export function isRevoked(row) {
  return row?.revoked_at != null;
}

// last_used_at is null for a key that has never been presented, which is the
// common case for a key minted five minutes ago — and the case for EVERY key on
// a fresh deployment.
//
// 0, not -Infinity. The mapping used to be -Infinity, which grouped nulls at one
// end as intended for a MIXED list and broke the all-null one completely:
// `-Infinity - -Infinity` is NaN, `sortRows` returns the comparator's value the
// moment it is `!== 0`, and NaN is — so the id tiebreak below it never ran.
// Ascending and descending produced the same order, that order tracked whatever
// order the rows arrived in, and two loads could disagree. 0 keeps the same
// grouping (an unused key still sorts before every used one, because every real
// timestamp is a positive epoch) and `0 - 0` falls through to the tiebreak,
// which is what makes the sort stable and its two directions different.
function usedValue(r) {
  return r.last_used_at == null ? 0 : r.last_used_at;
}

// Comparators keyed by sort column, each returning the ASC ordering (sortRows
// negates for DESC). created_at is FIRST because sortRows falls back to the
// first comparator on an unknown sort key, and newest-first is this table's
// natural default.
const COMPARATORS = {
  created_at: (a, b) => (a.created_at || 0) - (b.created_at || 0),
  email: (a, b) => (a.email || "").localeCompare(b.email || "", undefined, { sensitivity: "base" }),
  label: (a, b) => (a.label || "").localeCompare(b.label || "", undefined, { sensitivity: "base" }),
  last_used_at: (a, b) => usedValue(a) - usedValue(b),
  // Active before revoked in ASC, so DESC surfaces the withdrawn ones — which is
  // what an admin auditing "what did we turn off" is looking for.
  status: (a, b) => (isRevoked(a) ? 1 : 0) - (isRevoked(b) ? 1 : 0),
};

// The keys table's datatable config. Searching includes last4 so an admin
// holding a key fragment from a log or a config file can find the row it belongs
// to; `email` is absent from a user's own list and simply never matches there.
// The tiebreak is the row id — the primary key, and unique across both tables.
export const KEY_CONFIG = {
  fields: ["email", "label", "last4"],
  comparators: COMPARATORS,
  tiebreak: (r) => r.id ?? 0,
  nouns: { one: "key", many: "keys" },
};

// The user's own page renders a plain list, not a <DataTable>, so it needs the
// sort on its own. Newest first, which is where a key you just minted appears.
export function sortByNewest(rows) {
  return sortRows(rows, "created_at", "desc", KEY_CONFIG);
}
