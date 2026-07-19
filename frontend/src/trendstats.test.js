import { describe, it, expect } from "vitest";
import { linearFit, trendValues, pctChange } from "./trendstats.js";

describe("linearFit", () => {
  it("fits a perfect upward line", () => {
    const f = linearFit([1, 2, 3, 4]);
    expect(f.slope).toBeCloseTo(1); expect(f.intercept).toBeCloseTo(1);
  });
  it("fits a downward line", () => {
    const f = linearFit([10, 8, 6, 4]);
    expect(f.slope).toBeCloseTo(-2); expect(f.intercept).toBeCloseTo(10);
  });
  // Regression: a missing/NaN cell must not skew the fit — the point is dropped.
  it("ignores non-finite values", () => {
    expect(linearFit([1, 2, NaN, 4]).slope).toBeCloseTo(1);
  });
  it("returns null with fewer than 2 usable points", () => {
    expect(linearFit([5])).toBeNull();
    expect(linearFit([NaN, NaN, 3])).toBeNull();
    expect(linearFit([])).toBeNull();
  });
});

describe("trendValues", () => {
  it("returns the fitted y at each row", () => {
    expect(trendValues([{ y: 1 }, { y: 2 }, { y: 3 }], "y").map((v) => Math.round(v)))
      .toEqual([1, 2, 3]);
  });
  it("returns null when a fit isn't possible", () => {
    expect(trendValues([{ y: 1 }], "y")).toBeNull();
  });
});

describe("pctChange", () => {
  it("computes an increase (up)", () => {
    const d = pctChange([{ n: 100 }, { n: 110 }], "n");
    expect(d.pct).toBeCloseTo(10); expect(d.dir).toBe("up");
    expect(d.first).toBe(100); expect(d.last).toBe(110);
  });
  it("computes a decrease (down)", () => {
    expect(pctChange([{ n: 200 }, { n: 150 }], "n").dir).toBe("down");
  });
  it("flags flat within ±0.5%", () => {
    expect(pctChange([{ n: 1000 }, { n: 1003 }], "n").dir).toBe("flat");
  });
  // Regression: first→last uses the first and last FINITE values, skipping gaps.
  it("skips gaps for the first/last endpoints", () => {
    const d = pctChange([{ n: 100 }, { n: NaN }, { n: 120 }], "n");
    expect(d.first).toBe(100); expect(d.last).toBe(120);
  });
  it("returns null from a zero base or with fewer than 2 points", () => {
    expect(pctChange([{ n: 0 }, { n: 5 }], "n")).toBeNull();
    expect(pctChange([{ n: 5 }], "n")).toBeNull();
  });
});
