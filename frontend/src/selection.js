// Pure selection-model logic for the admin bulk-actions feature (bulk
// row-selection across the Allowlist users / Pending requests / Blocked users
// tables). The stateful React side (useTableSelection.js, DataTable.jsx's
// checkbox column, BulkBar.jsx) is Playwright-covered
// (frontend/e2e/admin-bulk-actions.spec.js); everything here is deterministic
// input->output string/counting logic, so it belongs in vitest per CLAUDE.md's
// test pyramid. The exact input->output behaviour is pinned by selection.test.js.
//
// SELECTION MODEL (see the architect's plan): a selection is
// `{ mode: "explicit" | "all", selectedIds: Set }`.
//   - explicit: selectedIds holds the SELECTED ids.
//   - all:      selectedIds holds the EXCLUDED ids (effective selection =
//               filteredEligibleIds - selectedIds).
//
// NUMBER RENDERING (pinned convention): cardinal numbers 0-9 are spelled out
// ("Five", "one") -- capitalized only when they're the FIRST word of the
// string -- and 10+ render as plain digits ("12", "142"). Applied uniformly by
// every string-producing function below.

const CARDINALS = [
  "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
];

// A cardinal number as a word (0-9) or digits (10+). `capitalize` applies only
// to the spelled-out (<10) form -- a digit string is never "capitalized".
function numWord(n, capitalize = false) {
  if (n >= 0 && n < 10) {
    const w = CARDINALS[n];
    return capitalize ? w[0].toUpperCase() + w.slice(1) : w;
  }
  return String(n);
}

// Subject-verb agreement on a count: singular "is" vs plural "are".
function isAre(n) {
  return n === 1 ? "is" : "are";
}

// was/were agreement on a count (bulkResultToast's skip/fail clauses).
function wasWere(n) {
  return n === 1 ? "was" : "were";
}

// Pick the singular/plural noun for a count from a { one, many } pair.
function nounFor(n, nouns) {
  return n === 1 ? nouns.one : nouns.many;
}

// Tri-state derivation for a page's master checkbox: "none" (nothing on this
// page is selected), "all" (every eligible row on this page is selected -- the
// checked state), or "some" (a proper subset -- drives `indeterminate`).
// `selected` need only reflect THIS page's effective checked ids; extra ids
// outside `pageEligibleIds` (e.g. from another page) don't count against it.
export function pageHeaderState(pageEligibleIds, selected) {
  if (pageEligibleIds.length === 0) return "none";
  const n = pageEligibleIds.filter((id) => selected.has(id)).length;
  if (n === 0) return "none";
  if (n === pageEligibleIds.length) return "all";
  return "some";
}

// The effective selected count against the CURRENT filtered/eligible set --
// never inflated by a stale id whose row has scrolled out of view (a search
// narrowing the table, or a row that's no longer eligible).
export function selectionCount(selection, filteredEligibleIds) {
  let inFiltered = 0;
  for (const id of selection.selectedIds) {
    if (filteredEligibleIds.has(id)) inFiltered += 1;
  }
  if (selection.mode === "all") return filteredEligibleIds.size - inFiltered;
  return inFiltered;
}

// Per-action client-side eligibility preview for the Allowlist users table
// (promote/demote/delete): predicated on `row.is_admin`. approve/reject/
// unblock never pre-skip client-side -- the server is the SOLE authority on
// their eligibility (the client has no live view of allowlist/denial state at
// click time), so every selected row is reported eligible with nothing skipped.
const ELIGIBILITY_RULES = {
  promote: { skipIf: (row) => !!row.is_admin, reason: "already an administrator" },
  demote: { skipIf: (row) => !row.is_admin, reason: "not an administrator" },
  delete: { skipIf: (row) => !!row.is_admin, reason: "is an administrator — demote first" },
};

export function partitionEligibility(selectedRows, action) {
  const rule = ELIGIBILITY_RULES[action];
  if (!rule) return { eligible: [...selectedRows], skipped: [] };
  const eligible = [];
  const skipped = [];
  for (const row of selectedRows) {
    if (rule.skipIf(row)) skipped.push({ row, reason: rule.reason });
    else eligible.push(row);
  }
  return { eligible, skipped };
}

// "18 users selected" / "One user selected" -- a label, not a sentence (no
// trailing period, no verb).
export function selectedCountLabel(count, nouns) {
  return `${numWord(count, true)} ${nounFor(count, nouns)} selected`;
}

// "25 requests on this page are selected." / "One request on this page is
// selected." -- the page-level notice (drives the "select all matching" prompt).
export function pageSelectedNotice(count, nouns) {
  return `${numWord(count, true)} ${nounFor(count, nouns)} on this page ${isAre(count)} selected.`;
}

// "Select all 143 matching users" / "Select all five matching users" -- a
// button/link label (lowercase number, no trailing period; "Select" is the
// only capitalized word since it's the sentence-initial one).
export function selectAllMatchingLabel(count, nouns) {
  return `Select all ${numWord(count, false)} matching ${nounFor(count, nouns)}`;
}

// "All 143 matching users are selected." -- shown once the all-matching
// selection covers the WHOLE filtered/eligible set (nothing excluded).
export function allMatchingLabel(count, nouns) {
  return `All ${numWord(count, false)} matching ${nounFor(count, nouns)} ${isAre(count)} selected.`;
}

// "142 of 143 matching users are selected." -- the all-matching selection MINUS
// one or more excluded rows. The verb agrees with the SELECTED count; noun
// plurality agrees with the TOTAL (the universe being described).
export function reducedMatchingLabel(selected, total, nouns) {
  return `${numWord(selected, true)} of ${numWord(total, false)} matching `
    + `${nounFor(total, nouns)} ${isAre(selected)} selected.`;
}

// The confirmation modal's body sentence: what's selected, what will actually
// happen, and (when non-empty) why some selected rows will be skipped. Each
// action hardcodes its own nouns/verbs (they don't come from a table's {one,
// many} pair -- "regular users"/"administrators"/"requests"/"blocked users"
// read naturally only spelled out per action).
export function bulkConfirmSummary(action, counts) {
  const { selected, eligible, skipped } = counts;
  const USERS = { one: "user", many: "users" };
  const ADMINS = { one: "administrator", many: "administrators" };
  const REQUESTS = { one: "request", many: "requests" };
  const BLOCKED = { one: "blocked user", many: "blocked users" };

  switch (action) {
    case "promote": {
      const parts = [
        `${numWord(selected, true)} ${nounFor(selected, USERS)} ${isAre(selected)} selected.`,
        `${numWord(eligible, true)} regular ${nounFor(eligible, USERS)} will be promoted.`,
      ];
      if (skipped > 0) {
        parts.push(`${numWord(skipped, true)} ${nounFor(skipped, USERS)} ${isAre(skipped)} `
          + `already ${skipped === 1 ? "an administrator" : "administrators"} and will not be changed.`);
      }
      return parts.join(" ");
    }
    case "demote": {
      const parts = [
        `${numWord(selected, true)} ${nounFor(selected, USERS)} ${isAre(selected)} selected.`,
        `${numWord(eligible, true)} ${nounFor(eligible, ADMINS)} will be demoted.`,
      ];
      if (skipped > 0) {
        parts.push(`${numWord(skipped, true)} ${nounFor(skipped, USERS)} ${isAre(skipped)} `
          + `not ${skipped === 1 ? "an administrator" : "administrators"} and will not be changed.`);
      }
      return parts.join(" ");
    }
    case "delete": {
      const parts = [
        `${numWord(selected, true)} ${nounFor(selected, USERS)} ${isAre(selected)} selected.`,
        `${numWord(eligible, true)} ${nounFor(eligible, USERS)} will be removed from the allowlist.`,
      ];
      if (skipped > 0) {
        parts.push(`${numWord(skipped, true)} still ${skipped === 1 ? "holds" : "hold"} `
          + `admin and must be demoted first.`);
      }
      return parts.join(" ");
    }
    case "approve": {
      const parts = [
        `${numWord(selected, true)} ${nounFor(selected, REQUESTS)} ${isAre(selected)} selected.`,
        `${numWord(eligible, true)} ${nounFor(eligible, REQUESTS)} will be approved and emailed a sign-in link.`,
      ];
      return parts.join(" ");
    }
    case "reject": {
      const parts = [
        `${numWord(selected, true)} ${nounFor(selected, REQUESTS)} ${isAre(selected)} selected.`,
        `${numWord(eligible, true)} ${nounFor(eligible, REQUESTS)} will be rejected and blocked from requesting again.`,
      ];
      return parts.join(" ");
    }
    case "unblock": {
      const parts = [
        `${numWord(selected, true)} ${nounFor(selected, BLOCKED)} ${isAre(selected)} selected.`,
        `${numWord(eligible, true)} will be allowed to request access again `
          + `— this grants no access and sends no email.`,
      ];
      return parts.join(" ");
    }
    default:
      return "";
  }
}

// The result toast after a bulk action completes: the base outcome, plus (when
// non-empty) a skip clause and a fail clause, each pluralized/verb-agreed on
// their own count. `failed` always wins the toast's *kind* over a nonzero
// affected count -- a partial failure is still worth flagging as an error even
// though something DID succeed.
function baseClause(action, affected) {
  const n = affected;
  switch (action) {
    case "promote":
      return `${numWord(n, true)} ${nounFor(n, { one: "user", many: "users" })} promoted.`;
    case "demote":
      return `${numWord(n, true)} ${nounFor(n, { one: "administrator", many: "administrators" })} demoted.`;
    case "delete":
      return `${numWord(n, true)} ${nounFor(n, { one: "user", many: "users" })} removed from the allowlist.`;
    case "approve":
      return `${numWord(n, true)} ${nounFor(n, { one: "request", many: "requests" })} approved.`;
    case "reject":
      return `${numWord(n, true)} ${nounFor(n, { one: "request", many: "requests" })} rejected and blocked.`;
    case "unblock":
      return `${numWord(n, true)} `
        + `${nounFor(n, { one: "blocked user", many: "blocked users" })} allowed to request access again.`;
    default:
      return "";
  }
}

// Irregular-pluralization overrides for the three skip REASONS (not
// actions) whose wording can't be built by plugging the raw reason string
// into the generic "N was/were <reason> and was/were skipped." clause below
// (genericSkipClause) -- each needs its own word agreement for n>1
// ("administrator"->"administrators", "holds"->"hold") or, for delete,
// entirely different wording. Keyed on the exact server-side reason string
// (not the action) since a reason is 1:1 with the action that produces it.
// Every OTHER known reason -- "already allowlisted"/"already resolved"/"not
// blocked"/"not found"/"not on the allowlist", and any future one -- reads
// correctly straight out of the generic clause, so no entry is needed here.
const SKIP_REASON_OVERRIDE = {
  "already an administrator": (n) => `${numWord(n, true)} ${wasWere(n)} already `
    + `${n === 1 ? "an administrator" : "administrators"} and ${wasWere(n)} skipped.`,
  "not an administrator": (n) => `${numWord(n, true)} ${wasWere(n)} not `
    + `${n === 1 ? "an administrator" : "administrators"} and ${wasWere(n)} skipped.`,
  "is an administrator — demote first": (n) =>
    `${numWord(n, true)} still ${n === 1 ? "holds" : "hold"} admin and ${wasWere(n)} skipped.`,
};

// The generic per-reason clause: "<N> was/were <reason> and was/were
// skipped." -- what lets a skip reason with NO override above (including one
// the client has never seen before, e.g. a bulk-action row that vanished
// from the allowlist between the client's preview and the server's write)
// still render a grammatical, accurate clause with zero code changes here.
function genericSkipClause(n, reason) {
  return `${numWord(n, true)} ${wasWere(n)} ${reason} and ${wasWere(n)} skipped.`;
}

// Group `skipped` by its raw `reason` string (order = first appearance in
// the array) and render ONE clause per distinct reason (code review #3 /
// M2). Before this fix, the toast rendered exactly one hardcoded-per-ACTION
// clause against `skipped.length`, silently misattributing every item to
// that one reason -- correct only because every skip reason the server
// happened to return for a given action was, so far, homogeneous. A
// HOMOGENEOUS set (the common case, and every existing pinned example)
// collapses to exactly one clause, byte-for-byte identical to the wording
// this replaced. A MIXED set (e.g. a promote batch where some rows were
// already admin and others vanished from the allowlist before the request
// landed) renders one accurate clause per reason, each with its own count.
function skipClauses(skipped) {
  const order = [];
  const counts = new Map();
  for (const { reason } of skipped) {
    if (!counts.has(reason)) { counts.set(reason, 0); order.push(reason); }
    counts.set(reason, counts.get(reason) + 1);
  }
  return order.map((reason) => {
    const n = counts.get(reason);
    const override = SKIP_REASON_OVERRIDE[reason];
    return override ? override(n) : genericSkipClause(n, reason);
  });
}

// The verb naming what a FAILED item couldn't be -- matches the action's own
// past-participle, never the base clause's own wording (e.g. delete's base
// clause says "removed", so its fail clause names the same verb).
const FAIL_VERB = {
  promote: "promoted", demote: "demoted", delete: "removed",
  approve: "approved", reject: "rejected", unblock: "unblocked",
};

export function bulkResultToast(action, result) {
  const { affected, skipped = [], failed = [] } = result;
  const parts = [baseClause(action, affected)];
  if (skipped.length > 0) parts.push(...skipClauses(skipped));
  if (failed.length > 0) {
    parts.push(`${numWord(failed.length, true)} could not be ${FAIL_VERB[action]} `
      + `and ${isAre(failed.length)} still selected.`);
  }
  const kind = failed.length > 0 ? "error" : (affected > 0 ? "ok" : "");
  return { text: parts.join(" "), kind };
}

// The six bulk actions split by whether they REMOVE the acted row from its own
// table. delete/approve/reject/unblock make the row disappear (deleted, or moved
// to another table); promote/demote leave the row in place (only its admin flag
// changes). retainedSelectionAfterBulk keys off this set.
const ACTIONS_THAT_REMOVE_ROWS = new Set(["delete", "approve", "reject", "unblock"]);

// Which of the just-acted ids stay selected after a bulk action commits. The
// product decision is "keep the whole selection": every row still present in the
// table stays checked. So promote/demote (in-place actions) retain the ENTIRE
// selection -- succeeded, skipped and failed rows alike -- while the
// removing actions drop the ids the server actually processed (those rows are
// gone) but keep the ids it skipped (ineligible) or failed on, which are still
// in the table. `selectedRowIds` is every effectively-selected id at click time;
// `result.skipped`/`.failed` carry the same id field (`email` for the users
// table, `id` for pending/blocked). Returns an explicit id array -- the caller
// feeds it to selectExplicit(), which also freezes an "all matching" selection
// to these concrete ids so a row polled in later isn't silently pre-selected.
export function retainedSelectionAfterBulk(action, selectedRowIds, result, idField) {
  if (!ACTIONS_THAT_REMOVE_ROWS.has(action)) return [...selectedRowIds];
  const kept = new Set();
  for (const s of result.skipped || []) kept.add(s[idField]);
  for (const f of result.failed || []) kept.add(f[idField]);
  return selectedRowIds.filter((id) => kept.has(id));
}
