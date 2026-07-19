import { describe, it, expect } from "vitest";
import { normalizeFigure } from "./figure.js";

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
