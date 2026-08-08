import { describe, expect, test } from "vitest";

import { loadNotice } from "./loadstate.js";

// Regression coverage for the two-sided error/refresh rule described in
// Logs.jsx and Allowlist.jsx: a load failure must always be VISIBLE, but a
// populated table must never be silently REPLACED by a transient poll
// failure (that would blow away the admin's search/sort/page/selection for
// no reason). loadNotice() is the pure decision the two components share —
// see CLAUDE.md's [[loadstate]] contract:
//   loadNotice({ error, hasRows }) -> null | { text, replace: boolean }

describe("loadNotice", () => {
  test("no error -> no notice, whether or not rows are present "
    + "(a healthy load must never show a stale-data banner)", () => {
    expect(loadNotice({ error: "", hasRows: true })).toBeNull();
    expect(loadNotice({ error: "", hasRows: false })).toBeNull();
    expect(loadNotice({ error: null, hasRows: false })).toBeNull();
    expect(loadNotice({ error: undefined, hasRows: true })).toBeNull();
  });

  test("REGRESSION [Allowlist.jsx unhandled rejection / empty-table lie]: "
    + "an error on a table with NO rows on screen replaces the panel — an "
    + "empty table read as fact ('nobody is blocked', 'nobody can sign in') "
    + "is the dangerous lie a first load must never tell", () => {
    const notice = loadNotice({ error: "Couldn't load users.", hasRows: false });
    expect(notice).not.toBeNull();
    expect(notice.replace).toBe(true);
    // The panel is being REPLACED by this text, so it must actually say
    // something — an empty/blank replacement is just a different silent lie.
    expect(notice.text.length).toBeGreaterThan(0);
  });

  test("REGRESSION [Logs.jsx auto-refresh]: an error on a table that already "
    + "HAS rows on screen must NOT replace them — the 4s auto-refresh poll "
    + "must not blow away the admin's already-loaded rows on a transient "
    + "failure", () => {
    const notice = loadNotice({ error: "Couldn't load the logs.", hasRows: true });
    expect(notice).not.toBeNull();
    expect(notice.replace).toBe(false);
  });

  test("REGRESSION: the populated-table notice says the rows may be stale — "
    + "this is the whole point of the branch (Logs.jsx today sets `err` on a "
    + "failed refresh and renders it NOWHERE because the gate is "
    + "records.length === 0, so an admin watching a live problem sees nothing "
    + "change and concludes the server is fine)", () => {
    const notice = loadNotice({ error: "Couldn't load the logs.", hasRows: true });
    expect(notice.text).toMatch(/stale/i);
  });

  test("the underlying error detail is carried into the notice text, not "
    + "discarded, on BOTH branches — an admin trying to diagnose a locked "
    + "logs.db needs the server's own sentence, not a generic apology", () => {
    const replaced = loadNotice({ error: "logs.db is locked", hasRows: false });
    const stale = loadNotice({ error: "logs.db is locked", hasRows: true });
    expect(replaced.text).toContain("logs.db is locked");
    expect(stale.text).toContain("logs.db is locked");
  });
});
