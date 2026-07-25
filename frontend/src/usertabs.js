// Pure logic for the Admin → Users tabbed interface (Current users / Pending
// requests / Blocked users). Kept out of Admin.jsx so it can be unit-tested
// under vitest (the fast tier); the focus/navigate/DOM side effects that consume
// these values stay in the component, where Playwright covers them.

// Ordered sub-tabs of the Users section. `key` is BOTH the URL path segment
// (/admin/users/<key>) and the internal tab identity; `label` is the visible +
// accessible tab name. Order here is the tab order (and Left/Right arrow order).
export const USER_SUBTABS = [
  { key: "current", label: "Current users" },
  { key: "pending", label: "Pending requests" },
  { key: "blocked", label: "Blocked users" },
];

const KEYS = USER_SUBTABS.map((t) => t.key);

// The default tab: opening /admin/users (or any invalid sub) lands here.
export const DEFAULT_SUBTAB = "current";

// Resolve a raw :sub route param to a valid sub-tab key. Anything absent or
// unrecognized falls back to DEFAULT_SUBTAB, so a stale bookmark to
// /admin/users/bogus opens Current users rather than a blank panel — the same
// forgiving contract AdminRoute applies to an unknown outer :tab.
export function resolveSubTab(sub) {
  return KEYS.includes(sub) ? sub : DEFAULT_SUBTAB;
}

// Next sub-tab key for a keyboard action on the tablist. Left/Right wrap around
// the ends (the common APG tabs behavior); Home/End jump to the first/last.
// Pure index math — the caller owns the focus() + navigate() side effects, so
// this stays vitest-testable without a browser. An unknown current key is
// treated as index 0 so a bad param can never strand keyboard nav.
export function subTabKeyForArrow(currentKey, action) {
  const i = KEYS.indexOf(currentKey);
  const cur = i === -1 ? 0 : i;
  switch (action) {
    case "left": return KEYS[(cur - 1 + KEYS.length) % KEYS.length];
    case "right": return KEYS[(cur + 1) % KEYS.length];
    case "home": return KEYS[0];
    case "end": return KEYS[KEYS.length - 1];
    default: return currentKey;
  }
}

// Attention tone for the Pending-requests count badge: "attention" (accent)
// ONLY while there's something awaiting review, otherwise "idle" (neutral,
// inactive-tab styling). Never an error tone — a pending queue is work waiting
// for an admin, not an application failure, so it must not read as red/broken.
export function pendingBadgeTone(count) {
  return count > 0 ? "attention" : "idle";
}

// --- session memory --------------------------------------------------------
// The active sub-tab is remembered per browser session, so returning via the
// bare /admin/users link reopens the tab you left — the spec's "...or was
// previously selected during the current administrative session". A :sub in the
// URL always wins over this.
//
// These live HERE, next to resolveSubTab, rather than in Admin.jsx: the shell
// writes the remembered value on navigation and the Allowlist panel writes it on
// a tab switch, so leaving them in Admin.jsx would force Allowlist.jsx to import
// back from its own parent — a module cycle for two lines of sessionStorage.
const USERS_SUBTAB_STORAGE_KEY = "admin.usersSubTab";

// Both wrap storage in try/catch: sessionStorage THROWS (not returns null) in a
// hardened/private-mode browser, and losing tab memory must never take the whole
// Admin section down with it.
export function rememberedSubTab() {
  try { return resolveSubTab(sessionStorage.getItem(USERS_SUBTAB_STORAGE_KEY)); }
  catch { return DEFAULT_SUBTAB; }
}

export function rememberSubTab(sub) {
  try { sessionStorage.setItem(USERS_SUBTAB_STORAGE_KEY, sub); } catch { /* storage disabled */ }
}
