import { describe, it, expect } from "vitest";
import { filterRows, sortRows, viewRows } from "./datatable.js";
import { KEY_CONFIG, KEY_PREFIX, isRevoked, maskedKey, sortByNewest } from "./apikeys.js";

// The pure display + table logic behind both API-key screens. Browser truth (the
// one-shot reveal dialog, the revoke confirmation, focus) lives in
// frontend/e2e/keys.spec.js and admin-keys.spec.js; this owns what the pipeline
// computes and what a row is allowed to show.

const K = (id, opts = {}) => ({
  id,
  last4: opts.last4 ?? "abcd",
  label: opts.label ?? null,
  email: opts.email ?? null,
  created_at: opts.created ?? 1_700_000_000,
  created_by: opts.createdBy ?? null,
  last_used_at: opts.used ?? null,
  revoked_at: opts.revoked ?? null,
});

describe("maskedKey", () => {
  // The regression: any change here that widens the visible tail turns an
  // identification aid into a partial credential leak on a page a user may well
  // screen-share. The server only ever sends four characters — assert the render
  // shows those four and nothing else.
  it("shows the prefix and exactly the last four characters", () => {
    expect(maskedKey(K(1, { last4: "9f2a" }))).toBe(`${KEY_PREFIX}…9f2a`);
  });
  it("never renders a secret even if one somehow rides along on the row", () => {
    const row = { ...K(1, { last4: "9f2a" }), key: "ipeds_mcp_totally-secret-value" };
    expect(maskedKey(row)).not.toContain("totally-secret-value");
    expect(maskedKey(row)).toBe(`${KEY_PREFIX}…9f2a`);
  });
  it("degrades to the bare prefix rather than 'undefined' on a missing last4", () => {
    expect(maskedKey({ id: 1 })).toBe(`${KEY_PREFIX}…`);
    expect(maskedKey(undefined)).toBe(`${KEY_PREFIX}…`);
  });
});

describe("isRevoked", () => {
  // revoked_at is a unix timestamp, and 0 is a legitimate one. A truthiness test
  // (`!!row.revoked_at`) would report a key revoked at the epoch as active.
  it("is true for any timestamp, including 0", () => {
    expect(isRevoked(K(1, { revoked: 1_700_000_500 }))).toBe(true);
    expect(isRevoked(K(1, { revoked: 0 }))).toBe(true);
  });
  it("is false for a live key and for a missing row", () => {
    expect(isRevoked(K(1))).toBe(false);
    expect(isRevoked(undefined)).toBe(false);
  });
});

describe("the keys table config", () => {
  const rows = [
    K(1, { email: "alice@x.edu", label: "Laptop", last4: "1111" }),
    K(2, { email: "bob@y.edu", label: null, last4: "2222" }),
    K(3, { email: "carol@x.edu", label: "CI runner", last4: "3333" }),
  ];

  it("searches email, label AND last4 — the three things an admin has to hand", () => {
    expect(filterRows(rows, "bob", KEY_CONFIG.fields).map((r) => r.id)).toEqual([2]);
    expect(filterRows(rows, "ci run", KEY_CONFIG.fields).map((r) => r.id)).toEqual([3]);
    // The last4 case is the one that only exists here: a key fragment copied out
    // of a config file or a log is often all anyone has to go on.
    expect(filterRows(rows, "3333", KEY_CONFIG.fields).map((r) => r.id)).toEqual([3]);
  });
  it("treats a null label as empty rather than crashing the whole table", () => {
    expect(filterRows(rows, "laptop", KEY_CONFIG.fields).map((r) => r.id)).toEqual([1]);
    expect(filterRows(rows, "", KEY_CONFIG.fields).length).toBe(3);
  });

  // Never-used keys are the common case, so where they land is not an edge case.
  // The unused rows must group at one end rather than scattering through the
  // table wherever the incoming list happened to put them.
  it("groups never-used keys at one end, both directions", () => {
    const used = [
      K(10, { used: 1_700_000_100 }),
      K(11, { used: null }),
      K(12, { used: 1_700_000_300 }),
      K(13, { used: null }),
    ];
    expect(sortRows(used, "last_used_at", "desc", KEY_CONFIG).map((r) => r.id))
      .toEqual([12, 10, 11, 13]);
    expect(sortRows(used, "last_used_at", "asc", KEY_CONFIG).map((r) => r.id))
      .toEqual([11, 13, 10, 12]);
  });

  // The case the mixed one above CANNOT see, and the one every deployment starts
  // in: no key has been presented yet, so every value is null. The comparator
  // used to map null to -Infinity, and `-Infinity - -Infinity` is NaN —
  // `sortRows` returns the comparator's result the moment it is `!== 0`, and NaN
  // is, so the id tiebreak never ran. Measured on the real modules: ascending
  // and descending both returned [13, 11, 12] for that input, and [11, 12, 13]
  // when the same rows arrived in a different order. An admin clicked "Last
  // used", nothing moved, they clicked again for the other direction, and
  // nothing moved again.
  //
  // The comment above this test used to assert the opposite — that the mapping
  // was what PREVENTED the NaN — and the test passed anyway, because Array.sort
  // is stable and its fixture happened to arrive in id order.
  it("orders an all-unused list by the tiebreak, not by arrival order", () => {
    const ids = (rows, dir) =>
      sortRows(rows, "last_used_at", dir, KEY_CONFIG).map((r) => r.id);
    const fresh = (order) => order.map((id) => K(id, { used: null }));
    expect(ids(fresh([13, 11, 12]), "asc")).toEqual([11, 12, 13]);
    expect(ids(fresh([11, 12, 13]), "asc")).toEqual([11, 12, 13]);
    // Same list, arrived differently, same answer — which is the property the
    // tiebreak exists for and the one NaN destroyed.
    expect(ids(fresh([13, 11, 12]), "desc")).toEqual(ids(fresh([11, 12, 13]), "desc"));
  });

  it("sorts active before revoked ascending, and surfaces revoked descending", () => {
    const mixed = [
      K(20, { revoked: 1_700_000_900 }),
      K(21),
      K(22, { revoked: 1_700_000_800 }),
    ];
    expect(sortRows(mixed, "status", "asc", KEY_CONFIG).map((r) => r.id)).toEqual([21, 20, 22]);
    expect(sortRows(mixed, "status", "desc", KEY_CONFIG).map((r) => r.id)[0]).not.toBe(21);
  });

  it("breaks ties on the row id so a refetch can't reshuffle the page", () => {
    // Three keys minted in the same second — a plausible scripted mint, and the
    // case where an unstable backend order would let a row move under a paging
    // admin between loads.
    const same = [K(31, { created: 5 }), K(30, { created: 5 }), K(32, { created: 5 })];
    expect(sortRows(same, "created_at", "asc", KEY_CONFIG).map((r) => r.id)).toEqual([30, 31, 32]);
    expect(sortRows([...same].reverse(), "created_at", "asc", KEY_CONFIG).map((r) => r.id))
      .toEqual([30, 31, 32]);
  });

  it("falls back to created_at when handed an unknown sort key", () => {
    const byAge = [K(40, { created: 3 }), K(41, { created: 1 }), K(42, { created: 2 })];
    expect(sortRows(byAge, "not_a_column", "asc", KEY_CONFIG).map((r) => r.id))
      .toEqual([41, 42, 40]);
  });

  it("names the rows 'key'/'keys' in the live range label", () => {
    const one = viewRows([K(1)], { query: "", sortKey: "created_at", sortDir: "desc", page: 1, perPage: 25 }, KEY_CONFIG);
    expect(one.label).toBe("Showing 1 of 1 key");
    const none = viewRows([], { query: "", sortKey: "created_at", sortDir: "desc", page: 1, perPage: 25 }, KEY_CONFIG);
    expect(none.label).toBe("No keys");
  });
});

describe("sortByNewest", () => {
  // The user's own page renders a plain list, so this is the only thing putting
  // a key you just minted at the top where you expect to find it.
  it("puts the most recently created key first and does not mutate the input", () => {
    const rows = [K(1, { created: 100 }), K(2, { created: 300 }), K(3, { created: 200 })];
    expect(sortByNewest(rows).map((r) => r.id)).toEqual([2, 3, 1]);
    expect(rows.map((r) => r.id)).toEqual([1, 2, 3]);
  });
});
