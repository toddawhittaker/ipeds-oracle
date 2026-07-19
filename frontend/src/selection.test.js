import { describe, it, expect } from "vitest";
import {
  pageHeaderState,
  selectionCount,
  partitionEligibility,
  selectedCountLabel,
  pageSelectedNotice,
  selectAllMatchingLabel,
  allMatchingLabel,
  reducedMatchingLabel,
  bulkConfirmSummary,
  bulkResultToast,
  retainedSelectionAfterBulk,
} from "./selection.js";

// Pure selection-model logic for the admin bulk-actions feature (bulk
// row-selection across the Allowlist users / Pending requests / Blocked users
// tables). The stateful React side (useTableSelection, DataTable's checkbox
// column, BulkBar) is Playwright-covered (frontend/e2e/admin-bulk-actions.spec.js)
// -- everything here is deterministic input->output string/counting logic, so it
// belongs in vitest per CLAUDE.md's test pyramid.
//
// NUMBER RENDERING (pinned convention, per the architect's plan + PM decision):
// cardinal numbers 0-9 are spelled out ("Five", "one") -- capitalized only when
// they are the FIRST word of the string/sentence -- and 10+ render as plain
// digits ("12", "142"). This applies uniformly across every string-producing
// function below (not just the prose summary/toast functions): every pinned
// example in the architect's contract that happens to use a value >=10 renders
// a digit ("18 users...", "142 of 143..."), and every value <10 is spelled
// ("Five users...", "One user..."), with no function-specific exception carved
// out anywhere in the contract.

const USER_NOUNS = { one: "user", many: "users" };
const PENDING_NOUNS = { one: "request", many: "requests" };
// bulkConfirmSummary/bulkResultToast hardcode their own nouns per action (they
// don't take a `nouns` param -- see the architect's contract), so there's no
// BLOCKED_NOUNS constant needed here; "blocked user(s)" appears as a literal
// in those tests' expected strings instead.

describe("pageHeaderState", () => {
  // The tri-state master checkbox atop each table's page: none checked, every
  // eligible row on the page checked, or SOME (the indeterminate state -- the
  // one a naive `every`/`some` implementation is likeliest to get backwards).
  it("an empty page (no eligible rows, e.g. every row is disabled/self) is 'none'", () => {
    expect(pageHeaderState([], new Set())).toBe("none");
  });
  it("no eligible row selected is 'none'", () => {
    expect(pageHeaderState(["a", "b", "c"], new Set())).toBe("none");
  });
  it("every eligible row selected is 'all'", () => {
    expect(pageHeaderState(["a", "b", "c"], new Set(["a", "b", "c"]))).toBe("all");
  });
  it("a proper subset selected is 'some' (drives the indeterminate checkbox)", () => {
    expect(pageHeaderState(["a", "b", "c"], new Set(["a"]))).toBe("some");
  });
  it("a selection that includes ids NOT on this page still reads 'all' for THIS page "
    + "(extra ids outside pageEligibleIds must not count against it)", () => {
    expect(pageHeaderState(["a", "b"], new Set(["a", "b", "z"]))).toBe("all");
  });
});

describe("selectionCount", () => {
  // explicit mode: count = |selectedIds ∩ filteredEligibleIds| -- a stale
  // selectedId whose row scrolled out of the current filter/search must NOT
  // inflate the visible count (regression: a search narrowing the table would
  // otherwise show a count for rows the admin can no longer even see).
  it("explicit mode counts only the intersection with the filtered/eligible set", () => {
    const selection = { mode: "explicit", selectedIds: new Set(["a", "b", "z"]) };
    expect(selectionCount(selection, new Set(["a", "b", "c"]))).toBe(2);
  });
  it("explicit mode with nothing selected is 0", () => {
    const selection = { mode: "explicit", selectedIds: new Set() };
    expect(selectionCount(selection, new Set(["a", "b", "c"]))).toBe(0);
  });
  // all-matching mode: selectedIds holds the EXCLUDED ids; effective count =
  // filtered - |excluded ∩ filtered|.
  it("all-matching mode subtracts only excluded ids that are actually in the filtered set", () => {
    const selection = { mode: "all", selectedIds: new Set(["c"]) };
    expect(selectionCount(selection, new Set(["a", "b", "c", "d"]))).toBe(3);
  });
  it("all-matching mode: an excluded id no longer in the filtered set doesn't "
    + "double-subtract (regression: a naive `total - excluded.size` undercounts)", () => {
    const selection = { mode: "all", selectedIds: new Set(["z"]) }; // z isn't in filtered
    expect(selectionCount(selection, new Set(["a", "b", "c"]))).toBe(3);
  });
  it("all-matching mode with nothing excluded counts every filtered id", () => {
    const selection = { mode: "all", selectedIds: new Set() };
    expect(selectionCount(selection, new Set(["a", "b", "c"]))).toBe(3);
  });
});

describe("partitionEligibility", () => {
  const rows = [
    { email: "admin1@x.edu", is_admin: 1 },
    { email: "user1@x.edu", is_admin: 0 },
    { email: "user2@x.edu", is_admin: 0 },
  ];

  it("promote: skips an already-admin row with the exact reason string", () => {
    const { eligible, skipped } = partitionEligibility(rows, "promote");
    expect(eligible).toEqual([rows[1], rows[2]]);
    expect(skipped).toEqual([{ row: rows[0], reason: "already an administrator" }]);
  });

  it("demote: skips non-admin rows with the exact reason string", () => {
    const { eligible, skipped } = partitionEligibility(rows, "demote");
    expect(eligible).toEqual([rows[0]]);
    expect(skipped).toEqual([
      { row: rows[1], reason: "not an administrator" },
      { row: rows[2], reason: "not an administrator" },
    ]);
  });

  it("delete: skips an admin row, preserving the demote-first invariant's reason string", () => {
    const { eligible, skipped } = partitionEligibility(rows, "delete");
    expect(eligible).toEqual([rows[1], rows[2]]);
    expect(skipped).toEqual([{ row: rows[0], reason: "is an administrator — demote first" }]);
  });

  // approve/reject/unblock: the server is the ONLY authority on eligibility for
  // these three (the client has no live view of allowlist/denial state at
  // click time), so the client-side partition never invents a skip here --
  // regression: a client-side guess here would show a confusing, possibly
  // wrong, "preview" count that the server's real result then contradicts.
  for (const action of ["approve", "reject", "unblock"]) {
    it(`${action}: every selected row is reported eligible, nothing pre-skipped client-side`, () => {
      const reqRows = [{ id: 1 }, { id: 2 }, { id: 3 }];
      const { eligible, skipped } = partitionEligibility(reqRows, action);
      expect(eligible).toEqual(reqRows);
      expect(skipped).toEqual([]);
    });
  }
});

describe("selectedCountLabel", () => {
  it("18 users selected (pinned architect example, no trailing period)", () => {
    expect(selectedCountLabel(18, USER_NOUNS)).toBe("18 users selected");
  });
  it("singular boundary: count===1 uses the singular noun and a spelled-out number", () => {
    expect(selectedCountLabel(1, USER_NOUNS)).toBe("One user selected");
  });
  it("a mid-single-digit count is spelled out, plural noun", () => {
    expect(selectedCountLabel(5, PENDING_NOUNS)).toBe("Five requests selected");
  });
  it("does not append a period (this is a label, not a sentence)", () => {
    expect(selectedCountLabel(18, USER_NOUNS).endsWith(".")).toBe(false);
  });
});

describe("pageSelectedNotice", () => {
  it("25 requests on this page are selected. (pinned architect example)", () => {
    expect(pageSelectedNotice(25, PENDING_NOUNS)).toBe("25 requests on this page are selected.");
  });
  it("singular boundary: count===1 -> 'is', singular noun, spelled number", () => {
    expect(pageSelectedNotice(1, PENDING_NOUNS)).toBe("One request on this page is selected.");
  });
});

describe("selectAllMatchingLabel", () => {
  it("Select all 143 matching users (pinned architect example, no trailing period)", () => {
    expect(selectAllMatchingLabel(143, USER_NOUNS)).toBe("Select all 143 matching users");
  });
  it("a small count is spelled out, lowercase (not sentence-initial -- 'Select' is)", () => {
    expect(selectAllMatchingLabel(5, USER_NOUNS)).toBe("Select all five matching users");
  });
  it("singular boundary: count===1 uses the singular noun", () => {
    expect(selectAllMatchingLabel(1, USER_NOUNS)).toBe("Select all one matching user");
  });
  it("does not append a period (this is a button/link label, not a sentence)", () => {
    expect(selectAllMatchingLabel(143, USER_NOUNS).endsWith(".")).toBe(false);
  });
});

describe("allMatchingLabel", () => {
  it("All 143 matching users are selected. (pinned architect example)", () => {
    expect(allMatchingLabel(143, USER_NOUNS)).toBe("All 143 matching users are selected.");
  });
  it("singular boundary: count===1 -> 'is', singular noun, spelled lowercase number", () => {
    expect(allMatchingLabel(1, USER_NOUNS)).toBe("All one matching user is selected.");
  });
  it("a mid-single-digit count is spelled out lowercase", () => {
    expect(allMatchingLabel(5, PENDING_NOUNS)).toBe("All five matching requests are selected.");
  });
});

describe("reducedMatchingLabel", () => {
  it("142 of 143 matching users are selected. (pinned architect example; "
    + "the verb keys on the SELECTED count, not the total)", () => {
    expect(reducedMatchingLabel(142, 143, USER_NOUNS)).toBe(
      "142 of 143 matching users are selected.");
  });
  it("singular boundary on the SELECTED count -> 'is', spelled+capitalized "
    + "(sentence-initial), while the mid-sentence total is spelled lowercase", () => {
    expect(reducedMatchingLabel(1, 5, USER_NOUNS)).toBe(
      "One of five matching users is selected.");
  });
  it("noun plurality agrees with the TOTAL (the universe being described), "
    + "not the selected count", () => {
    expect(reducedMatchingLabel(3, 10, PENDING_NOUNS)).toBe(
      "Three of 10 matching requests are selected.");
  });
});

describe("bulkConfirmSummary", () => {
  // THE canonical pinned example (verbatim from the architect's contract) --
  // authoritative for casing, spell-out, and pluralization across every other
  // action below, derived by the identical rule.
  it("promote: pinned canonical example (18 selected, 13 eligible, 5 skipped)", () => {
    expect(bulkConfirmSummary("promote", { selected: 18, eligible: 13, skipped: 5 })).toBe(
      "18 users are selected. 13 regular users will be promoted. "
      + "Five users are already administrators and will not be changed.");
  });
  it("promote: the skip sentence is OMITTED entirely when skipped===0 "
    + "(not rendered as 'Zero users are already administrators...')", () => {
    expect(bulkConfirmSummary("promote", { selected: 4, eligible: 4, skipped: 0 })).toBe(
      "Four users are selected. Four regular users will be promoted.");
  });
  it("promote: singular boundary on every count (1 user, singular noun, 'is'/'is')", () => {
    expect(bulkConfirmSummary("promote", { selected: 1, eligible: 1, skipped: 0 })).toBe(
      "One user is selected. One regular user will be promoted.");
  });

  it("demote: typical batch (derived by the same rule as the pinned promote example)", () => {
    expect(bulkConfirmSummary("demote", { selected: 6, eligible: 4, skipped: 2 })).toBe(
      "Six users are selected. Four administrators will be demoted. "
      + "Two users are not administrators and will not be changed.");
  });
  it("demote: singular skip ('is not an administrator', not the plural noun)", () => {
    expect(bulkConfirmSummary("demote", { selected: 5, eligible: 3, skipped: 1 })).toBe(
      "Five users are selected. Three administrators will be demoted. "
      + "One user is not an administrator and will not be changed.");
  });

  it("delete: typical batch -- the skip sentence names the demote-first invariant", () => {
    expect(bulkConfirmSummary("delete", { selected: 9, eligible: 6, skipped: 3 })).toBe(
      "Nine users are selected. Six users will be removed from the allowlist. "
      + "Three still hold admin and must be demoted first.");
  });
  it("delete: singular skip uses 'holds' (verb agreement), not 'hold'", () => {
    expect(bulkConfirmSummary("delete", { selected: 4, eligible: 3, skipped: 1 })).toBe(
      "Four users are selected. Three users will be removed from the allowlist. "
      + "One still holds admin and must be demoted first.");
  });

  it("approve: never has a skip sentence (partitionEligibility never skips this action)", () => {
    expect(bulkConfirmSummary("approve", { selected: 15, eligible: 15, skipped: 0 })).toBe(
      "15 requests are selected. 15 requests will be approved and emailed a sign-in link.");
  });
  it("approve: singular boundary", () => {
    expect(bulkConfirmSummary("approve", { selected: 1, eligible: 1, skipped: 0 })).toBe(
      "One request is selected. One request will be approved and emailed a sign-in link.");
  });

  it("reject: names the block explicitly (both counts <10, so both spelled out)", () => {
    expect(bulkConfirmSummary("reject", { selected: 8, eligible: 8, skipped: 0 })).toBe(
      "Eight requests are selected. Eight requests will be rejected and "
      + "blocked from requesting again.");
  });
  it("reject: singular boundary", () => {
    expect(bulkConfirmSummary("reject", { selected: 1, eligible: 1, skipped: 0 })).toBe(
      "One request is selected. One request will be rejected and blocked from requesting again.");
  });

  it("unblock: the eligible sentence has NO repeated noun ('will be allowed', not "
    + "'users will be allowed') and states the no-access/no-email guarantee", () => {
    expect(bulkConfirmSummary("unblock", { selected: 10, eligible: 10, skipped: 0 })).toBe(
      "10 blocked users are selected. 10 will be allowed to request access again "
      + "— this grants no access and sends no email.");
  });
  it("unblock: singular boundary uses 'blocked user' (singular)", () => {
    expect(bulkConfirmSummary("unblock", { selected: 1, eligible: 1, skipped: 0 })).toBe(
      "One blocked user is selected. One will be allowed to request access again "
      + "— this grants no access and sends no email.");
  });
});

describe("bulkResultToast", () => {
  it("kind='error' whenever ANY item failed, even if others succeeded "
    + "(failed always wins over a nonzero affected count)", () => {
    const { kind } = bulkResultToast("promote",
      { ok: true, affected: 10, skipped: [], failed: [{ email: "x@x.edu", reason: "db error" }] });
    expect(kind).toBe("error");
  });
  it("kind='ok' when something was affected and nothing failed", () => {
    const { kind } = bulkResultToast("promote", { ok: true, affected: 1, skipped: [], failed: [] });
    expect(kind).toBe("ok");
  });
  it("kind='' (neutral) when NOTHING was affected and nothing failed "
    + "(e.g. every selected row was already ineligible) -- must not read as an error", () => {
    const { kind } = bulkResultToast("promote", { ok: true, affected: 0, skipped: [{}, {}], failed: [] });
    expect(kind).toBe("");
  });

  it("promote: pinned canonical example -- 12 promoted, 5 skipped, spelled+capitalized", () => {
    const skipped = Array.from({ length: 5 }, (_, i) => ({ email: `a${i}@x.edu`, reason: "already an administrator" }));
    const { text } = bulkResultToast("promote", { ok: true, affected: 12, skipped, failed: [] });
    expect(text).toBe("12 users promoted. Five were already administrators and were skipped.");
  });
  it("promote: 0-affected/all-skipped is still a coherent, neutral-kind message", () => {
    const skipped = [{ email: "a@x.edu", reason: "already an administrator" },
      { email: "b@x.edu", reason: "already an administrator" },
      { email: "c@x.edu", reason: "already an administrator" }];
    const { text, kind } = bulkResultToast("promote", { ok: true, affected: 0, skipped, failed: [] });
    expect(text).toBe("Zero users promoted. Three were already administrators and were skipped.");
    expect(kind).toBe("");
  });
  it("promote: partial failure appends the fail clause with a spelled singular count", () => {
    const { text, kind } = bulkResultToast("promote",
      { ok: true, affected: 10, skipped: [], failed: [{ email: "z@x.edu", reason: "db error" }] });
    expect(text).toBe("10 users promoted. One could not be promoted and is still selected.");
    expect(kind).toBe("error");
  });
  it("promote: skip clause singular ('was'/'an administrator', not the plural forms)", () => {
    const { text } = bulkResultToast("promote",
      { ok: true, affected: 4, skipped: [{ email: "a@x.edu", reason: "already an administrator" }], failed: [] });
    expect(text).toBe("Four users promoted. One was already an administrator and was skipped.");
  });

  it("demote: base + skip clause ('not administrators', mirroring the confirm-summary reason)", () => {
    const skipped = [{ email: "a@x.edu", reason: "not an administrator" },
      { email: "b@x.edu", reason: "not an administrator" },
      { email: "c@x.edu", reason: "not an administrator" }];
    const { text } = bulkResultToast("demote", { ok: true, affected: 7, skipped, failed: [] });
    expect(text).toBe("Seven administrators demoted. Three were not administrators and were skipped.");
  });
  it("demote: skip + fail clauses can BOTH be present in one toast", () => {
    const { text, kind } = bulkResultToast("demote", {
      ok: true, affected: 4,
      skipped: [{ email: "a@x.edu", reason: "not an administrator" },
        { email: "b@x.edu", reason: "not an administrator" }],
      failed: [{ email: "z@x.edu", reason: "db error" }],
    });
    expect(text).toBe(
      "Four administrators demoted. Two were not administrators and were skipped. "
      + "One could not be demoted and is still selected.");
    expect(kind).toBe("error");
  });

  it("delete: base + skip clause ('still hold admin', mirroring the demote-first invariant)", () => {
    const skipped = [{ email: "a@x.edu", reason: "is an administrator — demote first" },
      { email: "b@x.edu", reason: "is an administrator — demote first" }];
    const { text } = bulkResultToast("delete", { ok: true, affected: 5, skipped, failed: [] });
    expect(text).toBe("Five users removed from the allowlist. Two still hold admin and were skipped.");
  });
  it("delete: fail clause names the removal verb", () => {
    const { text } = bulkResultToast("delete",
      { ok: true, affected: 3, skipped: [], failed: [{ email: "z@x.edu", reason: "db error" }] });
    expect(text).toBe("Three users removed from the allowlist. One could not be removed and is still selected.");
  });

  it("approve: base + skip clause for a genuine SERVER-side skip (already allowlisted by "
    + "the time the request landed -- the client-side preview never predicts this)", () => {
    const skipped = [{ id: 1, reason: "already allowlisted" }, { id: 2, reason: "already allowlisted" }];
    const { text } = bulkResultToast("approve", { ok: true, affected: 9, skipped, failed: [] });
    expect(text).toBe("Nine requests approved. Two were already allowlisted and were skipped.");
  });
  it("approve: fail clause", () => {
    const { text } = bulkResultToast("approve",
      { ok: true, affected: 2, skipped: [], failed: [{ id: 9, reason: "db error" }] });
    expect(text).toBe("Two requests approved. One could not be approved and is still selected.");
  });

  it("reject: base + skip clause for an already-resolved id", () => {
    const { text } = bulkResultToast("reject",
      { ok: true, affected: 6, skipped: [{ id: 1, reason: "already resolved" }], failed: [] });
    expect(text).toBe("Six requests rejected and blocked. One was already resolved and was skipped.");
  });
  it("reject: fail clause", () => {
    const { text } = bulkResultToast("reject",
      { ok: true, affected: 1, skipped: [], failed: [{ id: 2, reason: "db error" }] });
    expect(text).toBe("One request rejected and blocked. One could not be rejected and is still selected.");
  });

  it("unblock: base + skip clause for a not-blocked id", () => {
    const { text } = bulkResultToast("unblock",
      { ok: true, affected: 3, skipped: [{ id: 1, reason: "not blocked" }], failed: [] });
    expect(text).toBe(
      "Three blocked users allowed to request access again. One was not blocked and was skipped.");
  });
  it("unblock: fail clause", () => {
    const { text } = bulkResultToast("unblock",
      { ok: true, affected: 1, skipped: [], failed: [{ id: 2, reason: "db error" }] });
    expect(text).toBe(
      "One blocked user allowed to request access again. "
      + "One could not be unblocked and is still selected.");
  });

  // M2 (code review #3): before this fix, the toast rendered exactly ONE
  // hardcoded-per-ACTION clause against `skipped.length`, so a batch whose
  // skips came from more than one reason silently misattributed every skip to
  // whichever single reason that action's clause happened to name -- e.g. a
  // promote batch with both already-admin AND no-longer-on-the-allowlist
  // skips would have told the admin ALL of them were "already
  // administrators", which is simply false for the second group. Each case
  // below pins an exact mixed-reason string the implementer's grouping
  // (skipClauses) actually produces, so a regression back to the old
  // single-clause behavior fails a byte-for-byte comparison, not a vague
  // "contains" check.
  describe("mixed-reason skip grouping (M2 regression coverage)", () => {
    it("promote: 3x already-an-administrator + 2x not-on-the-allowlist renders "
      + "ONE clause per reason, each with its own count (pinned implementer output)", () => {
      const skipped = [
        ...Array.from({ length: 3 }, (_, i) => ({ email: `admin${i}@x.edu`, reason: "already an administrator" })),
        ...Array.from({ length: 2 }, (_, i) => ({ email: `gone${i}@x.edu`, reason: "not on the allowlist" })),
      ];
      const { text } = bulkResultToast("promote", { ok: true, affected: 10, skipped, failed: [] });
      expect(text).toBe(
        "10 users promoted. Three were already administrators and were skipped. "
        + "Two were not on the allowlist and were skipped.");
    });

    it("approve: 2x already-allowlisted + 1x not-found renders two separate "
      + "clauses (pinned implementer output)", () => {
      const skipped = [
        { id: 1, reason: "already allowlisted" }, { id: 2, reason: "already allowlisted" },
        { id: 3, reason: "not found" },
      ];
      const { text } = bulkResultToast("approve", { ok: true, affected: 5, skipped, failed: [] });
      expect(text).toBe(
        "Five requests approved. Two were already allowlisted and were skipped. "
        + "One was not found and was skipped.");
    });

    it("reject: a mixed already-resolved + not-found set (derived by the same "
      + "rule) also splits into two per-reason clauses", () => {
      const skipped = [
        { id: 1, reason: "already resolved" },
        { id: 2, reason: "not found" }, { id: 3, reason: "not found" },
      ];
      const { text } = bulkResultToast("reject", { ok: true, affected: 4, skipped, failed: [] });
      expect(text).toBe(
        "Four requests rejected and blocked. One was already resolved and was skipped. "
        + "Two were not found and were skipped.");
    });

    it("clauses are ordered by FIRST APPEARANCE of each reason in `skipped`, "
      + "not alphabetically or by count -- a reason seen second-but-more-often "
      + "still renders second", () => {
      // "not on the allowlist" appears first (id 1) even though "already
      // allowlisted" (2 occurrences) outnumbers it (1 occurrence) -- if the
      // grouping sorted by count or alphabetically instead of first-seen
      // order, this would render in the opposite sequence.
      const skipped = [
        { id: 1, reason: "not on the allowlist" },
        { id: 2, reason: "already allowlisted" },
        { id: 3, reason: "already allowlisted" },
      ];
      const { text } = bulkResultToast("approve", { ok: true, affected: 6, skipped, failed: [] });
      expect(text).toBe(
        "Six requests approved. One was not on the allowlist and was skipped. "
        + "Two were already allowlisted and were skipped.");
    });

    it("a mixed-reason skip set alongside a `failed` entry still yields "
      + "kind='error' (failed always wins the toast's kind, regardless of how "
      + "many distinct skip reasons are also present)", () => {
      const skipped = [
        { email: "a@x.edu", reason: "already an administrator" },
        { email: "b@x.edu", reason: "not on the allowlist" },
      ];
      const { kind } = bulkResultToast("promote", {
        ok: true, affected: 3, skipped,
        failed: [{ email: "z@x.edu", reason: "db error" }],
      });
      expect(kind).toBe("error");
    });

    // Regression pin: a naive single-clause implementation (the pre-M2 code)
    // would produce exactly one sentence covering ALL skips -- e.g. "Five
    // were already administrators and were skipped." for a mixed 3+2 set,
    // which is simply wrong for the 2 that weren't already admins. Assert
    // the two-reason case does NOT collapse to that single (wrong) clause.
    it("REGRESSION: a two-reason skip set does not collapse to a single, "
      + "one-reason-attributed clause", () => {
      const skipped = [
        { email: "a@x.edu", reason: "already an administrator" },
        { email: "b@x.edu", reason: "already an administrator" },
        { email: "c@x.edu", reason: "already an administrator" },
        { email: "d@x.edu", reason: "not on the allowlist" },
        { email: "e@x.edu", reason: "not on the allowlist" },
      ];
      const { text } = bulkResultToast("promote", { ok: true, affected: 10, skipped, failed: [] });
      const wrongSingleClause =
        "10 users promoted. Five were already administrators and were skipped.";
      expect(text).not.toBe(wrongSingleClause);
      expect(text).toBe(
        "10 users promoted. Three were already administrators and were skipped. "
        + "Two were not on the allowlist and were skipped.");
    });
  });
});

// "Keep the whole selection" after a bulk action commits. The regression this
// guards: a bulk action must NOT clear the checkboxes of rows that are still in
// the table. promote/demote leave every row in place -> the entire selection is
// retained; delete/approve/reject/unblock remove the rows they process ->
// only those ids drop, while skipped/failed rows (still in the table) stay
// checked.
describe("retainedSelectionAfterBulk", () => {
  const R = (skipped = [], failed = []) => ({ ok: true, affected: 0, skipped, failed });

  it("promote/demote (in-place) keep the ENTIRE selection, order preserved", () => {
    const ids = ["c@x.edu", "a@x.edu", "b@x.edu"];
    // Even with skips + failures, every id stays -- the rows are all still there.
    const result = R([{ email: "a@x.edu", reason: "already an administrator" }],
      [{ email: "b@x.edu", reason: "could not be updated" }]);
    expect(retainedSelectionAfterBulk("promote", ids, result, "email")).toEqual(ids);
    expect(retainedSelectionAfterBulk("demote", ids, result, "email")).toEqual(ids);
  });

  it("delete (removing) drops the fully-processed ids, keeps skipped + failed", () => {
    // 4 selected: two removed, one skipped (still admin), one failed.
    const ids = ["gone1@x.edu", "gone2@x.edu", "skip@x.edu", "fail@x.edu"];
    const result = R([{ email: "skip@x.edu", reason: "is an administrator — demote first" }],
      [{ email: "fail@x.edu", reason: "could not be updated" }]);
    expect(retainedSelectionAfterBulk("delete", ids, result, "email"))
      .toEqual(["skip@x.edu", "fail@x.edu"]);
  });

  it("delete with nothing skipped/failed clears to empty (every row removed)", () => {
    const ids = ["a@x.edu", "b@x.edu"];
    expect(retainedSelectionAfterBulk("delete", ids, R(), "email")).toEqual([]);
  });

  it("approve/reject/unblock (removing) key skipped/failed by the `id` field", () => {
    const ids = [1, 2, 3, 4];
    const result = R([{ id: 2, reason: "already resolved" }],
      [{ id: 4, reason: "could not be updated" }]);
    for (const action of ["approve", "reject", "unblock"]) {
      expect(retainedSelectionAfterBulk(action, ids, result, "id")).toEqual([2, 4]);
    }
  });

  it("tolerates a result with no skipped/failed keys at all", () => {
    // A removing action whose response omits the arrays entirely -> empty kept set.
    expect(retainedSelectionAfterBulk("approve", [1, 2], { ok: true, affected: 2 }, "id"))
      .toEqual([]);
    // An in-place action ignores them regardless.
    expect(retainedSelectionAfterBulk("promote", ["a@x.edu"], { ok: true }, "email"))
      .toEqual(["a@x.edu"]);
  });

  it("returns a fresh array, not the caller's `selectedRowIds` reference", () => {
    const ids = ["a@x.edu"];
    const out = retainedSelectionAfterBulk("promote", ids, R(), "email");
    expect(out).toEqual(ids);
    expect(out).not.toBe(ids); // spread copy -- caller's array is never mutated
  });
});
