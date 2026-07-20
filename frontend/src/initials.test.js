import { describe, it, expect } from "vitest";
import { initials } from "./initials.js";

describe("initials — avatar monogram from an email", () => {
  it.each([
    // dotted first.last -> two initials, uppercased
    ["todd.whittaker@franklin.edu", "TW"],
    ["Jane.Doe@example.com", "JD"],
    // other name separators
    ["jane_doe@example.com", "JD"],
    ["jane-doe@example.com", "JD"],
    // single-token local part -> first letter only
    ["todd@thewhittakers.org", "T"],
    ["a@b.co", "A"],
    // +tag is a routing tag, not a surname: stripped before splitting
    ["todd+ipeds@franklin.edu", "T"],
    ["todd.whittaker+test@franklin.edu", "TW"],
    // three tokens -> still just the first two
    ["mary.jane.watson@x.edu", "MJ"],
    // a numeric leading chunk is skipped when a name token follows
    ["2024.cohort@x.edu", "C"],
  ])("%s -> %s", (email, expected) => {
    expect(initials(email)).toBe(expected);
  });

  it("returns '?' for an empty or degenerate address", () => {
    expect(initials("")).toBe("?");
    expect(initials(null)).toBe("?");
    expect(initials(undefined)).toBe("?");
    expect(initials("   ")).toBe("?");
    expect(initials("@nolocal.com")).toBe("?");
  });

  it("uppercases a single numeric-only local part's first char", () => {
    // no letter token, but a leading digit still gives a stable monogram
    expect(initials("123@x.com")).toBe("1");
  });
});
