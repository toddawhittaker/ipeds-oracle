import { describe, it, expect } from "vitest";
import { isFigureVerified, normalizeFigure } from "./figure.js";

// The figure normalizer is the last gate before rendering a hero statistic. Its
// contract: value AND label required (no headline number / caption → no figure),
// only the four known keys survive, everything is a trimmed string.
describe("normalizeFigure", () => {
  it("passes a full valid spec through, trimmed", () => {
    expect(normalizeFigure(
      { value: " 7,679 ", unit: " degrees ", label: " CS bachelor's ", source: " IPEDS " }))
      .toEqual({ value: "7,679", unit: "degrees", label: "CS bachelor's", source: "IPEDS" });
  });

  // Regression: without a number OR a caption there is nothing to typeset — a
  // half-spec must never render as a lopsided figure.
  it("requires a non-empty value AND label", () => {
    expect(normalizeFigure({ value: "5" })).toBeNull();              // no label
    expect(normalizeFigure({ label: "x" })).toBeNull();             // no value
    expect(normalizeFigure({ value: "", label: "x" })).toBeNull();  // empty value
    expect(normalizeFigure({ value: "5", label: "   " })).toBeNull(); // whitespace label
  });

  // Regression: the spec comes from model output / a stored column — never spread
  // unknown keys into the render.
  it("keeps only value/unit/label/source, dropping anything else", () => {
    expect(normalizeFigure({ value: "5", label: "x", evil: "drop", data: [1, 2] }))
      .toEqual({ value: "5", label: "x" });
  });

  it("coerces a numeric value to a string", () => {
    expect(normalizeFigure({ value: 7679, label: "x" })).toEqual({ value: "7679", label: "x" });
  });

  it("omits optional keys that are empty or absent", () => {
    expect(normalizeFigure({ value: "5", label: "x", unit: "", source: null }))
      .toEqual({ value: "5", label: "x" });
  });

  it("returns null for non-objects", () => {
    for (const bad of [null, undefined, "str", 5, [], true]) {
      expect(normalizeFigure(bad)).toBeNull();
    }
  });
});

// THE REGRESSION these guard is a mark appearing on a number the server could
// NOT reproduce — the one failure mode that would make the badge worse than no
// badge, because it lends confidence rather than withholding it.
describe("isFigureVerified", () => {
  it("marks the three reproduced statuses", () => {
    for (const s of ["exact", "rounded", "derived"]) {
      expect(isFigureVerified(s)).toBe(true);
    }
  });

  it("never marks a figure the checker could not reproduce", () => {
    expect(isFigureVerified("ungrounded")).toBe(false);
  });

  // Not-checked is not the same as not-reproduced, but it earns no mark either:
  // the badge claims verification happened, so silence is the honest output.
  it("does not mark a verdict that was never reached", () => {
    for (const s of ["no_figure", "malformed", "unchecked", "", null, undefined]) {
      expect(isFigureVerified(s)).toBe(false);
    }
  });

  // Guards a confusion this very test caught: llm.py ALSO records
  // `figure_derivation` — a composed provenance string like
  // `retry:ctx:sum(q3.awards)`. That field is backend-only telemetry and is a
  // different column; figure_grounding is only ever a bare status. Treating a
  // derivation as a status (or vice versa) must not produce a mark.
  it("does not mark a derivation string mistaken for a status", () => {
    for (const d of ["retry:ctx:sum(q3.awards)", "sum(q2.total)", "retry:suppressed"]) {
      expect(isFigureVerified(d)).toBe(false);
    }
  });

  it("ignores case and surrounding whitespace", () => {
    expect(isFigureVerified("  EXACT  ")).toBe(true);
  });

  it("returns false for non-strings rather than throwing", () => {
    for (const bad of [5, {}, [], true]) expect(isFigureVerified(bad)).toBe(false);
  });
});
