import { describe, it, expect } from "vitest";
import { exhaustionLabel, groundedFigureLabel, groundedFigureRate, groundedTableLabel, groundedTableRate, leakLabel, leakRate, promptCacheRate, schemaCacheRate, spendEstimated, spendLabel } from "./usagestats.js";

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

describe("groundedFigureLabel (the stat's sample size)", () => {
  // A bare "100%" hides what it rests on: one checked figure and four hundred
  // render identically. During the observe-only period that difference is the
  // whole point, so the tile carries its own numerator/denominator.
  it("reads N/N with the counts", () => {
    expect(groundedFigureLabel({ figures_checked: 7, figures_ungrounded: 0 }))
      .toBe("7/7 Grounded figures");
    expect(groundedFigureLabel({ figures_checked: 10, figures_ungrounded: 3 }))
      .toBe("7/10 Grounded figures");
  });

  it("appends the suppressed count, and only when there is one", () => {
    // A retry-suppressed turn ships NO figure, so it is rightly outside the
    // rate — but it used to be scored as an ungrounded figure (10 of the 25
    // ungrounded turns in the real log), reading 88.2% against a true 92.5%.
    // Correcting the rate must not make the signal vanish with it.
    expect(groundedFigureLabel({ figures_checked: 10, figures_ungrounded: 1,
                                 figures_suppressed: 4 }))
      .toBe("9/10 Grounded figures · 4 suppressed");
    // Zero suppressions leaves the ordinary label untouched.
    expect(groundedFigureLabel({ figures_checked: 10, figures_ungrounded: 1,
                                 figures_suppressed: 0 }))
      .toBe("9/10 Grounded figures");
    // An older backend sends no such key at all — never invent the claim.
    expect(groundedFigureLabel({ figures_checked: 10, figures_ungrounded: 1 }))
      .toBe("9/10 Grounded figures");
  });

  it("shows suppressions even when nothing else was measured", () => {
    // The window where it matters most: every figure the model tried was forced
    // and withheld, so `checked` is 0 and the rate is "—". Dropping the tail
    // here would render that identically to a clean empty window.
    expect(groundedFigureLabel({ figures_checked: 0, figures_suppressed: 3 }))
      .toBe("Grounded figures · 3 suppressed");
  });

  it("drops the counts when nothing was measured", () => {
    // "0/0 Grounded figures" reads like a failure; an empty window is not one
    // (the rate itself already shows "—").
    expect(groundedFigureLabel({ figures_checked: 0, figures_ungrounded: 0 }))
      .toBe("Grounded figures");
    expect(groundedFigureLabel({})).toBe("Grounded figures");
    expect(groundedFigureLabel(undefined)).toBe("Grounded figures");
  });

  it("coerces string totals", () => {
    expect(groundedFigureLabel({ figures_checked: "4", figures_ungrounded: "1" }))
      .toBe("3/4 Grounded figures");
  });
});

describe("groundedTableRate (cell-level transcription accuracy)", () => {
  // Same misread-dashboard guard as the figure rate: an empty window (no table
  // cells checked) must read "—", never a falsely reassuring "100%".
  it("returns — when no table cell was checked in the window", () => {
    expect(groundedTableRate({ table_cells_checked: 0, table_cells_matched: 0 })).toBe("—");
    expect(groundedTableRate({})).toBe("—");
    expect(groundedTableRate(undefined)).toBe("—");
    expect(groundedTableRate(null)).toBe("—");
  });

  const cases = [
    [318, 318, "100%"],  // every cell reproducible — the healthy state
    [318, 312, "98%"],
    [20, 15, "75%"],
    [10, 0, "0%"],       // nothing reproducible → an honest 0%, never "—"
  ];
  it.each(cases)("checked=%s matched=%s → %s", (table_cells_checked, table_cells_matched, expected) => {
    expect(groundedTableRate({ table_cells_checked, table_cells_matched })).toBe(expected);
  });

  it("coerces string totals", () => {
    expect(groundedTableRate({ table_cells_checked: "20", table_cells_matched: "18" })).toBe("90%");
  });
});

describe("groundedTableLabel (the stat's sample size)", () => {
  it("reads matched/checked with the counts", () => {
    expect(groundedTableLabel({ table_cells_checked: 318, table_cells_matched: 318 }))
      .toBe("318/318 Grounded cells");
    expect(groundedTableLabel({ table_cells_checked: 20, table_cells_matched: 15 }))
      .toBe("15/20 Grounded cells");
  });

  it("drops the counts when nothing was measured", () => {
    expect(groundedTableLabel({ table_cells_checked: 0, table_cells_matched: 0 }))
      .toBe("Grounded cells");
    expect(groundedTableLabel({})).toBe("Grounded cells");
    expect(groundedTableLabel(undefined)).toBe("Grounded cells");
  });

  it("coerces string totals", () => {
    expect(groundedTableLabel({ table_cells_checked: "20", table_cells_matched: "18" }))
      .toBe("18/20 Grounded cells");
  });
});

describe("leakRate / leakLabel (structured-emission telemetry)", () => {
  it("returns — with no measured agent turns", () => {
    expect(leakRate({ emit_turns: 0, leaked_turns: 0 })).toBe("—");
    expect(leakRate({})).toBe("—");
    expect(leakLabel({})).toBe("Answer leaks");
  });

  it("computes the leak rate and the structured-share label", () => {
    const t = { emit_turns: 50, leaked_turns: 1, structured_turns: 50 };
    expect(leakRate(t)).toBe("2%");
    expect(leakLabel(t)).toBe("1/50 leaked · 100% structured");
  });

  it("shows 0% leaks (the healthy structured state)", () => {
    const t = { emit_turns: 40, leaked_turns: 0, structured_turns: 40 };
    expect(leakRate(t)).toBe("0%");
    expect(leakLabel(t)).toBe("0/40 leaked · 100% structured");
  });
});

describe("exhaustionLabel (S5 health stat)", () => {
  it("is a bare label when nothing was degraded (incl. empty window)", () => {
    expect(exhaustionLabel({})).toBe("Exhausted");
    expect(exhaustionLabel(undefined)).toBe("Exhausted");
    expect(exhaustionLabel({ exhausted_turns: 3, degraded_turns: 0 })).toBe("Exhausted");
  });

  it("carries the degraded breakdown once any were degraded", () => {
    expect(exhaustionLabel({ exhausted_turns: 5, degraded_turns: 2 }))
      .toBe("Exhausted · 2 degraded");
    expect(exhaustionLabel({ degraded_turns: "1" })).toBe("Exhausted · 1 degraded");
  });
});

describe("spend provenance (estimated vs provider-billed)", () => {
  // usage_log.cost holds two different kinds of number and rendered both as a
  // plain "$0.63" — so a list-price estimate, which can be off by multiples,
  // read exactly like an invoice. These decide the "~" and the label detail.

  it("is unmarked when every priceable turn was billed by the provider", () => {
    const t = { priceable_turns: 40, estimated_turns: 0, spend: 0.63 };
    expect(spendEstimated(t)).toBe(false);
    expect(spendLabel(t)).toBe("Spend");
  });

  it("says plainly 'estimated' when the whole window is estimated", () => {
    const t = { priceable_turns: 12, estimated_turns: 12 };
    expect(spendEstimated(t)).toBe(true);
    expect(spendLabel(t)).toBe("Spend · estimated");
  });

  it("carries the SPLIT for a window spanning a provider switch", () => {
    // The case a single boolean cannot describe: half the rows carry
    // OpenRouter's billed figure, half are DeepSeek-direct estimates.
    const t = { priceable_turns: 40, estimated_turns: 12 };
    expect(spendEstimated(t)).toBe(true);
    expect(spendLabel(t)).toBe("Spend · 12 of 40 estimated");
  });

  it("never invents an estimated claim from missing counts", () => {
    // An older backend, an empty window, or an e2e fixture predating the
    // columns. Not-knowing must render the unmarked number, never a false mark.
    expect(spendEstimated({})).toBe(false);
    expect(spendEstimated(undefined)).toBe(false);
    expect(spendEstimated({ spend: 1.23 })).toBe(false);
    expect(spendLabel({})).toBe("Spend");
    expect(spendLabel(undefined)).toBe("Spend");
  });

  it("coerces string counts (JSON numbers can arrive as strings)", () => {
    expect(spendEstimated({ estimated_turns: "3" })).toBe(true);
    expect(spendLabel({ estimated_turns: "3", priceable_turns: "9" }))
      .toBe("Spend · 3 of 9 estimated");
  });
});
