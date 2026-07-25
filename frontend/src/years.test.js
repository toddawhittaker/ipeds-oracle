import { describe, expect, it } from "vitest";
import { collectionYearLabel, collectionYearRange } from "./years.js";

// The regression these guard is an OFF-BY-ONE that is invisible on screen:
// IPEDS stores the ENDING year, so labelling 2020 as "2020-21" instead of
// "2019-20" mislabels the entire dataset by a year and still looks plausible.
describe("collectionYearLabel", () => {
  it("reaches back a year — 2020 is the 2019-20 collection", () =>
    expect(collectionYearLabel(2020)).toBe("2019-20"));
  it("zero-pads the ending year across a century rollover", () =>
    expect(collectionYearLabel(2100)).toBe("2099-00"));
  it("pads a single-digit ending year", () =>
    expect(collectionYearLabel(2009)).toBe("2008-09"));
  it("returns empty for a non-number rather than 'NaN-aN'", () =>
    expect(collectionYearLabel(undefined)).toBe(""));
});

describe("collectionYearRange", () => {
  it("names the full span", () =>
    expect(collectionYearRange({ min: 2020, max: 2025 }))
      .toBe("collection years 2019-20 through 2024-25"));

  // A fresh deployment that imported one year is common, not an edge case, and
  // "2024-25 through 2024-25" reads as a bug to whoever sees it.
  it("collapses a single loaded year instead of repeating it", () =>
    expect(collectionYearRange({ min: 2025, max: 2025 }))
      .toBe("collection year 2024-25"));

  it("returns empty when nothing is loaded", () => {
    expect(collectionYearRange(null)).toBe("");
    expect(collectionYearRange(undefined)).toBe("");
  });

  it("returns empty on malformed bounds rather than a half-written sentence", () =>
    expect(collectionYearRange({ min: null, max: 2025 })).toBe(""));
});
