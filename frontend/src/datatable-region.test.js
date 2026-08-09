import { describe, expect, it } from "vitest";

import { needsScrollRegion } from "./datatable-region.js";

// This predicate exists because the same wrapper was copied onto a table with
// no focusable cells and shipped a scroll region no keyboard user could reach
// (Usage.jsx's Top users). It is tested HERE rather than through the browser
// because no current DataTable consumer trips the true branch — an e2e
// assertion that today's tables get no region is satisfied just as well by
// deleting the derivation, which is exactly what a mutation showed.

const sortable = (n) => Array.from({ length: n }, (_, i) => ({ key: `c${i}`, sortable: true }));

describe("needsScrollRegion", () => {
  it("is false only when BOTH extremes are already keyboard-reachable", () => {
    // Action buttons at the right edge, a sort button in every header: focusing
    // any of them scrolls that column into view, so a region would be a
    // redundant tab stop announcing the table's name twice. This is the shape
    // all three Allowlist tables have.
    expect(needsScrollRegion(true, sortable(4))).toBe(false);
  });

  it("is true with no actions column — nothing focusable at the right edge", () => {
    expect(needsScrollRegion(false, sortable(4))).toBe(true);
  });

  it("is true when ANY column is not sortable — that header cannot be reached", () => {
    const cols = [...sortable(3), { key: "note" }];
    expect(needsScrollRegion(true, cols)).toBe(true);
    // ...including when the unreachable one is first, so this is not an
    // accident of iteration order.
    expect(needsScrollRegion(true, [{ key: "note" }, ...sortable(3)])).toBe(true);
  });

  it("is true for a table with no columns at all", () => {
    // Degenerate, but the safe direction: an empty/missing column list means
    // nothing is known to be reachable, so make the wrapper focusable.
    expect(needsScrollRegion(false, [])).toBe(true);
    expect(needsScrollRegion(true, [])).toBe(false);
    expect(needsScrollRegion(true, undefined)).toBe(false);
  });

  it("treats a null column entry as unreachable rather than throwing", () => {
    expect(needsScrollRegion(true, [null, ...sortable(2)])).toBe(true);
  });
});
