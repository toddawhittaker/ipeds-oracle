import { describe, it, expect } from "vitest";
import { DONE_META_KEYS, messageFieldsFromDone } from "./donefields.js";

// The `done` SSE event is a flat dict carrying BOTH turn bookkeeping (type,
// message_id, model, tokens, ...) and the answer fields that must land on the
// message object (duration_ms, results_truncated, figure_grounding, ...).
// Chat.jsx used to name each answer field by hand on the finalize merge — a
// FOURTH hand-enumerated site (chat.py's `done` dict, `_persist`'s
// turn_values, get_conversation's SELECT, and this) — so a field added to the
// server's `done` event rendered on reload (Chat.jsx spreads `...m` there)
// but not live, until someone remembered to add it here too. This module is
// the one merge point: a DENYLIST of known bookkeeping keys, so anything else
// on the event passes straight through.

describe("DONE_META_KEYS", () => {
  it("names every non-answer key the done event carries", () => {
    // Not exhaustive by construction (that's the point of the denylist design
    // below) but this pins the known bookkeeping keys so a future accidental
    // deletion from the constant is visible here, not just in behavior.
    for (const key of ["type", "message_id", "user_message_id", "title",
                        "model", "escalated", "tokens", "cached", "refused",
                        "no_data"]) {
      expect(DONE_META_KEYS.has(key)).toBe(true);
    }
  });
});

describe("messageFieldsFromDone", () => {
  it("excludes every meta key — none of them may land on a message object", () => {
    // THE regression this guards: a turn's billing/bookkeeping fields
    // (escalated, tokens, model, ...) are not answer content. If a future
    // refactor swept them into the message merge, a chat bubble would start
    // rendering raw token counts / internal escalation flags.
    const done = {
      type: "done", message_id: 1, user_message_id: 2, title: "A title",
      model: "gpt-x", escalated: true, tokens: 42, cached: false,
      refused: false, no_data: false,
      duration_ms: 1234, results_truncated: true,
    };
    const fields = messageFieldsFromDone(done);
    for (const meta of ["type", "message_id", "user_message_id", "title",
                        "model", "escalated", "tokens", "cached", "refused",
                        "no_data"]) {
      expect(fields).not.toHaveProperty(meta);
    }
    expect(fields).toEqual({ duration_ms: 1234, results_truncated: true });
  });

  it("skips null values, preserving the `!= null` semantics every consumer relies on", () => {
    // datetime.js's thoughtLabel and tabletruth.js's tableTrustNote both guard
    // with loose `== null`, which treats an ABSENT key the same as an
    // explicit null. If this function forwarded nulls as real keys with a
    // null value, that's still consumer-safe today — but the contract this
    // module exists to keep is "an absent field behaves exactly like a null
    // one", so a caller that switches to `"k" in obj` (a real, plausible
    // refactor here) must not regress.
    const done = {
      type: "done", message_id: 1,
      duration_ms: 500, figure_grounding: null, table_grounding: null,
      results_truncated: false,
    };
    const fields = messageFieldsFromDone(done);
    expect(fields).not.toHaveProperty("figure_grounding");
    expect(fields).not.toHaveProperty("table_grounding");
    // A legitimate falsy-but-present value (false, 0) must still come through.
    expect(fields.results_truncated).toBe(false);
    expect(fields.duration_ms).toBe(500);
  });

  it("passes an unknown/future field straight through — the whole point of a denylist", () => {
    // This is what a naive "tidy it into an allowlist" change would break:
    // an allowlist requires editing THIS file for every new server field,
    // reproducing the exact hand-enumeration bug the denylist exists to end.
    // A field neither named here nor in DONE_META_KEYS must still reach the
    // message object.
    const done = { type: "done", message_id: 1, a_brand_new_field: "surprise" };
    const fields = messageFieldsFromDone(done);
    expect(fields.a_brand_new_field).toBe("surprise");
  });

  it("keeps keys in snake_case — the shape the reload path already returns", () => {
    // Merged keys must match the server row shape verbatim (snake_case), or a
    // render site that works for a reloaded message (`m.results_truncated`)
    // would silently miss the live one (`m.resultsTruncated`).
    const fields = messageFieldsFromDone({
      type: "done", table_cells_checked: 4, table_cells_matched: 4,
    });
    expect(fields).toEqual({ table_cells_checked: 4, table_cells_matched: 4 });
  });

  it("never throws on a malformed/empty/null event — this runs inside a live stream handler", () => {
    expect(messageFieldsFromDone({})).toEqual({});
    expect(messageFieldsFromDone(null)).toEqual({});
    expect(messageFieldsFromDone(undefined)).toEqual({});
  });
});
