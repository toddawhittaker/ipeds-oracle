export declare const DONE_META_KEYS: Set<string>;
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
export declare function messageFieldsFromDone(ev: Record<string, unknown> | null | undefined): Record<string, unknown>;
