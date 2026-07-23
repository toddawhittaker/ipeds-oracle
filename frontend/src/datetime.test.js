import { describe, it, expect } from "vitest";
import { formatStamp, thoughtLabel, shortZone } from "./datetime.js";

describe("thoughtLabel (turn duration → 'Thought for N seconds')", () => {
  it("pluralizes whole seconds", () => {
    expect(thoughtLabel(12000)).toBe("Thought for 12 seconds");
    expect(thoughtLabel(1000)).toBe("Thought for 1 second");
    expect(thoughtLabel(1600)).toBe("Thought for 2 seconds"); // rounds
  });
  it("collapses sub-second to a phrase", () => {
    expect(thoughtLabel(500)).toBe("Thought for less than a second");
    expect(thoughtLabel(0)).toBe("Thought for less than a second");
  });
  it("returns null when there's nothing to show", () => {
    expect(thoughtLabel(null)).toBe(null);
    expect(thoughtLabel(undefined)).toBe(null);
    expect(thoughtLabel(-5)).toBe(null);
    expect(thoughtLabel("nope")).toBe(null);
  });
});

describe("formatStamp (unix seconds → local time + zone)", () => {
  it("renders a time with a zone name for a valid timestamp", () => {
    const s = formatStamp(1_700_000_000); // an arbitrary real instant
    expect(typeof s).toBe("string");
    expect(s).toMatch(/\d/);      // has a time digit
    expect(s).toMatch(/[A-Za-z]/); // has a zone name (e.g. UTC/EST/GMT+0)
  });
  it("returns '' for a non-timestamp", () => {
    expect(formatStamp(0)).toBe("");
    expect(formatStamp(null)).toBe("");
    expect(formatStamp(undefined)).toBe("");
    expect(formatStamp("x")).toBe("");
  });
});

describe("shortZone", () => {
  it("returns a string (the viewer's short zone name)", () => {
    expect(typeof shortZone()).toBe("string");
  });
});
