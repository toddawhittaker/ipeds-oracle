import { describe, it, expect } from "vitest";
import { filterUsers, sortUsers, paginate, rangeLabel, viewUsers } from "./userlist.js";

// The pure filter/sort/paginate logic behind the admin Users table. Browser
// truth (typing into the search box, clicking headers, the page-size select,
// aria-live) lives in frontend/e2e/; this owns WHAT the pipeline computes.

const U = (email, opts = {}) => ({
  email, note: opts.note ?? "", is_admin: opts.admin ? 1 : 0,
  last_login: opts.login ?? null,
});

describe("filterUsers", () => {
  const rows = [
    U("alice@x.edu", { note: "Registrar" }),
    U("bob@y.edu", { note: "Provost office" }),
    U("carol@x.edu", { note: null }),
  ];

  it("blank/whitespace query returns all rows (not an empty list)", () => {
    expect(filterUsers(rows, "").length).toBe(3);
    expect(filterUsers(rows, "   ").length).toBe(3);
  });
  it("matches email case-insensitively", () => {
    expect(filterUsers(rows, "ALICE").map((r) => r.email)).toEqual(["alice@x.edu"]);
  });
  it("matches note case-insensitively", () => {
    expect(filterUsers(rows, "provost").map((r) => r.email)).toEqual(["bob@y.edu"]);
  });
  it("trims surrounding whitespace before matching", () => {
    expect(filterUsers(rows, "  x.edu  ").map((r) => r.email)).toEqual(
      ["alice@x.edu", "carol@x.edu"]);
  });
  it("treats a null note as empty, not a crash", () => {
    expect(filterUsers(rows, "carol").map((r) => r.email)).toEqual(["carol@x.edu"]);
  });
  it("no match yields an empty array", () => {
    expect(filterUsers(rows, "zzz")).toEqual([]);
  });
});

describe("sortUsers", () => {
  it("email asc is case-insensitive alphabetical (the default)", () => {
    const rows = [U("Zeta@x.edu"), U("alpha@x.edu"), U("Mid@x.edu")];
    expect(sortUsers(rows, "email", "asc").map((r) => r.email)).toEqual(
      ["alpha@x.edu", "Mid@x.edu", "Zeta@x.edu"]);
  });
  it("email desc reverses it", () => {
    const rows = [U("alpha@x.edu"), U("zeta@x.edu")];
    expect(sortUsers(rows, "email", "desc").map((r) => r.email)).toEqual(
      ["zeta@x.edu", "alpha@x.edu"]);
  });
  it("admin groups by the boolean", () => {
    const rows = [U("a@x.edu", { admin: false }), U("b@x.edu", { admin: true }),
      U("c@x.edu", { admin: false })];
    // asc: non-admins (0) before admins (1)
    expect(sortUsers(rows, "admin", "asc").map((r) => r.is_admin)).toEqual([0, 0, 1]);
    expect(sortUsers(rows, "admin", "desc").map((r) => r.is_admin)).toEqual([1, 0, 0]);
  });

  // The one behavior the spec pins: never-logged-in users go to the END when
  // sorting most-recent-first.
  it("last_login desc puts null (never logged in) at the end", () => {
    const rows = [U("never@x.edu"), U("old@x.edu", { login: 100 }),
      U("new@x.edu", { login: 900 })];
    expect(sortUsers(rows, "last_login", "desc").map((r) => r.email)).toEqual(
      ["new@x.edu", "old@x.edu", "never@x.edu"]);
  });
  it("last_login asc groups null at the start (consistent grouping)", () => {
    const rows = [U("old@x.edu", { login: 100 }), U("never@x.edu"),
      U("new@x.edu", { login: 900 })];
    expect(sortUsers(rows, "last_login", "asc").map((r) => r.email)).toEqual(
      ["never@x.edu", "old@x.edu", "new@x.edu"]);
  });

  it("breaks ties on the unique email, NOT the incoming order — so an unstable "
    + "fetch order can't reshuffle a page after a delete", () => {
    // All three tie on note; the input order is deliberately shuffled. The result
    // must be email-ascending regardless, so a refetch that returns the same rows
    // in a different order yields the SAME page (no row jumping off on delete).
    const shuffled = [U("c@x.edu", { note: "same" }), U("a@x.edu", { note: "same" }),
      U("b@x.edu", { note: "same" })];
    expect(sortUsers(shuffled, "note", "asc").map((r) => r.email)).toEqual(
      ["a@x.edu", "b@x.edu", "c@x.edu"]);
    // Same rows, different incoming order -> identical output (determinism).
    const other = [U("b@x.edu", { note: "same" }), U("c@x.edu", { note: "same" }),
      U("a@x.edu", { note: "same" })];
    expect(sortUsers(other, "note", "asc").map((r) => r.email)).toEqual(
      ["a@x.edu", "b@x.edu", "c@x.edu"]);
  });
  it("does not mutate the input array", () => {
    const rows = [U("b@x.edu"), U("a@x.edu")];
    sortUsers(rows, "email", "asc");
    expect(rows.map((r) => r.email)).toEqual(["b@x.edu", "a@x.edu"]);
  });
});

describe("paginate", () => {
  const rows = Array.from({ length: 143 }, (_, i) => U(`u${i}@x.edu`));

  it("computes the 1-based range for a middle page (26–50 of 143)", () => {
    const p = paginate(rows, 2, 25);
    expect(p.start).toBe(26);
    expect(p.end).toBe(50);
    expect(p.total).toBe(143);
    expect(p.totalPages).toBe(6);
    expect(p.slice.length).toBe(25);
  });
  it("last page is a short slice", () => {
    const p = paginate(rows, 6, 25);
    expect(p.start).toBe(126);
    expect(p.end).toBe(143);
    expect(p.slice.length).toBe(18);
  });

  // The removal path: emptying the last page must drop the caller to the prior
  // page rather than showing a blank page. paginate clamps page to totalPages.
  it("clamps an out-of-range page back to the last valid page", () => {
    const twentySix = Array.from({ length: 26 }, (_, i) => U(`u${i}@x.edu`));
    // On page 2 of 26@25 (1 row), remove it -> 25 rows, page 2 no longer exists.
    const after = paginate(twentySix.slice(0, 25), 2, 25);
    expect(after.page).toBe(1);
    expect(after.totalPages).toBe(1);
    expect(after.slice.length).toBe(25);
  });
  it("empty list yields page 1, zero range, one page", () => {
    const p = paginate([], 1, 25);
    expect(p).toMatchObject({ page: 1, totalPages: 1, start: 0, end: 0, total: 0 });
    expect(p.slice).toEqual([]);
  });
});

describe("rangeLabel", () => {
  const cases = [
    { in: { start: 26, end: 50, total: 143 }, out: "Showing 26–50 of 143 users" },
    { in: { start: 1, end: 1, total: 1 }, out: "Showing 1 of 1 user" },
    { in: { start: 1, end: 10, total: 10 }, out: "Showing 1–10 of 10 users" },
    { in: { start: 0, end: 0, total: 0 }, out: "No users" },
  ];
  for (const c of cases) {
    it(c.out, () => expect(rangeLabel(c.in)).toBe(c.out));
  }
});

describe("viewUsers (filter -> sort -> paginate order)", () => {
  const rows = [
    U("carol@a.edu", { note: "b" }), U("alice@a.edu", { note: "a" }),
    U("bob@a.edu", { note: "c" }), U("dave@b.edu", { note: "d" }),
  ];
  it("applies search, then sort, then the page window", () => {
    const v = viewUsers(rows, { query: "a.edu", sortKey: "email", sortDir: "asc", page: 1, perPage: 2 });
    // filter -> 3 a.edu rows; sort email asc -> alice, bob, carol; page 1 of 2 -> [alice, bob]
    expect(v.total).toBe(3);
    expect(v.totalPages).toBe(2);
    expect(v.slice.map((r) => r.email)).toEqual(["alice@a.edu", "bob@a.edu"]);
    expect(v.label).toBe("Showing 1–2 of 3 users");
  });
  it("no search matches -> zero total, 'No users' label", () => {
    const v = viewUsers(rows, { query: "zzz", sortKey: "email", sortDir: "asc", page: 1, perPage: 25 });
    expect(v.total).toBe(0);
    expect(v.slice).toEqual([]);
    expect(v.label).toBe("No users");
  });
});
