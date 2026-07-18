import { describe, expect, it } from "vitest";
import { filterRows, sortRows } from "./datatable.js";
import { BLOCKED_CONFIG, PENDING_CONFIG, numCmp, strCmp } from "./accesstables.js";

// Pins the pure logic behind the Pending/Blocked admin tables: the comparators
// (incl. the null-safe denied_at sort), the canon+originals search field, the
// stable id tiebreak, and the nouns. These run only through Playwright otherwise.

describe("comparator helpers", () => {
  it("strCmp is case-insensitive and null-safe", () => {
    expect(strCmp("Bravo", "alpha")).toBeGreaterThan(0);
    expect(strCmp("alpha", "ALPHA")).toBe(0);
    expect(strCmp(null, "a")).toBeLessThan(0);
    expect(strCmp(null, null)).toBe(0);
  });

  it("numCmp treats null/undefined as 0 (no NaN)", () => {
    expect(numCmp(5, 3)).toBe(2);
    expect(numCmp(null, 3)).toBe(-3);
    expect(numCmp(5, undefined)).toBe(5);
    expect(Number.isNaN(numCmp(null, null))).toBe(false);
  });
});

describe("PENDING_CONFIG", () => {
  const rows = [
    { id: 2, email: "Zed@example.edu", created_at: 100 },
    { id: 1, email: "amy@example.edu", created_at: 300 },
    { id: 3, email: "bo@example.edu", created_at: 100 },
  ];

  it("sorts by email case-insensitively", () => {
    expect(sortRows(rows, "email", "asc", PENDING_CONFIG).map((r) => r.id)).toEqual([1, 3, 2]);
  });

  it("sorts by requested (created_at) with a stable id tiebreak", () => {
    // desc: 300 first, then the 100-tie broken by id ("2" < "3").
    expect(sortRows(rows, "requested", "desc", PENDING_CONFIG).map((r) => r.id)).toEqual([1, 2, 3]);
  });

  it("searches by email only", () => {
    expect(filterRows(rows, "amy", PENDING_CONFIG.fields).map((r) => r.id)).toEqual([1]);
    expect(filterRows(rows, "zed", PENDING_CONFIG.fields).map((r) => r.id)).toEqual([2]);
  });
});

describe("BLOCKED_CONFIG", () => {
  const rows = [
    { id: 1, canon_email: "victim@example.edu", emails: ["victim+1@example.edu", "victim@example.edu"],
      created_at: 100, denied_at: 500 },
    { id: 2, canon_email: "legacy@example.edu", emails: ["legacy@example.edu"],
      created_at: 200, denied_at: null }, // pre-migration: no denied_at
    { id: 3, canon_email: "abe@example.edu", emails: ["abe+x@example.edu"],
      created_at: 300, denied_at: 400 },
  ];

  it("default-sorts by denied newest-first, null denied_at sorting last on desc", () => {
    // desc: 500, 400, then null(→0). NaN would scatter these unpredictably.
    expect(sortRows(rows, "denied", "desc", BLOCKED_CONFIG).map((r) => r.id)).toEqual([1, 3, 2]);
  });

  it("sorts by canonical email and by requested independently", () => {
    expect(sortRows(rows, "email", "asc", BLOCKED_CONFIG).map((r) => r.id)).toEqual([3, 2, 1]);
    expect(sortRows(rows, "requested", "asc", BLOCKED_CONFIG).map((r) => r.id)).toEqual([1, 2, 3]);
  });

  it("searches over canon_email AND the original addresses (function field)", () => {
    // The base address (blocked) is findable...
    expect(filterRows(rows, "victim@", BLOCKED_CONFIG.fields).map((r) => r.id)).toEqual([1]);
    // ...and so is a +tag ORIGINAL that isn't the canonical address.
    expect(filterRows(rows, "abe+x", BLOCKED_CONFIG.fields).map((r) => r.id)).toEqual([3]);
  });

  it("carries the blocked-user nouns", () => {
    expect(BLOCKED_CONFIG.nouns).toEqual({ one: "blocked user", many: "blocked users" });
  });
});
