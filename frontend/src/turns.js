// Pure logic for the destructive side of editing or re-running an earlier prompt.
//
// Chat's `messages` is a strictly alternating user/assistant array — `submit()`
// appends exactly one of each, the server's conversation load returns the same
// shape, and the e2e mocks build it that way. So a "turn" is the pair
// [i, i+1]: the user prompt at i and its answer at i+1.
//
// Editing or re-running the prompt at index i drops that turn AND every turn
// after it, client-side (`slice(0, i)`) and server-side (`_persist`'s
// `DELETE FROM messages WHERE conversation_id=? AND id>=?`) — permanently, with
// no tombstone and no undo. Re-asking the LAST turn is the ordinary refine
// gesture and loses nothing the user still wants; re-asking an earlier one
// silently destroys the analysis below it. These helpers draw that line so the
// component can confirm only in the second case.

// How many complete exchanges after this one would be destroyed. The turn being
// edited is not counted — the user is deliberately replacing that one.
export function laterTurnsLost(messages, index) {
  const list = Array.isArray(messages) ? messages : [];
  if (!Number.isInteger(index) || index < 0 || index >= list.length) return 0;
  // Everything from `index` onward goes; the first pair is the turn being
  // replaced, so the remainder is what's collateral.
  return Math.max(0, Math.ceil((list.length - index - 2) / 2));
}

// Whether this edit/rerun needs a confirmation. False for the last turn (the
// common, safe refine) and for anything out of range.
export function needsDestructiveConfirm(messages, index) {
  return laterTurnsLost(messages, index) > 0;
}

// The confirm dialog's body. Names the count, because "this can't be undone" is
// only actionable if the user knows how much "this" is.
export function editConfirmBody(lost) {
  const n = lost === 1 ? "1 later question and its answer" : `${lost} later questions and their answers`;
  return `Re-asking this question will permanently remove ${n} below it, including their tables, charts and SQL. This can't be undone.`;
}

// Confirm-button label. Carries the count so the destructive scope is visible
// on the button itself, not just in the prose above it.
export function editConfirmLabel(lost, verb = "Edit") {
  return `${verb} and remove ${lost} later ${lost === 1 ? "exchange" : "exchanges"}`;
}
