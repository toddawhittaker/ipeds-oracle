import { describe, it, expect } from "vitest";
import {
  USER_SUBTABS,
  DEFAULT_SUBTAB,
  resolveSubTab,
  subTabKeyForArrow,
  pendingBadgeTone,
} from "./usertabs.js";

// The three Users sub-tabs, in order — the source of truth the tablist,
// keyboard nav, and the AdminRoute redirect all read.
describe("USER_SUBTABS", () => {
  it("lists current/pending/blocked in that order with labels", () => {
    expect(USER_SUBTABS.map((t) => t.key)).toEqual(["current", "pending", "blocked"]);
    expect(USER_SUBTABS.map((t) => t.label)).toEqual([
      "Current users", "Pending requests", "Blocked users",
    ]);
  });
  it("defaults to current", () => {
    expect(DEFAULT_SUBTAB).toBe("current");
  });
});

// Regression: a stale bookmark / typo in the :sub segment (/admin/users/bogus)
// must open the default tab, never a blank panel — and a valid key must pass
// through untouched so a deep link actually lands where it points.
describe("resolveSubTab", () => {
  const cases = [
    ["current", "current"],
    ["pending", "pending"],
    ["blocked", "blocked"],
    ["bogus", "current"],
    ["", "current"],
    [undefined, "current"],
    [null, "current"],
    ["Current", "current"], // case-sensitive: the URL segment is lowercase
  ];
  for (const [input, want] of cases) {
    it(`${JSON.stringify(input)} -> ${want}`, () => {
      expect(resolveSubTab(input)).toBe(want);
    });
  }
});

// Regression: keyboard nav on the tablist. Left/Right must WRAP at the ends
// (APG tabs pattern) and Home/End jump to first/last — the index math a bad
// off-by-one or a missing modulo would break, stranding arrow-key users.
describe("subTabKeyForArrow", () => {
  it("right advances and wraps past the last tab", () => {
    expect(subTabKeyForArrow("current", "right")).toBe("pending");
    expect(subTabKeyForArrow("pending", "right")).toBe("blocked");
    expect(subTabKeyForArrow("blocked", "right")).toBe("current");
  });
  it("left retreats and wraps past the first tab", () => {
    expect(subTabKeyForArrow("blocked", "left")).toBe("pending");
    expect(subTabKeyForArrow("pending", "left")).toBe("current");
    expect(subTabKeyForArrow("current", "left")).toBe("blocked");
  });
  it("home/end jump to the first/last tab from anywhere", () => {
    expect(subTabKeyForArrow("pending", "home")).toBe("current");
    expect(subTabKeyForArrow("current", "end")).toBe("blocked");
  });
  it("treats an unknown current key as the first tab so nav never strands", () => {
    expect(subTabKeyForArrow("bogus", "right")).toBe("pending");
    expect(subTabKeyForArrow("bogus", "left")).toBe("blocked");
  });
  it("returns the current key for an unrecognized action", () => {
    expect(subTabKeyForArrow("pending", "space")).toBe("pending");
  });
});

// Regression: the pending badge draws attention ONLY when work is waiting, and
// never with an error tone. Zero pending must fall back to neutral inactive
// styling (the spec forbids error styling for a merely-empty queue).
describe("pendingBadgeTone", () => {
  it("is attention when one or more requests await review", () => {
    expect(pendingBadgeTone(1)).toBe("attention");
    expect(pendingBadgeTone(21)).toBe("attention");
  });
  it("is idle (neutral) when zero", () => {
    expect(pendingBadgeTone(0)).toBe("idle");
  });
});
