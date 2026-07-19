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
