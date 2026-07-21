import { describe, it, expect } from "vitest";
import { normalizeClarify } from "./clarify.js";

// normalizeClarify turns the backend's `clarify` payload ({question, options[]})
// into a render-ready {question, options[]} or null. Mirrors normalizeSuggestions
// (suggestions.js/suggestions.test.js): defends the chip UI against a malformed/
// empty payload ever rendering a broken disambiguation prompt (no question, or a
// question with zero usable answer-phrases), and against a duplicated/overlong
// option list making the chip row unbounded.
describe("normalizeClarify", () => {
  it("returns null for non-object / empty input (regression: a malformed clarify payload must never render)", () => {
    for (const bad of [null, undefined, "a string", 5, [], {}]) {
      expect(normalizeClarify(bad)).toBeNull();
    }
  });

  it("returns null when the question is missing or blank", () => {
    expect(normalizeClarify({ options: ["Bachelor's only", "All levels"] })).toBeNull();
    expect(normalizeClarify({ question: "", options: ["a"] })).toBeNull();
    expect(normalizeClarify({ question: "   ", options: ["a"] })).toBeNull();
  });

  it("returns null when there are no non-empty options (regression: a question with no answer-phrases has nothing to click)", () => {
    expect(normalizeClarify({ question: "Which award level?", options: [] })).toBeNull();
    expect(normalizeClarify({ question: "Which award level?", options: ["", "   "] })).toBeNull();
    expect(normalizeClarify({ question: "Which award level?" })).toBeNull();
  });

  it("trims the question and every option", () => {
    expect(normalizeClarify({
      question: "  Which award level?  ",
      options: [" Bachelor's only ", " Include all levels "],
    })).toEqual({
      question: "Which award level?",
      options: ["Bachelor's only", "Include all levels"],
    });
  });

  it("de-duplicates options, preserving first-seen order", () => {
    expect(normalizeClarify({
      question: "Which award level?",
      options: ["Bachelor's only", "Bachelor's only", "All levels", " All levels "],
    })).toEqual({
      question: "Which award level?",
      options: ["Bachelor's only", "All levels"],
    });
  });

  it("caps options at 4 (regression: an over-long options array must not blow up the chip row)", () => {
    expect(normalizeClarify({
      question: "Which award level?",
      options: ["a", "b", "c", "d", "e", "f"],
    })).toEqual({ question: "Which award level?", options: ["a", "b", "c", "d"] });
  });
});
