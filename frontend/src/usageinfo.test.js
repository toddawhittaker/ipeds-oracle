import { describe, it, expect } from "vitest";
import { STAT_INFO, directionHint } from "./usageinfo.js";

describe("directionHint", () => {
  it("maps each direction to its guidance line", () => {
    expect(directionHint("up")).toBe("Higher is better.");
    expect(directionHint("down")).toBe("Lower is better.");
    expect(directionHint("flat")).toBe(
      "Just a count — neither high nor low is inherently good.");
  });
  it("falls back to the neutral hint for an unknown/absent direction", () => {
    // A typo'd direction must never render a blank hint — it degrades to "count".
    expect(directionHint("sideways")).toBe(directionHint("flat"));
    expect(directionHint(undefined)).toBe(directionHint("flat"));
  });
});

describe("STAT_INFO integrity", () => {
  // Guards the real regression: a stat wired in Admin.jsx whose help entry is
  // missing a name, missing/blank explanation, or carries a direction the hint
  // function doesn't understand — any of which renders a broken info bubble.
  const VALID = new Set(["up", "down", "flat"]);
  for (const [key, info] of Object.entries(STAT_INFO)) {
    it(`${key} has a name, a real explanation, and a known direction`, () => {
      expect(info.name, `${key}.name`).toBeTruthy();
      expect(info.what.length, `${key}.what`).toBeGreaterThan(30);
      expect(VALID.has(info.direction), `${key}.direction=${info.direction}`).toBe(true);
      if (info.note !== undefined) expect(info.note.length).toBeGreaterThan(0);
    });
  }
});
