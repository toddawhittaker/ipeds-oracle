import { test, expect } from "@playwright/test";
import {
  gotoAdmin,
  mockMe,
  mockConversations,
  mockSkills,
  mockSkillCategories,
  mockSkillRejections,
  mockDeleteRejections,
} from "./mocks.js";

// A2 (lesson-rejection memory): the "Reject & mute <LABEL>" action on a
// categorised lesson, the collapsed "Rejected (N)" / "Muted categories (N)"
// sections, and the load-failure state for the rejections panel. Modeled on
// frontend/e2e/undo-denial.spec.js (the closest existing precedent: an
// admin-reversible block/undo flow with its own confirmation + a visible
// load-failure state, not silent absence).
//
// None of this exists in the frontend yet -- every test below is expected RED
// until Skills.jsx grows the category pill, the mute action, and the two
// collapsed sections. Backend routes are equally new (see
// backend/tests/test_admin_router.py's A2 block): GET/POST/DELETE
// /api/admin/skills/categories(/{token}/mute), GET/DELETE
// /api/admin/skills/rejections(/{id}), and DELETE /api/admin/skills/{id}
// gaining an optional ?mute_category=1.

const CATEGORY_ROWS = [
  { token: "CIP_ROLLUP", label: "CIP rollup double-count", learnable: true, muted: false, pending: 1 },
  { token: "SECOND_MAJOR", label: "Second-major double-count", learnable: true, muted: false, pending: 0 },
  { token: "AWARD_LEVEL", label: "Award-level mixing", learnable: true, muted: false, pending: 0 },
  { token: "MAGNITUDE", label: "Implausible magnitude", learnable: true, muted: false, pending: 0 },
  { token: "QUESTION_MISMATCH", label: "Answer doesn't match the question", learnable: true, muted: false, pending: 0 },
  { token: "UNGROUNDED_NUMBER", label: "Number not in the data", learnable: false, muted: false, pending: 0 },
  { token: "OTHER", label: "Other", learnable: false, muted: false, pending: 0 },
];

test("Reject & mute ALWAYS confirms (even a fresh, no-vote, unverified proposal), and issues exactly ONE DELETE carrying mute_category=1", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockSkillCategories(page, CATEGORY_ROWS);
  await mockSkillRejections(page, []);
  // Deliberately a fresh, unverified, zero-votes/hits proposal -- exactly the
  // shape Skills.jsx's PLAIN reject action dismisses WITHOUT a confirmation
  // (see reject()'s `risky` check). Muting is a change to FUTURE behaviour,
  // not a disposable single proposal, so it must confirm regardless.
  await mockSkills(page, [
    {
      id: 50, question: "q", headline: "Rollup mixing headline.",
      lesson: "Rollup mixing description.", canonical_sql: "SELECT 1", notes: "",
      verified: false, created_by: "critic", category: "CIP_ROLLUP",
      upvotes: 0, downvotes: 0, hits: 0,
    },
  ]);
  const deleteUrls = [];
  page.on("request", (r) => {
    if (r.method() === "DELETE" && r.url().includes("/api/admin/skills/50")) deleteUrls.push(r.url());
  });
  await page.route("**/api/admin/skills/50*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' }));

  await page.goto("/");
  await gotoAdmin(page);
  await page.getByRole("link", { name: "Skills" }).click();

  await page.getByRole("button", { name: /Reject & mute CIP rollup double-count/i }).click();

  // Synchronous check BEFORE confirming: the click alone must not have fired
  // anything yet (an auto-retrying matcher after the confirm click could hide
  // a version that skips the modal entirely and still ends at exactly 1).
  expect(deleteUrls.length).toBe(0);

  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: /mute/i }).click();

  await expect.poll(() => deleteUrls.length).toBe(1);
  expect(deleteUrls[0]).toContain("mute_category=1");
});

test("Muted categories (N): a muted category is listed with an Unmute action that clears it", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockSkills(page, []);
  await mockSkillRejections(page, []);

  let muted = true;
  await page.route("**/api/admin/skills/categories", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const rows = CATEGORY_ROWS.map((r) => (r.token === "AWARD_LEVEL" ? { ...r, muted } : r));
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(rows) });
  });
  let unmuteCalls = 0;
  await page.route("**/api/admin/skills/categories/AWARD_LEVEL/mute", async (route) => {
    if (route.request().method() !== "DELETE") return route.continue();
    unmuteCalls += 1;
    muted = false;
    await route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' });
  });

  await page.goto("/");
  await gotoAdmin(page);
  await page.getByRole("link", { name: "Skills" }).click();

  const mutedHeading = page.getByText(/Muted categories \(1\)/);
  await expect(mutedHeading).toBeVisible();
  await mutedHeading.click(); // expand the collapsed section
  await expect(page.getByText("Award-level mixing")).toBeVisible();

  await page.getByRole("button", { name: /Unmute.*Award-level mixing/i }).click();
  await expect.poll(() => unmuteCalls).toBe(1);
  await expect(page.getByText(/Muted categories \(0\)/)).toBeVisible();
});

test("Rejected (N): collapsed by default, expands, per-row undo removes the row and focus lands off <body>", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockSkillCategories(page, CATEGORY_ROWS);
  await mockSkills(page, []);
  await mockSkillRejections(page, [
    {
      id: 101, headline: "Rejected headline one.", description: "A rejected rule description.",
      category: "CIP_ROLLUP", created_by: "critic", skill_id: 900, was_verified: 0,
      hits: 0, created_at: 1_700_000_000,
    },
  ]);
  const del = await mockDeleteRejections(page);

  await page.goto("/");
  await gotoAdmin(page);
  await page.getByRole("link", { name: "Skills" }).click();

  // Collapsed by default -- the row content is not on screen up front.
  await expect(page.getByText("Rejected headline one.")).toBeHidden();
  const rejectedHeading = page.getByText(/Rejected \(1\)/);
  await expect(rejectedHeading).toBeVisible();  // fail fast if the section itself is missing
  await rejectedHeading.click();
  await expect(page.getByText("Rejected headline one.")).toBeVisible();

  await page.getByRole("button", { name: /Allow again: Rejected headline one\./i }).click();
  await expect.poll(() => del.calls).toEqual([101]);
  await expect(page.getByText("Rejected headline one.")).toHaveCount(0);

  // Focus must land somewhere real, never <body> (WCAG 2.4.3) -- the same
  // "clearing the last row must not drop focus" contract undo-denial.spec.js
  // pins for the Blocked-users table. Polled, not a single synchronous check:
  // the restore is a post-commit effect (the focus-restore-vs-reload race this
  // codebase has hit before -- see Skills.jsx's own pendingEditFocus pattern),
  // so a bare snapshot can catch the brief moment focus is still on <body>
  // before that effect runs, even though it settles correctly moments later.
  await expect.poll(
    () => page.evaluate(() => globalThis.document.activeElement === globalThis.document.body),
  ).toBe(false);
});

test("a rejections load failure renders a visible error, never 'Rejected (0)'", async ({ page }) => {
  // SEC #3 precedent, generalized (see undo-denial.spec.js and
  // Allowlist.jsx's deniedError): a failed load must never read as
  // "confirmed empty" -- an admin reading "Rejected (0)" believes nothing has
  // ever been rejected, when the truth is the list couldn't be fetched at all.
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockSkillCategories(page, CATEGORY_ROWS);
  await mockSkills(page, []);
  await mockSkillRejections(page, [], { httpStatus: 500 });

  await page.goto("/");
  await gotoAdmin(page);
  await page.getByRole("link", { name: "Skills" }).click();

  await expect(page.getByText(/rejected \(0\)/i)).toHaveCount(0);
  await expect(page.getByText(/could(n.t| not) load rejected lessons/i)).toBeVisible();
});
