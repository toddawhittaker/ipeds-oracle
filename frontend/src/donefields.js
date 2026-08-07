// The `done` SSE event is a flat dict mixing turn bookkeeping (type,
// message_id, ...) with the answer fields that belong on the message object
// (duration_ms, results_truncated, figure_grounding, ...). This module is the
// ONE merge point on the live path — Chat.jsx used to name each answer field
// by hand at the finalize merge, a FOURTH hand-enumerated site alongside
// backend/app/routers/chat.py's `done` dict, `_persist`'s turn_values, and
// get_conversation's SELECT — so a field added to the server's `done` event
// rendered correctly after a reload (Chat.jsx spreads `...m` there) but not
// on the turn that produced it, until someone remembered to add it here too.
//
// DONE_META_KEYS is a DENYLIST, not an allowlist, on purpose: an allowlist
// would need editing every time the server grows an answer field, reproducing
// the exact hand-enumeration bug this module exists to end. Each key here is
// bookkeeping, never answer content — matching the server's own derivation
// of DONE_EVENT_FIELDS (backend/app/routers/chat.py), which is why nothing
// else needs listing on this side either:
//   type                        — the SSE event discriminator itself.
//   message_id / user_message_id — row identity, written separately.
//   title                        — a side effect (renames the conversation),
//                                   not a message field.
//   model / escalated / tokens   — billing/routing telemetry, never rendered
//                                   in a chat bubble.
//   cached / refused / no_data   — path markers describing HOW the turn was
//                                   handled, not what the answer contains.
export const DONE_META_KEYS = new Set([
  "type", "message_id", "user_message_id", "title",
  "model", "escalated", "tokens", "cached", "refused", "no_data",
]);

/**
 * Every field on a `done` event that belongs on the message object: every own
 * key NOT in DONE_META_KEYS, skipping null/undefined so an absent field
 * behaves exactly like one that was never graded (tabletruth.js and
 * datetime.js both guard with `== null`) — while keeping a legitimate falsy
 * value (`false`, `0`) intact, since `table_cells_matched: 0` is the caution
 * for an answer where EVERY value failed to reproduce.
 *
 * Never throws — this runs inside a live SSE handler, so a malformed/null/
 * undefined event must degrade to "no fields", not break the turn.
 * @param {Record<string, unknown> | null | undefined} ev the parsed `done` event
 * @returns {Record<string, unknown>} answer fields only, ready to merge onto a message
 */
export function messageFieldsFromDone(ev) {
  const out = {};
  if (!ev || typeof ev !== "object") return out;
  for (const key of Object.keys(ev)) {
    if (DONE_META_KEYS.has(key)) continue;
    const value = ev[key];
    if (value == null) continue;
    out[key] = value;
  }
  return out;
}
