import { describe, expect, it } from "vitest";
import { canMuteCategory, categoryLabel, rejectionCountLabel } from "./lessoncats.js";

// frontend/src/admin/lessoncats.js doesn't exist yet (A2, TDD red) -- these pin
// the pure predicates Skills.jsx's "Reject & mute <LABEL>" action and the
// "Rejected (N)" collapsed section are built on. Mirrors the admin/format.js
// precedent (format.test.js): these were destined to live inside a
// browser-tested component file, so extracting them into their own leaf
// module is what makes real input->output coverage possible at all.

const CATEGORIES = [
  { token: "CIP_ROLLUP", label: "CIP rollup double-count", learnable: true, muted: false, pending: 2 },
  { token: "SECOND_MAJOR", label: "Second-major double-count", learnable: true, muted: false, pending: 0 },
  { token: "AWARD_LEVEL", label: "Award-level mixing", learnable: true, muted: true, pending: 1 },
  { token: "MAGNITUDE", label: "Implausible magnitude", learnable: true, muted: false, pending: 0 },
  { token: "QUESTION_MISMATCH", label: "Answer doesn't match the question", learnable: true, muted: false, pending: 0 },
  { token: "UNGROUNDED_NUMBER", label: "Number not in the data", learnable: false, muted: false, pending: 3 },
  { token: "OTHER", label: "Other", learnable: false, muted: false, pending: 0 },
];

describe("canMuteCategory", () => {
  // The one case every other case in this describe is a variation of: all four
  // conditions hold, so the action is offered.
  it("is true when the skill has a learnable, unmuted, known category", () => {
    expect(canMuteCategory({ category: "CIP_ROLLUP" }, CATEGORIES)).toBe(true);
  });

  // Reason 1: no category at all (a pre-existing/seed/feedback row, per
  // migration 35 -- those columns stay NULL). THE REGRESSION this guards:
  // rendering "Reject and mute undefined" for a row with nothing to mute.
  it("is false when the skill has no category (NULL, pre-existing/seed/feedback row)", () => {
    expect(canMuteCategory({ category: null }, CATEGORIES)).toBe(false);
    expect(canMuteCategory({}, CATEGORIES)).toBe(false);
  });

  // Reason 2: the skill's category isn't one the server's live category list
  // recognizes (a stale client, or a category renamed/removed server-side).
  it("is false when the category isn't in the server's category list", () => {
    expect(canMuteCategory({ category: "NOT_A_REAL_TOKEN" }, CATEGORIES)).toBe(false);
  });

  // Reason 3: the category exists but is one of the two permanently-unlearnable
  // ones (UNGROUNDED_NUMBER/OTHER) -- muting it would offer an action that
  // controls nothing, since nothing in that category is ever recorded anyway.
  it("is false when the category is not learnable", () => {
    expect(canMuteCategory({ category: "UNGROUNDED_NUMBER" }, CATEGORIES)).toBe(false);
    expect(canMuteCategory({ category: "OTHER" }, CATEGORIES)).toBe(false);
  });

  // Reason 4: already muted -- offering "Reject & mute" again is a no-op
  // dressed up as an action.
  it("is false when the category is already muted", () => {
    expect(canMuteCategory({ category: "AWARD_LEVEL" }, CATEGORIES)).toBe(false);
  });

  it("is false with no categories list at all (e.g. the categories fetch hasn't landed yet)", () => {
    expect(canMuteCategory({ category: "CIP_ROLLUP" }, [])).toBe(false);
    expect(canMuteCategory({ category: "CIP_ROLLUP" }, undefined)).toBe(false);
  });
});

describe("categoryLabel", () => {
  it("returns the server-provided label for a known token", () => {
    expect(categoryLabel("CIP_ROLLUP", CATEGORIES)).toBe("CIP rollup double-count");
    expect(categoryLabel("OTHER", CATEGORIES)).toBe("Other");
  });

  // THE REGRESSION this pins: a pre-migration-35 row's category is NULL, and a
  // naive lookup/template (`categories.find(...).label`, or string-interpolating
  // the raw token) renders "Reject and mute undefined" or "Reject and mute
  // null" instead of simply not offering a category pill at all.
  it("returns '' for a NULL/missing token, never the literal word 'undefined' or 'null'", () => {
    expect(categoryLabel(null, CATEGORIES)).toBe("");
    expect(categoryLabel(undefined, CATEGORIES)).toBe("");
    expect(categoryLabel("", CATEGORIES)).toBe("");
  });

  it("returns '' for a token the server's category list doesn't recognize", () => {
    expect(categoryLabel("NOT_A_REAL_TOKEN", CATEGORIES)).toBe("");
  });

  it("returns '' when the categories list itself hasn't loaded yet", () => {
    expect(categoryLabel("CIP_ROLLUP", [])).toBe("");
    expect(categoryLabel("CIP_ROLLUP", undefined)).toBe("");
  });
});

describe("rejectionCountLabel", () => {
  // THE REGRESSION this pins (the deniedError precedent, generalized): a load
  // failure must read as an error, never as "confirmed zero" -- "Rejected (0)"
  // on a failed fetch would tell an admin nothing was ever rejected when the
  // truth is the list couldn't be loaded at all.
  it("never claims a count when the load failed", () => {
    const label = rejectionCountLabel([], "Couldn't load rejected lessons.");
    expect(label).not.toContain("(0)");
    expect(label).not.toMatch(/\(0 /);
  });

  it("reads as a confirmed empty state when the load succeeded with nothing rejected", () => {
    const label = rejectionCountLabel([], "");
    expect(label).toMatch(/\(0\b/);
  });

  it("singularizes exactly one rejection", () => {
    const label = rejectionCountLabel([{ id: 1 }], "");
    expect(label).not.toMatch(/1 lessons\b/);
    expect(label).toMatch(/\b1\b/);
  });

  it("pluralizes more than one rejection", () => {
    const label = rejectionCountLabel([{ id: 1 }, { id: 2 }, { id: 3 }], "");
    expect(label).toMatch(/\b3\b/);
    expect(label).not.toMatch(/3 lesson\b/); // must be "lessons", not "lesson"
  });

  it("treats a missing/falsy error the same as no error", () => {
    expect(rejectionCountLabel([{ id: 1 }], null)).toBe(rejectionCountLabel([{ id: 1 }], undefined));
    expect(rejectionCountLabel([{ id: 1 }], null)).toBe(rejectionCountLabel([{ id: 1 }], ""));
  });
});
