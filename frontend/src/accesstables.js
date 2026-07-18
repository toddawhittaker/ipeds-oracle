// datatable.js configs for the two access-request admin tables (Pending
// requests, Blocked users). Kept in their OWN module — like userlist.js's
// USER_CONFIG — so the pure comparator/search logic sits in the vitest coverage
// floor (see frontend/vitest.config.js) rather than escaping the gate inside the
// Playwright-only Admin.jsx. Pinned by accesstables.test.js.

// Case-insensitive locale compare; null/undefined sort as "".
export const strCmp = (a, b) => (a || "").localeCompare(b || "", undefined, { sensitivity: "base" });
// Numeric compare treating null/undefined/absent as 0 (so a legacy NULL
// denied_at sorts as the epoch rather than producing NaN).
export const numCmp = (a, b) => (a || 0) - (b || 0);

// Pending access requests: rows { id, email, created_at, ... } — raw addresses
// (Approve is exact). Default sort Requested (created_at) newest-first; the first
// comparator (email) is the unknown-key fallback.
export const PENDING_CONFIG = {
  fields: ["email"],
  comparators: {
    email: (a, b) => strCmp(a.email, b.email),
    requested: (a, b) => numCmp(a.created_at, b.created_at),
  },
  tiebreak: (r) => r.id,
  nouns: { one: "request", many: "requests" },
};

// Blocked users: rows { id, canon_email, emails[], created_at, denied_at } —
// grouped CANONICALLY, so Email keys on canon_email and search also spans the
// original `emails`. Default sort Denied (denied_at) newest-first.
export const BLOCKED_CONFIG = {
  fields: ["canon_email", (r) => r.emails.join(" ")],
  comparators: {
    email: (a, b) => strCmp(a.canon_email, b.canon_email),
    requested: (a, b) => numCmp(a.created_at, b.created_at),
    denied: (a, b) => numCmp(a.denied_at, b.denied_at),
  },
  tiebreak: (r) => r.id,
  nouns: { one: "blocked user", many: "blocked users" },
};
