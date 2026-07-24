import { describe, expect, it } from "vitest";

import {
  editConfirmBody,
  editConfirmLabel,
  laterTurnsLost,
  needsDestructiveConfirm,
} from "./turns.js";

// Build a strictly alternating user/assistant thread of `n` turns, the shape
// Chat.jsx's submit(), the server's conversation load, and the e2e mocks all
// produce.
const thread = (n) =>
  Array.from({ length: n * 2 }, (_, k) =>
    k % 2 === 0
      ? { role: "user", content: `q${k / 2}` }
      : { role: "assistant", content: `a${(k - 1) / 2}` });

describe("laterTurnsLost", () => {
  // THE REGRESSION: an off-by-one here either nags on every ordinary refine
  // (and breaks the existing "Try again" e2e, which reruns the last turn) or —
  // worse — stays silent on the destructive case the confirmation exists for.
  it.each([
    // [turns, edited user index, expected collateral turns]
    [3, 0, 2],   // edit the first of three -> two later exchanges die
    [3, 2, 1],   // edit the middle -> one dies
    [3, 4, 0],   // edit the LAST -> nothing collateral: the safe path
    [1, 0, 0],   // the only turn
    [8, 0, 7],
    [8, 14, 0],  // last of eight
  ])("thread of %i turns, editing index %i, loses %i later turns", (turns, index, expected) => {
    expect(laterTurnsLost(thread(turns), index)).toBe(expected);
  });

  it("returns 0 for an index past the end, rather than a negative count", () => {
    expect(laterTurnsLost(thread(2), 99)).toBe(0);
  });

  it("returns 0 for a partial trailing turn whose answer hasn't arrived", () => {
    // Mid-stream the assistant row exists but a failed load could leave an odd
    // tail; either way there is no COMPLETE later exchange to destroy.
    const partial = [...thread(1), { role: "user", content: "q1" }];
    expect(laterTurnsLost(partial, 2)).toBe(0);
  });

  it.each([[null], [undefined], [[]], ["nope"]])("survives a non-thread (%s)", (bad) => {
    expect(laterTurnsLost(bad, 0)).toBe(0);
  });

  it.each([[-1], [1.5], [NaN]])("returns 0 for a non-index (%s)", (bad) => {
    expect(laterTurnsLost(thread(3), bad)).toBe(0);
  });
});

describe("needsDestructiveConfirm", () => {
  it("is false for the last turn — the ordinary refine must stay modal-free", () => {
    expect(needsDestructiveConfirm(thread(4), 6)).toBe(false);
  });

  it("is true as soon as one later exchange would be lost", () => {
    expect(needsDestructiveConfirm(thread(4), 4)).toBe(true);
  });

  it("is false for a single-turn conversation", () => {
    expect(needsDestructiveConfirm(thread(1), 0)).toBe(false);
  });
});

describe("confirm copy", () => {
  it("singularizes one lost exchange", () => {
    expect(editConfirmBody(1)).toContain("1 later question and its answer");
    expect(editConfirmLabel(1)).toBe("Edit and remove 1 later exchange");
  });

  it("pluralizes several", () => {
    expect(editConfirmBody(4)).toContain("4 later questions and their answers");
    expect(editConfirmLabel(4)).toBe("Edit and remove 4 later exchanges");
  });

  it("states irreversibility and names what else is destroyed", () => {
    // The count alone under-sells it: the tables/charts/SQL go too, and that is
    // the work the user would actually mourn.
    const body = editConfirmBody(2);
    expect(body).toMatch(/can't be undone/i);
    expect(body).toMatch(/tables, charts and SQL/i);
  });

  it("takes the verb so Rerun doesn't say Edit", () => {
    expect(editConfirmLabel(2, "Rerun")).toBe("Rerun and remove 2 later exchanges");
  });
});
