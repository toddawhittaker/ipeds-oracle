// Pure text builders for the delete-conversation confirmation. Chat.jsx pushes
// these strings to the app-wide TOAST (useToast); the toast host owns the
// visible render, the live-region announcement, and the (focus-independent)
// lifecycle.
//
// Only the WORDING lives here — the singular/plural, the "no chats remaining"
// empty branch, and the "started a new chat" open-conversation branch. The
// browser behaviour around it (the confirmation modal, focus management,
// navigation) stays in Chat.jsx and is covered by
// frontend/e2e/delete-focus.spec.js. The exact strings are pinned by
// frontend/src/announce.test.js (vitest).
//
// The remaining-count in the wording is informative UX. It USED to be strictly
// load-bearing (a single shared aria-live region only re-announces on a text
// change, so two same-titled deletes needed different strings); the toast host
// now gives each push its own live-region child, so re-announcement is
// structural. The count still keeps consecutive messages distinct + useful.

// Shown when the DELETE request itself fails — a constant, not a builder.
export const DELETE_FAILED = "Couldn't delete that chat.";

// Build the announcement for a successful delete.
//   open      — the deleted conversation was the one currently open
//   remaining — how many conversations remain after the delete (>= 0)
// `title` is passed already-resolved (Chat.jsx defaults a blank title to
// "Untitled" before opening the confirm modal, so it's non-empty here).
export function deleteAnnouncement({ title, open, remaining }) {
  if (open) return `Deleted "${title}". Started a new chat.`;
  if (remaining === 0) return `Deleted "${title}". No chats remaining.`;
  const noun = remaining === 1 ? "chat" : "chats";
  return `Deleted "${title}". ${remaining} ${noun} remaining.`;
}
