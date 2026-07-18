import { describe, expect, it } from "vitest";
import {
  filterRows,
  paginate,
  rangeLabel,
  sortRows,
  viewRows,
} from "./datatable.js";

// Generic pipeline tests for datatable.js, using a synthetic row shape distinct
// from the user rows in userlist.test.js — so these pin the GENERALIZATION
// (function-accessor search fields, config-driven comparators/tiebreak, noun-
// parameterized labels) that the access-request tables rely on, not just the
// user case. A row here: { id, name, tags: string[], score, when }.

const rows = [
  { id: 3, name: "Bravo", tags: ["alpha@x.edu"], score: 10, when: 100 },
  { id: 1, name: "alpha", tags: ["a@x.edu", "a+tag@x.edu"], score: 10, when: 300 },
  { id: 2, name: "Charlie", tags: [], score: 5, when: 200 },
];

const CONFIG = {
  fields: ["name", (r) => r.tags.join(" ")],
  comparators: {
    name: (a, b) => (a.name || "").localeCompare(b.name || "", undefined, { sensitivity: "base" }),
    score: (a, b) => a.score - b.score,
    when: (a, b) => a.when - b.when,
  },
  tiebreak: (r) => r.id,
  nouns: { one: "request", many: "requests" },
};

describe("filterRows", () => {
  it("returns a COPY of all rows for a blank/whitespace query", () => {
    const out = filterRows(rows, "   ", CONFIG.fields);
    expect(out).toHaveLength(3);
    expect(out).not.toBe(rows);
  });

  it("matches case-insensitively across a string-key field", () => {
    expect(filterRows(rows, "BRAVO", CONFIG.fields).map((r) => r.id)).toEqual([3]);
  });

  it("matches via a FUNCTION field accessor (e.g. a joined array column)", () => {
    // The Blocked table searches over its emails[] array; a +tag original must
    // be findable even though it isn't a plain string column.
    expect(filterRows(rows, "a+tag", CONFIG.fields).map((r) => r.id)).toEqual([1]);
  });

  it("trims surrounding whitespace on the query", () => {
    expect(filterRows(rows, "  charlie  ", CONFIG.fields).map((r) => r.id)).toEqual([2]);
  });

  it("returns [] when nothing matches", () => {
    expect(filterRows(rows, "zzz", CONFIG.fields)).toEqual([]);
  });
});

describe("sortRows", () => {
  it("sorts by a string comparator ascending and descending", () => {
    expect(sortRows(rows, "name", "asc", CONFIG).map((r) => r.name)).toEqual(["alpha", "Bravo", "Charlie"]);
    expect(sortRows(rows, "name", "desc", CONFIG).map((r) => r.name)).toEqual(["Charlie", "Bravo", "alpha"]);
  });

  it("falls back to the first comparator for an unknown sortKey", () => {
    // keys order: name, score, when -> unknown key sorts by name asc.
    expect(sortRows(rows, "nope", "asc", CONFIG).map((r) => r.name)).toEqual(["alpha", "Bravo", "Charlie"]);
  });

  it("breaks ties deterministically on the unique tiebreak, regardless of input order", () => {
    // id 3 and id 1 both score 10 -> tie broken by id ("1" < "3"), stable across
    // shuffles. This guards the anti-reshuffle guarantee for non-total orders.
    const forward = sortRows(rows, "score", "asc", CONFIG).map((r) => r.id);
    const shuffled = sortRows([rows[1], rows[0], rows[2]], "score", "asc", CONFIG).map((r) => r.id);
    expect(forward).toEqual([2, 1, 3]); // score 5 first, then the 10-tie by id
    expect(shuffled).toEqual(forward);
  });

  it("does not flip the tiebreak with descending direction", () => {
    // score desc: the two 10s come first, still ordered id 1 then 3 (tiebreak
    // stays ASC), then score 5.
    expect(sortRows(rows, "score", "desc", CONFIG).map((r) => r.id)).toEqual([1, 3, 2]);
  });

  it("does not mutate the input", () => {
    const before = rows.map((r) => r.id);
    sortRows(rows, "name", "desc", CONFIG);
    expect(rows.map((r) => r.id)).toEqual(before);
  });
});

describe("paginate", () => {
  it("clamps an out-of-range page to the last valid page (the delete path)", () => {
    const p = paginate([1, 2, 3], 9, 2);
    expect(p.page).toBe(2);
    expect(p.slice).toEqual([3]);
    expect(p).toMatchObject({ totalPages: 2, start: 3, end: 3, total: 3 });
  });

  it("reports a zero range for an empty set on page 1", () => {
    expect(paginate([], 1, 25)).toMatchObject({ page: 1, totalPages: 1, start: 0, end: 0, total: 0 });
  });
});

describe("rangeLabel", () => {
  it("uses the plural noun for a multi-row span with an en dash", () => {
    expect(rangeLabel({ start: 26, end: 50, total: 83 }, CONFIG.nouns)).toBe("Showing 26–50 of 83 requests");
  });

  it("uses the singular noun for exactly one row", () => {
    expect(rangeLabel({ start: 1, end: 1, total: 1 }, CONFIG.nouns)).toBe("Showing 1 of 1 request");
  });

  it("says 'No <many>' when empty", () => {
    expect(rangeLabel({ start: 0, end: 0, total: 0 }, { one: "blocked user", many: "blocked users" }))
      .toBe("No blocked users");
  });
});

describe("viewRows", () => {
  it("composes filter -> sort -> paginate and labels the page", () => {
    const v = viewRows(rows, { query: "", sortKey: "when", sortDir: "desc", page: 1, perPage: 2 }, CONFIG);
    expect(v.slice.map((r) => r.id)).toEqual([1, 2]); // when desc: 300, 200
    expect(v).toMatchObject({ page: 1, totalPages: 2, total: 3 });
    expect(v.label).toBe("Showing 1–2 of 3 requests");
  });

  it("labels a filtered miss as 'No <many>'", () => {
    const v = viewRows(rows, { query: "zzz", sortKey: "name", sortDir: "asc", page: 1, perPage: 25 }, CONFIG);
    expect(v.slice).toEqual([]);
    expect(v.label).toBe("No requests");
  });
});
