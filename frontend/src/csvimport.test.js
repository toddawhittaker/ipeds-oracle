import { describe, it, expect } from "vitest";
import {
  parseCsv, normalizeHeader, mapColumns, parseAdminFlag,
  isValidEmail, resolveNote, buildImportPlan,
} from "./csvimport.js";

// The pure CSV import pipeline. The browser flow (drop zone, file read, summary/
// confirm UI, error report render) is Playwright's in csv-import.spec.js; this
// owns WHAT the parse/normalize/validate/plan produces. Each case names the
// specific regression it guards.

describe("parseCsv", () => {
  it("splits simple rows and cells", () => {
    expect(parseCsv("a,b,c\n1,2,3")).toEqual([["a", "b", "c"], ["1", "2", "3"]]);
  });
  it("keeps commas inside quoted fields (the reason a hand-rolled split is wrong)", () => {
    expect(parseCsv('email,note\na@x.com,"Chair, CS"')).toEqual([
      ["email", "note"], ["a@x.com", "Chair, CS"],
    ]);
  });
  it("unescapes doubled quotes inside a quoted field", () => {
    expect(parseCsv('note\n"She said ""hi"""')).toEqual([["note"], ['She said "hi"']]);
  });
  it("keeps newlines inside a quoted field as one cell (record spans lines)", () => {
    expect(parseCsv('note\n"line one\nline two"')).toEqual([["note"], ["line one\nline two"]]);
  });
  it("handles CRLF line endings the same as LF", () => {
    expect(parseCsv("a,b\r\n1,2\r\n")).toEqual([["a", "b"], ["1", "2"]]);
  });
  it("does not invent a phantom trailing record after a final newline", () => {
    expect(parseCsv("a\n")).toEqual([["a"]]);
  });
  it("keeps a final record with no trailing newline", () => {
    expect(parseCsv("a\nb")).toEqual([["a"], ["b"]]);
  });
  it("returns no records for empty input", () => {
    expect(parseCsv("")).toEqual([]);
  });
  it("preserves a mid-file blank line as a one-empty-cell record (row numbering)", () => {
    expect(parseCsv("a\n\nb")).toEqual([["a"], [""], ["b"]]);
  });
});

describe("normalizeHeader", () => {
  const variants = ["email", "Email", "E-mail", "e_mail", "E Mail", " EMAIL "];
  for (const v of variants) {
    it(`maps ${JSON.stringify(v)} -> email`, () => expect(normalizeHeader(v)).toBe("email"));
  }
  it("maps note/admin variants", () => {
    expect(normalizeHeader("Note")).toBe("note");
    expect(normalizeHeader("Admin?")).toBe("admin");
    expect(normalizeHeader("is admin")).toBe(null); // "isadmin" != "admin"
  });
  it("returns null for an unknown column (it gets ignored)", () => {
    expect(normalizeHeader("department")).toBe(null);
  });
});

describe("mapColumns", () => {
  it("maps supported columns by index and ignores unknown ones", () => {
    expect(mapColumns(["Email", "Department", "Note", "Admin"]))
      .toEqual({ email: 0, note: 2, admin: 3 });
  });
  it("leaves absent columns null and takes the first match", () => {
    expect(mapColumns(["e-mail", "email"])).toEqual({ email: 0, note: null, admin: null });
  });
});

describe("parseAdminFlag", () => {
  // The whole point of a dedicated parser: the accepted-true set includes "x"
  // and is case/whitespace-insensitive; everything else is false.
  for (const v of ["yes", "y", "t", "true", "1", "x", "YES", "T", "True", " X ", "Yes"]) {
    it(`${JSON.stringify(v)} -> true`, () => expect(parseAdminFlag(v)).toBe(true));
  }
  for (const v of ["no", "n", "f", "false", "0", "", "  ", "maybe", "2", "yep", undefined, null]) {
    it(`${JSON.stringify(v)} -> false`, () => expect(parseAdminFlag(v)).toBe(false));
  }
});

describe("isValidEmail", () => {
  it("accepts a plain address (trimmed)", () => {
    expect(isValidEmail("  a@x.com ")).toBe(true);
  });
  for (const bad of ["", "a@x", "a@@x.com", "no-at-sign", "a @x.com", "@x.com", "a@.com"]) {
    it(`rejects ${JSON.stringify(bad)}`, () => expect(isValidEmail(bad)).toBe(false));
  }
});

describe("resolveNote", () => {
  it("uses the trimmed cell when non-blank", () => {
    expect(resolveNote("  Chair  ", "7/17/2026")).toBe("Chair");
  });
  it("defaults a blank/absent note to Imported on {date}", () => {
    expect(resolveNote("   ", "7/17/2026")).toBe("Imported on 7/17/2026");
    expect(resolveNote(undefined, "7/17/2026")).toBe("Imported on 7/17/2026");
  });
});

describe("buildImportPlan", () => {
  const today = "7/17/2026";
  const plan = (text, existing = []) => buildImportPlan(text, existing, { today });

  it("flags an empty file", () => {
    expect(plan("").headerError).toBe("The file is empty.");
  });

  it("flags a file with no email column and offers nothing to import", () => {
    const p = plan("name,note\nAlex,hi");
    expect(p.headerError).toMatch(/email/i);
    expect(p.ready).toEqual([]);
    expect(p.totalRows).toBe(0);
  });

  it("builds ready rows with resolved note + admin, and counts admins", () => {
    const p = plan("email,note,admin\nalex@example.com,Department chair,yes\njamie@example.com,,\n");
    expect(p.headerError).toBe(null);
    expect(p.totalRows).toBe(2);
    expect(p.ready).toEqual([
      { row: 2, email: "alex@example.com", note: "Department chair", is_admin: true },
      { row: 3, email: "jamie@example.com", note: "Imported on 7/17/2026", is_admin: false },
    ]);
    expect(p.adminCount).toBe(1);
  });

  it("lowercases emails for both the payload and the dedupe/existing checks", () => {
    const p = plan("email\nAlex@Example.com", ["alex@example.com"]);
    expect(p.ready).toEqual([]);
    expect(p.existingOrDuplicate).toEqual([{ row: 2, email: "alex@example.com", reason: "already a user" }]);
  });

  it("imports an in-file duplicate only once (first kept, rest flagged)", () => {
    const p = plan("email\na@x.com\na@x.com\nA@X.com");
    expect(p.ready.map((r) => r.email)).toEqual(["a@x.com"]);
    expect(p.existingOrDuplicate).toEqual([
      { row: 3, email: "a@x.com", reason: "duplicate in file" },
      { row: 4, email: "a@x.com", reason: "duplicate in file" },
    ]);
  });

  it("skips an email already on the allowlist", () => {
    const p = plan("email\nnew@x.com\nold@x.com", ["old@x.com"]);
    expect(p.ready.map((r) => r.email)).toEqual(["new@x.com"]);
    expect(p.existingOrDuplicate).toEqual([{ row: 3, email: "old@x.com", reason: "already a user" }]);
  });

  it("reports missing and invalid emails with the file row number, and keeps going", () => {
    const p = plan("email,note\n,orphan note\nnot-an-email,x\ngood@x.com,ok");
    expect(p.invalid).toEqual([
      { row: 2, email: "", reason: "missing email" },
      { row: 3, email: "not-an-email", reason: "invalid email" },
    ]);
    expect(p.ready.map((r) => r.email)).toEqual(["good@x.com"]);
    expect(p.totalRows).toBe(3);
  });

  it("ignores blank rows entirely (not counted, not reported) but keeps row numbers aligned", () => {
    const p = plan("email\na@x.com\n\n\nb@x.com");
    expect(p.totalRows).toBe(2);
    expect(p.ready).toEqual([
      { row: 2, email: "a@x.com", note: "Imported on 7/17/2026", is_admin: false },
      { row: 5, email: "b@x.com", note: "Imported on 7/17/2026", is_admin: false },
    ]);
  });

  it("ignores unknown columns and treats an absent admin column as all-false", () => {
    const p = plan("email,department\na@x.com,CS");
    expect(p.ready).toEqual([{ row: 2, email: "a@x.com", note: "Imported on 7/17/2026", is_admin: false }]);
    expect(p.adminCount).toBe(0);
  });
});
