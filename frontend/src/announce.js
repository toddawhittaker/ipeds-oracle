// Pure text builders for the delete-conversation aria-live announcer.
//
// Only the WORDING lives here — the singular/plural, the "no chats remaining"
// empty branch, and the "started a new chat" open-conversation branch. The
// browser behaviour around it (window.confirm, focus management, navigation,
// and the fact that a live region only re-announces on a text MUTATION) stays
// in Chat.jsx and is covered by web/e2e/delete-focus.spec.js. The exact strings
// are pinned by web/src/announce.test.js (vitest) — no browser needed.
//
// The remaining-count in the wording is LOAD-BEARING, not chatty: two deletes of
// identically-titled ("Untitled") conversations must yield DIFFERENT strings, or
// the aria-live region would not re-announce the second one. The count strictly
// decreases, so consecutive announcements always differ.

// Shown when the DELETE request itself fails — a constant, not a builder.
export const DELETE_FAILED = "Couldn't delete that chat.";

// Build the announcement for a successful delete.
//   open      — the deleted conversation was the one currently open
//   remaining — how many conversations remain after the delete (>= 0)
// `title` is passed already-resolved (Chat.jsx defaults a blank title to
// "Untitled" before the confirm dialog, so it's non-empty here).
export function deleteAnnouncement({ title, open, remaining }) {
  if (open) return `Deleted "${title}". Started a new chat.`;
  if (remaining === 0) return `Deleted "${title}". No chats remaining.`;
  const noun = remaining === 1 ? "chat" : "chats";
  return `Deleted "${title}". ${remaining} ${noun} remaining.`;
}
