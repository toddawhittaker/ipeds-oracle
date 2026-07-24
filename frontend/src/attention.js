// Pure logic for the Admin "attention" badges — the top-bar Admin button's total
// and the per-section nav counts (Users / Skills / Logs). Kept out of the
// components so it can be unit-tested under vitest (the fast tier); the fetch/
// poll/DOM side effects that consume these values stay in App.jsx / Admin.jsx,
// where Playwright covers them. The tone reuses usertabs.js's pendingBadgeTone so
// there's ONE definition of "accent when work is waiting, neutral otherwise".
export { pendingBadgeTone as badgeTone } from "./usertabs.js";

// The areas with an actionable backlog, in nav order. Matches the backend
// /api/admin/attention response keys AND the ADMIN_TABS names, so a section's
// count is just `counts[tab]`. imports/usage are absent (no backlog) → no badge.
export const ATTENTION_KEYS = ["users", "skills", "logs"];

const num = (v) => (Number.isFinite(v) ? v : 0);

// Sum of everything awaiting an admin — drives the single top-bar Admin badge.
// Missing/NaN counts coerce to 0 so a partial/failed fetch never NaNs the total.
export function attentionTotal(counts) {
  const c = counts || {};
  return ATTENTION_KEYS.reduce((sum, k) => sum + num(c[k]), 0);
}

// The top-bar avatar badge total: the section backlog PLUS one for an available
// update (admins see the update in the same "something's waiting" cue). Kept a
// pure add-on so the section-only `attentionTotal` (which must match the backend
// keys) stays untouched.
export function avatarBadgeTotal(counts, hasUpdate) {
  return attentionTotal(counts) + (hasUpdate ? 1 : 0);
}

// Badge text for a count: "" (no badge) at 0/negative, the plain number up to
// `cap`, else a capped "99+" form so a big backlog stays one short token.
export function formatBadge(n, cap = 99) {
  const v = num(n);
  if (v <= 0) return "";
  return v <= cap ? String(v) : `${cap}+`;
}
