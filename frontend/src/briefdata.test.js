import { describe, it, expect } from "vitest";
import { briefLayout } from "./briefdata.js";

const CHART = '```chart\n{"type":"line","x":"year","y":"n","data":[{"year":2023,"n":1},{"year":2024,"n":2}]}\n```';
const TABLE = "| Year | N |\n| --- | --- |\n| 2023 | 1 |\n| 2024 | 2 |";

describe("briefLayout", () => {
  it("pairs a single table with a single chart (the brief)", () => {
    const r = briefLayout(`synopsis\n\n${TABLE}\n\n${CHART}\n\n*source*`);
    expect(r.pair).toBe(true);
    expect(r.chart).toMatchObject({ type: "line", x: "year" });
  });

  // Regression: pairing must be exactly one-table + one-chart, or we'd suppress a
  // chart with nothing to render it beside / pair the wrong table.
  it("does not pair without a chart, or without a table", () => {
    expect(briefLayout(`prose\n\n${TABLE}`)).toEqual({ pair: false, chart: null });
    expect(briefLayout(`prose\n\n${CHART}`)).toEqual({ pair: false, chart: null });
  });
  it("does not pair with multiple tables or multiple charts", () => {
    expect(briefLayout(`${TABLE}\n\n${TABLE}\n\n${CHART}`).pair).toBe(false);
    expect(briefLayout(`${TABLE}\n\n${CHART}\n\n${CHART}`).pair).toBe(false);
  });
  it("does not pair on invalid chart JSON", () => {
    expect(briefLayout(`${TABLE}\n\n\`\`\`chart\nnot json\n\`\`\``))
      .toEqual({ pair: false, chart: null });
  });
  // A `---` horizontal rule has no pipe, so it must not be miscounted as a table.
  it("ignores a --- horizontal rule when counting tables", () => {
    expect(briefLayout(`prose\n\n---\n\n${TABLE}\n\n${CHART}`).pair).toBe(true);
  });
  it("returns not-paired for non-strings", () => {
    for (const bad of [null, undefined, 5, {}]) {
      expect(briefLayout(bad)).toEqual({ pair: false, chart: null });
    }
  });
});
