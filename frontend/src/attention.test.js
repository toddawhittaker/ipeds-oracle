import { describe, it, expect } from "vitest";
import { attentionTotal, formatBadge, badgeTone } from "./attention.js";

describe("formatBadge", () => {
  const cases = [
    // n, cap, expected — 0/negative suppress the badge entirely
    [0, 99, ""],
    [-3, 99, ""],
    [1, 99, "1"],
    [42, 99, "42"],
    [99, 99, "99"],       // exactly at the cap → plain number
    [100, 99, "99+"],     // one past → capped form
    [5000, 99, "99+"],
    [10, 9, "9+"],        // custom cap
    [9, 9, "9"],
    [NaN, 99, ""],        // a NaN count never NaNs the badge
  ];
  it.each(cases)("formatBadge(%s, cap=%s) → %o", (n, cap, expected) => {
    expect(formatBadge(n, cap)).toBe(expected);
  });

  it("defaults the cap to 99", () => {
    expect(formatBadge(150)).toBe("99+");
    expect(formatBadge(99)).toBe("99");
  });
});

describe("attentionTotal", () => {
  it("sums the three actionable areas", () => {
    expect(attentionTotal({ users: 2, skills: 3, logs: 5 })).toBe(10);
  });
  it("treats missing/NaN counts as 0 (a partial fetch never NaNs the total)", () => {
    expect(attentionTotal({ users: 4 })).toBe(4);
    expect(attentionTotal({ users: 4, skills: NaN, logs: undefined })).toBe(4);
    expect(attentionTotal({})).toBe(0);
    expect(attentionTotal(null)).toBe(0);
    expect(attentionTotal(undefined)).toBe(0);
  });
  it("ignores keys outside the actionable set (imports/usage never badge)", () => {
    expect(attentionTotal({ users: 1, imports: 9, usage: 9 })).toBe(1);
  });
});

describe("badgeTone", () => {
  it("is accent 'attention' while work waits, else neutral 'idle'", () => {
    expect(badgeTone(1)).toBe("attention");
    expect(badgeTone(0)).toBe("idle");
  });
});
