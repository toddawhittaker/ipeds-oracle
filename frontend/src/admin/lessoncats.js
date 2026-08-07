// Pure predicates for Skills.jsx's category features (A2: lesson-rejection
// memory). Mirrors the admin/format.js precedent: these were destined to live
// inside a browser-tested component file, so extracting them into their own
// leaf module is what makes real input->output coverage possible at all.

/**
 * Whether a "Reject & mute <label>" action should be offered for this skill.
 * True only when ALL of these hold: the skill carries a category, that token
 * is one the server's live category list (GET /api/admin/skills/categories)
 * recognizes, it's a LEARNABLE category (never UNGROUNDED_NUMBER/OTHER —
 * muting them would control nothing), and it isn't already muted (offering
 * the action again would be a no-op dressed up as one).
 * @param {{category?: string|null}} skill
 * @param {Array<{token: string, learnable: boolean, muted: boolean}>} [categories]
 */
export function canMuteCategory(skill, categories) {
  const token = skill && skill.category;
  if (!token || !Array.isArray(categories)) return false;
  const entry = categories.find((c) => c.token === token);
  if (!entry) return false;
  return Boolean(entry.learnable) && !entry.muted;
}

/**
 * The server-provided human label for a category token, or "" when there's
 * nothing to show — a NULL/missing token (pre-existing/seed/feedback rows,
 * per migration 35), an unrecognized token, or a categories list that hasn't
 * loaded yet. Never renders the literal word "undefined" or "null".
 * @param {string|null|undefined} token
 * @param {Array<{token: string, label: string}>} [categories]
 */
export function categoryLabel(token, categories) {
  if (!token || !Array.isArray(categories)) return "";
  const entry = categories.find((c) => c.token === token);
  return entry ? entry.label : "";
}

/**
 * The "Rejected (N)" section heading. A load failure must NEVER read as a
 * confirmed zero (the deniedError precedent, generalized): an admin seeing
 * "Rejected (0)" would believe nothing was ever rejected, when the truth is
 * the list couldn't be fetched at all.
 * @param {Array<unknown>} rows
 * @param {string|null|undefined} error
 */
export function rejectionCountLabel(rows, error) {
  if (error) return "Rejected — couldn't load";
  const n = Array.isArray(rows) ? rows.length : 0;
  return `Rejected (${n})`;
}
