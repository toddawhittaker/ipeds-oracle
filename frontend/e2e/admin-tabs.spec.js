import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockAllowlist,
  mockAccessRequests,
  mockDeniedRequests,
  mockUsage,
  mockSkills,
  mockImportJobs,
} from "./mocks.js";

// Flow 4: admin tabs. Signed in as an admin, click through each Admin subtab
// and assert its mocked content renders; also submit the add-allowlist form
// and assert the POST fired with the expected body.
test("admin tabs render mocked content and the add-allowlist form posts", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);

  const allowlist = await mockAllowlist(page, [
    { email: "user@example.edu", note: "staff", is_admin: false, last_login: 1700000000 },
  ]);
  await mockAccessRequests(page, [{ id: 1, email: "newperson@example.edu" }]);
  // SEC #3 (round-3 security review): Admin.jsx's load() swallows a failed
  // GET .../denied with a bare .catch(() => {}) -- added, per the
  // implementer, because THIS spec didn't mock the endpoint and an
  // unhandled rejection surfaced as a page error. Mocking it here removes
  // that test-convenience justification for the swallow; see
  // undo-denial.spec.js's SEC #3 test for the actual failure-state
  // regression test that now covers the endpoint's own error path.
  await mockDeniedRequests(page, []);
  await mockUsage(page, {
    since: 0, until: 1, bucket: "day",
    totals: { queries: 123, tokens: 45678, spend: 0.42, cache_hits: 12, escalations: 3, failures: 1 },
    series: [{ t: "2026-07-13", queries: 10, tokens: 2000, spend: 0.02 }],
    top_users: [{ email: "user@example.edu", queries: 50, tokens: 12000, spend: 0.1 }],
  });
  await mockSkills(page, [
    {
      id: 1,
      question: "CA nursing associate's degrees by year",
      lesson: "Match cipcode='51.3801' exactly; never LIKE (rollup overcount).",
      canonical_sql: "SELECT year, SUM(x) FROM c_a WHERE cipcode='51.3801' GROUP BY year",
      notes: "confirmed against known totals",
      verified: true,
      created_by: "seed",
      upvotes: 3,
      downvotes: 0,
      hits: 5,
    },
  ]);
  await mockImportJobs(page, [
    { id: 9, filename: "IPEDS2526.accdb", status: "passed", updated_at: 1700000000 },
  ]);

  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();

  // Allowlist is the default subtab — do the form submission here, before
  // navigating anywhere else. Skills is still visited last purely to keep
  // this happy-path spec's flow linear; the Skills-unmount crash regression
  // itself is covered separately below.
  // Current users is the default sub-tab: its table + add form show now; the
  // Pending / Blocked tables live behind their own tabs (checked below).
  // exact:true -- the Users table's leading selection checkbox (H1 a11y fix)
  // has accessible name "Select user user@example.edu", which otherwise
  // substring-collides with this cell under Playwright's default matching.
  await expect(page.getByRole("cell", { name: "user@example.edu", exact: true })).toBeVisible();

  await page.getByPlaceholder("email", { exact: true }).fill("newuser@example.edu");
  await page.getByPlaceholder("note (optional)").fill("added via e2e");
  await page.getByRole("button", { name: "Add" }).click();

  await expect.poll(() => allowlist.posts.length).toBe(1);
  expect(allowlist.posts[0]).toEqual({
    email: "newuser@example.edu",
    note: "added via e2e",
    is_admin: false,
  });

  // Switch to the Pending requests sub-tab to see its row (the tab's accessible
  // name carries the count badge, e.g. "Pending requests 1").
  await page.getByRole("tab", { name: /Pending requests/ }).click();
  await expect(page.getByRole("cell", { name: "newperson@example.edu", exact: true })).toBeVisible();

  await page.getByRole("link", { name: "Imports" }).click();
  await expect(page.getByText("IPEDS2526.accdb")).toBeVisible();
  await expect(page.getByRole("cell", { name: "9" })).toBeVisible();

  await page.getByRole("link", { name: "Usage" }).click();
  // "Queries" also labels table columns further down the panel, so scope to
  // the first (summary stat) occurrence rather than asserting a unique match.
  await expect(page.getByText("123", { exact: true })).toBeVisible();
  await expect(page.getByText("Queries", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("cell", { name: "user@example.edu" })).toBeVisible();

  // Skills last — do not navigate away from it (see known-bug test below).
  await page.getByRole("link", { name: "Skills" }).click();
  await expect(page.getByText(/Match cipcode='51.3801' exactly/)).toBeVisible();
  await expect(page.getByText("verified", { exact: true })).toBeVisible();
});

// Regression test — was a KNOWN BUG (test.fail'd) until fixed.
//
// frontend/src/Admin.jsx's Skills() component used to do:
//   const load = () => api.skills().then(setRows);
//   useEffect(load, []);
// `load`'s arrow-fn body had no braces, so it implicitly returned the Promise
// from `.then()`. React treats a non-undefined return from an effect as a
// cleanup ("destroy") function; when Skills unmounted (i.e. the admin clicked
// to any other subtab), React invoked that Promise as `destroy()`, threw
// "TypeError: destroy is not a function", and — with no error boundary in the
// tree — the entire app unmounted (root element went empty).
//
// Fixed to useEffect(() => { load(); }, []), which returns undefined. This
// test now asserts the correct behavior directly: navigating away from Skills
// (and back) must not crash the app.
test("regression: navigating away from the Skills tab and back does not crash the app", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", (err) => pageErrors.push(err));

  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAllowlist(page, []);
  await mockAccessRequests(page, []);
  // See the SEC #3 comment on the mockDeniedRequests call above.
  await mockDeniedRequests(page, []);
  await mockSkills(page, [
    { id: 1, question: "q", lesson: "example rule", canonical_sql: "SELECT 1", notes: "",
      verified: true, created_by: "seed", upvotes: 0, downvotes: 0, hits: 0 },
  ]);

  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByRole("link", { name: "Skills" }).click();
  await expect(page.getByText("example rule")).toBeVisible();

  // Unmount Skills by navigating to another subtab — this used to crash the
  // whole app (root element went blank). Assert visibility first (fails fast
  // at expect's 5s default) rather than a raw .click(), which would otherwise
  // block for the full test timeout while the "Allowlist" -> "Users" rename
  // is still unimplemented.
  const usersSub = page.getByRole("link", { name: "Users" });
  await expect(usersSub).toBeVisible();
  await usersSub.click();
  await expect(page.getByRole("heading", { name: "Users" })).toBeVisible();
  // The rest of the shell must still be intact too, not just the Admin panel.
  await expect(page.getByRole("link", { name: "Chat", exact: true })).toBeVisible();

  // And back to Skills again, to be thorough about remounting.
  await page.getByRole("link", { name: "Skills" }).click();
  await expect(page.getByText("example rule")).toBeVisible();

  expect(pageErrors).toEqual([]);
});
