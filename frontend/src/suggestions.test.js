import { describe, it, expect } from "vitest";
import { normalizeSuggestions } from "./suggestions.js";

describe("normalizeSuggestions", () => {
  it("keeps non-empty trimmed strings", () => {
    expect(normalizeSuggestions([" How about Texas? ", "Which programs?"]))
      .toEqual(["How about Texas?", "Which programs?"]);
  });
  it("caps at 3", () => {
    expect(normalizeSuggestions(["a", "b", "c", "d", "e"])).toEqual(["a", "b", "c"]);
  });
  it("drops blanks and de-duplicates", () => {
    expect(normalizeSuggestions(["a", "", "  ", "a", "b"])).toEqual(["a", "b"]);
  });
  it("returns [] for non-arrays or empties", () => {
    for (const bad of [null, undefined, "str", 5, {}, [], ["", "  "]]) {
      expect(normalizeSuggestions(bad)).toEqual([]);
    }
  });
});
