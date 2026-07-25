import { describe, expect, it } from "vitest";
import {
  canonEmailForDisplay,
  fmtApprovalDate,
  fmtDateTime,
  humanBytes,
  humanSeconds,
  money,
  ruleName,
} from "./format.js";

// These lived inside Admin.jsx — a browser-tested component file — so none of
// them had fast-tier coverage despite being pure input→output logic with real
// boundaries to get wrong. Extracting them is what makes these tests possible.

describe("humanBytes", () => {
  it("keeps whole bytes whole and scales the rest to one decimal", () => {
    expect(humanBytes(512)).toBe("512 B");
    expect(humanBytes(1024)).toBe("1.0 KB");
    expect(humanBytes(1536)).toBe("1.5 KB");
  });

  // The dataset is ~2 GB, so the GB/TB steps are the ones actually on screen.
  it("climbs the unit ladder", () => {
    expect(humanBytes(1024 ** 3)).toBe("1.0 GB");
    expect(humanBytes(2.5 * 1024 ** 3)).toBe("2.5 GB");
    expect(humanBytes(1024 ** 4)).toBe("1.0 TB");
  });

  // Caps at TB rather than walking off the end of the units array into
  // "undefined" — the loop's `i < units.length - 1` bound.
  it("stops at the largest unit it knows", () => {
    expect(humanBytes(1024 ** 6)).toMatch(/TB$/);
  });

  it("renders a missing/garbage size as ? rather than NaN", () => {
    for (const bad of [null, undefined, NaN, Infinity]) expect(humanBytes(bad)).toBe("?");
  });
});

describe("humanSeconds", () => {
  it("reads in seconds under a minute", () => {
    expect(humanSeconds(0)).toBe("0s");
    expect(humanSeconds(45)).toBe("45s");
  });

  // A round minute must not render "5m 0s".
  it("drops a zero seconds remainder", () => {
    expect(humanSeconds(300)).toBe("5m");
  });

  it("keeps a non-zero remainder", () => {
    expect(humanSeconds(90)).toBe("1m 30s");
  });

  it("renders a missing/garbage duration as ?", () => {
    for (const bad of [null, undefined, NaN, Infinity]) expect(humanSeconds(bad)).toBe("?");
  });
});

// THE REGRESSION here is a real security-adjacent one: this decides which rows
// the Blocked-users table groups together, and it must mirror the backend's
// canon_email. Stripping dots (a tempting "normalization") would over-block —
// at many providers first.last@ and firstlast@ are DIFFERENT REAL PEOPLE.
describe("canonEmailForDisplay", () => {
  it("lowercases and strips a +tag from the local part", () => {
    expect(canonEmailForDisplay("Victim+Newsletter@Example.edu"))
      .toBe("victim@example.edu");
  });

  it("LEAVES DOTS ALONE — they can be a different person", () => {
    expect(canonEmailForDisplay("first.last@example.edu")).toBe("first.last@example.edu");
  });

  // A + in the DOMAIN is not a tag; only the local part is split.
  it("only strips a tag from the local part", () => {
    expect(canonEmailForDisplay("a@b+c.edu")).toBe("a@b+c.edu");
  });

  it("passes through a string with no @ rather than mangling it", () => {
    expect(canonEmailForDisplay("  NotAnEmail ")).toBe("notanemail");
  });
});

describe("fmtDateTime", () => {
  // Unix SECONDS, not ms — passing ms would land in the year 55000 and still
  // render a plausible-looking string, which is why this pins the year.
  it("reads its input as seconds", () => {
    expect(fmtDateTime(1750000000)).toContain("2025");
  });

  it("renders an absent timestamp as an em dash, never 1970", () => {
    expect(fmtDateTime(null)).toBe("—");
    expect(fmtDateTime(0)).toBe("—");
    expect(fmtDateTime(undefined)).toBe("—");
  });
});

describe("fmtApprovalDate", () => {
  // Deliberately NOT asserting a format: dates render in the VIEWER's locale
  // app-wide, so pinning "7/25/2026" would encode one machine's locale as the
  // contract. What matters is that it produces the date it was given.
  it("formats the date it is given, in the viewer's locale", () => {
    const d = new Date(2026, 6, 25);
    expect(fmtApprovalDate(d)).toBe(d.toLocaleDateString());
  });

  it("defaults to today", () => {
    expect(fmtApprovalDate()).toBe(new Date().toLocaleDateString());
  });
});

describe("money", () => {
  // Sub-dollar precision is the whole point: at 2 places a per-query cost
  // rounds to $0.00 and the entire spend column reads as free.
  it("keeps 4 places under a dollar", () => {
    expect(money(0.0034)).toBe("$0.0034");
    expect(money(0.9999)).toBe("$0.9999");
  });

  it("uses 2 places from a dollar up", () => {
    expect(money(1)).toBe("$1.00");
    expect(money(12.345)).toBe("$12.35");
  });

  it("treats missing spend as zero", () => {
    expect(money(null)).toBe("$0.0000");
    expect(money(undefined)).toBe("$0.0000");
  });
});

describe("ruleName", () => {
  it("prefers the generalized headline", () => {
    expect(ruleName({ headline: "H", lesson: "L", notes: "N", question: "Q" })).toBe("H");
  });

  // A seeded or older row may carry only the later fields — the fallback order
  // is what keeps those rows from all rendering as "untitled lesson".
  it("falls back through lesson → notes → question", () => {
    expect(ruleName({ lesson: "L", notes: "N", question: "Q" })).toBe("L");
    expect(ruleName({ notes: "N", question: "Q" })).toBe("N");
    expect(ruleName({ question: "Q" })).toBe("Q");
  });

  it("names an otherwise-empty lesson rather than rendering blank", () => {
    expect(ruleName({})).toBe("untitled lesson");
    expect(ruleName({ headline: "" })).toBe("untitled lesson");
  });
});
