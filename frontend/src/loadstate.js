// The pure decision behind every admin panel's load-failure notice —
// [[loadstate]] in CLAUDE.md. Two admin screens (Logs, Allowlist) fetch on a
// timer/poll on top of an initial load, and the two failure shapes need
// OPPOSITE treatment:
//
//   - a FIRST load that fails has no rows to show, so the empty table IS the
//     failure -- and an empty table reads as fact ("nobody is blocked",
//     "no log records"), which is the dangerous lie. The error must REPLACE
//     the panel.
//   - a REFRESH that fails on an already-populated screen must NOT blank the
//     table -- that would also blow away the admin's search text, sort,
//     page, and any lifted row selection over a transient 4s poll blip. The
//     rows stay up; a notice just says they may be stale.
//
// loadNotice() is that single decision, shared by both components so the
// rule can't drift between them. `error` is the ALREADY-HUMAN message the
// caller produced (typically via authcopy.js's loadErrorMessage) -- this
// module owns only the replace-vs-stale shape, not the wording of the
// underlying failure.
export function loadNotice({ error, hasRows }) {
  if (!error) return null;
  if (!hasRows) {
    // Nothing on screen to protect -- the error IS the content.
    return { replace: true, text: error };
  }
  // Rows are still up; say so, and carry the server's own sentence rather
  // than a generic "something went wrong" (an admin chasing a locked
  // logs.db needs that detail, not an apology).
  return { replace: false, text: `These rows may be stale — the last refresh failed: ${error}` };
}
