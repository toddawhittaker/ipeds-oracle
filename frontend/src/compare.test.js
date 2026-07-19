import { describe, it, expect } from "vitest";
import { comparableTable, compareSpec } from "./compare.js";

// A ranking table: one row per university (entity) + a numeric metric, plus a
// rank/index column that must be treated as a dimension (never the entity col).
const RANK_HEADERS = ["Rank", "University", "Enrollment"];
const RANK_ROWS = [
  ["1", "Ohio State", "60,000"],
  ["2", "Michigan", "50,000"],
  ["3", "Penn State", "48,000"],
];

describe("comparableTable", () => {
  it("returns the entity column, labels, and spec for a categorical ranking table", () => {
    const cmp = comparableTable(RANK_HEADERS, RANK_ROWS);
    expect(cmp).not.toBeNull();
    expect(cmp.entityCol).toBe(1); // "University", not the rank/enrollment cols
    expect(cmp.labels).toEqual(["Ohio State", "Michigan", "Penn State"]);
    expect(cmp.spec.type).toBe("bar");
    expect(cmp.spec.x).toBe("University");
  });

  it("rejects a time-series table (year rows are a trend, not a comparison)", () => {
    const headers = ["Year", "Degrees"];
    const rows = [["2020", "100"], ["2021", "120"], ["2022", "140"]];
    expect(comparableTable(headers, rows)).toBeNull(); // chartSpecFromTable → type "line"
  });

  it("rejects a table with no numeric metric", () => {
    const headers = ["State", "Notes"];
    const rows = [["OH", "big"], ["CA", "bigger"]];
    expect(comparableTable(headers, rows)).toBeNull();
  });

  it("rejects a single-row table (nothing to compare)", () => {
    expect(comparableTable(RANK_HEADERS, [["1", "Ohio State", "60,000"]])).toBeNull();
  });
});

describe("compareSpec", () => {
  const spec = comparableTable(RANK_HEADERS, RANK_ROWS).spec;

  it("filters the data to the selected entities and forces a bar snapshot", () => {
    const out = compareSpec(spec, ["Ohio State", "Penn State"]);
    expect(out.type).toBe("bar");
    expect(out.title).toBe("Comparison");
    expect(out.x).toBe(spec.x);
    expect(out.y).toEqual(spec.y); // metric series kept stable
    expect(out.data.map((d) => d.University)).toEqual(["Ohio State", "Penn State"]);
  });

  it("returns null with fewer than 2 matching selections", () => {
    expect(compareSpec(spec, ["Ohio State"])).toBeNull();
    expect(compareSpec(spec, ["Nobody", "Nowhere"])).toBeNull();
    expect(compareSpec(null, ["a", "b"])).toBeNull();
  });
});
