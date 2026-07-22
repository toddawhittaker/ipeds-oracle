import { describe, it, expect } from "vitest";
import { groundedFigureRate, promptCacheRate, schemaCacheRate } from "./usagestats.js";

// Both rates share one guarded ratio helper; the regression each guards is the
// same: a naive cached/total renders "NaN%"/"Infinity%" on an empty window (0
// tokens) — the exact state a fresh deployment or a quiet range shows. It must
// read "—". They differ only in WHICH token columns they divide.

describe("promptCacheRate (blended, all calls)", () => {
  it("returns — when there are no prompt tokens to divide by", () => {
    expect(promptCacheRate({ prompt_tokens: 0, cached_prompt_tokens: 0 })).toBe("—");
    expect(promptCacheRate({ prompt_tokens: 0, cached_prompt_tokens: 5 })).toBe("—");
  });

  it("returns — for missing/empty/absent totals", () => {
    expect(promptCacheRate(undefined)).toBe("—");
    expect(promptCacheRate(null)).toBe("—");
    expect(promptCacheRate({})).toBe("—");
  });

  const cases = [
    [1000, 900, "90%"],
    [1000, 0, "0%"],       // real traffic, cold cache → an honest 0%, not "—"
    [1000, 1000, "100%"],
    [3, 1, "33%"],         // rounds to whole percent
    [3, 2, "67%"],
  ];
  it.each(cases)("prompt=%s cached=%s → %s", (prompt_tokens, cached_prompt_tokens, expected) => {
    expect(promptCacheRate({ prompt_tokens, cached_prompt_tokens })).toBe(expected);
  });

  it("coerces string totals (JSON numbers can arrive as strings)", () => {
    expect(promptCacheRate({ prompt_tokens: "1000", cached_prompt_tokens: "500" })).toBe("50%");
  });
});

describe("schemaCacheRate (first call only)", () => {
  it("divides the first_call_* columns, not the blended ones", () => {
    // Given a blended rate that would read 90% but a first-call rate of 40%, the
    // schema stat must report the FIRST-CALL number — the whole point of the
    // split is that these two can diverge.
    const totals = {
      prompt_tokens: 1000, cached_prompt_tokens: 900,
      first_call_prompt_tokens: 500, first_call_cached_prompt_tokens: 200,
    };
    expect(schemaCacheRate(totals)).toBe("40%");
    expect(promptCacheRate(totals)).toBe("90%");
  });

  it("returns — with no first-call prompt tokens (empty window / unreported)", () => {
    expect(schemaCacheRate({ first_call_prompt_tokens: 0, first_call_cached_prompt_tokens: 0 })).toBe("—");
    expect(schemaCacheRate({})).toBe("—");
    expect(schemaCacheRate(undefined)).toBe("—");
  });
});

describe("groundedFigureRate (data integrity, not cost)", () => {
  // The regression this guards is a MISREAD dashboard, not a crash: this stat
  // answers "are figures reaching users that the server can't reproduce from
  // its own data?", so a window with nothing to measure must read "—" and never
  // a falsely reassuring "100%".
  it("returns — when no figure was checked in the window", () => {
    expect(groundedFigureRate({ figures_checked: 0, figures_ungrounded: 0 })).toBe("—");
    expect(groundedFigureRate({})).toBe("—");
    expect(groundedFigureRate(undefined)).toBe("—");
    expect(groundedFigureRate(null)).toBe("—");
  });

  const cases = [
    [100, 0, "100%"],   // every figure reproducible — the healthy state
    [100, 1, "99%"],
    [4, 1, "75%"],
    [3, 3, "0%"],       // nothing reproducible → an honest 0%, never "—"
  ];
  it.each(cases)("checked=%s ungrounded=%s → %s", (figures_checked, figures_ungrounded, expected) => {
    expect(groundedFigureRate({ figures_checked, figures_ungrounded })).toBe(expected);
  });

  it("coerces string totals (JSON numbers can arrive as strings)", () => {
    expect(groundedFigureRate({ figures_checked: "10", figures_ungrounded: "2" })).toBe("80%");
  });
});
