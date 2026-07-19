import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockAllowlistBulk,
  mockAccessRequestsBulk,
  mockDeniedRequestsBulk,
} from "./mocks.js";

// Browser truth for bulk row-selection + bulk actions on the three admin tables
// (Allowlist users / Pending requests / Blocked users). The pure counting/string
// logic (tri-state derivation, selection counts, confirm-summary/result-toast
// copy) is unit-tested in frontend/src/selection.test.js (vitest) — this owns
// what only a browser gives: checkbox tri-state (incl. `indeterminate`, which
// jsdom fakes), the search-clears-selection flow, the disabled self-checkbox,
// and the full confirm -> processing -> toast -> refresh flow through the
// SAME reusable ConfirmModal already proven generically by confirm-modal.spec.js
// (so this file does not re-test focus-trap/Escape/retry — only the bulk-
// specific wiring around it).
//
// Every selector/label choice below not literally pinned by the architect's
// contract (header-checkbox accessible name, per-row checkbox naming, the
// bulk button labels) is the test-engineer's own call, made for internal
// consistency with the contract's given examples — see the test-engineer's
// report to the PM for the full list.

const ADMIN = { email: "admin@example.edu", is_admin: true };

function userRows() {
  return [
    { email: "admin@example.edu", note: "me", is_admin: true, last_login: 1_700_000_000 },
    { email: "alice@example.edu", note: "", is_admin: false, last_login: 1_700_000_000 },
    { email: "bob@example.edu", note: "", is_admin: false, last_login: 1_700_000_000 },
    { email: "carol@example.edu", note: "", is_admin: false, last_login: 1_700_000_000 },
    { email: "dave@example.edu", note: "", is_admin: false, last_login: 1_700_000_000 },
  ];
}

async function openUsersBulk(page, { rows = userRows(), forceFailed, delayMs } = {}) {
  await mockMe(page, ADMIN);
  await mockConversations(page, []);
  const users = await mockAllowlistBulk(page, rows, { forceFailed, delayMs });
  await mockAccessRequestsBulk(page, []);
  await mockDeniedRequestsBulk(page, []);
  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await expect(page.getByRole("heading", { name: "Users" })).toBeVisible();
  return users;
}

function rowCheckbox(page, email) {
  return page.getByRole("row", { name: new RegExp(email.replace(/\./g, "\\.")) })
    .getByRole("checkbox");
}

test.describe("bulk row selection — checkbox tri-state", () => {
  test("the checkbox column is first, and the page header checkbox goes "
    + "none -> some (indeterminate) -> all", async ({ page }) => {
    await openUsersBulk(page);

    // Structural: the checkbox column renders FIRST (before Email).
    const firstHeaderCell = page.locator("table.grid.users thead tr th").first();
    await expect(firstHeaderCell.getByRole("checkbox")).toBeVisible();

    const header = page.getByRole("checkbox", { name: "Select all users on this page" });
    await expect(header).not.toBeChecked();

    await rowCheckbox(page, "alice@example.edu").check();
    await expect(header).not.toBeChecked();
    // The indeterminate DOM property is what actually drives the tri-state
    // visual — jsdom cannot be trusted for this, which is exactly why this
    // check belongs in Playwright, not vitest.
    expect(await header.evaluate((el) => el.indeterminate)).toBe(true);

    await rowCheckbox(page, "bob@example.edu").check();
    await rowCheckbox(page, "carol@example.edu").check();
    await rowCheckbox(page, "dave@example.edu").check();
    await expect(header).toBeChecked();
    expect(await header.evaluate((el) => el.indeterminate)).toBe(false);
  });

  test("checking a row announces the live selected count (WCAG 4.1.3)", async ({ page }) => {
    await openUsersBulk(page);
    await rowCheckbox(page, "alice@example.edu").check();
    // selectedCountLabel(1, ...) spells out the count ("One user selected") --
    // this is the same string selection.test.js pins for the pure function;
    // this test only proves the aria-live region actually fires with it.
    await expect(page.locator("[aria-live]").filter({ hasText: "One user selected" })).toBeVisible();
  });
});

test.describe("select-all-matching", () => {
  test("select-all-matching excludes the signed-in admin's own row, and "
    + "un-checking one row narrows the count (the '142 of 143' pattern)", async ({ page }) => {
    await openUsersBulk(page); // 5 rows total, 1 is self -> 4 eligible

    // 4 eligible (self excluded) -> "Select all four matching users" (a
    // count <10 is spelled out per the pinned NUMBER RENDERING convention).
    const selectAllMatching = page.getByRole("button", { name: "Select all four matching users" });
    await expect(selectAllMatching).toBeVisible();
    await selectAllMatching.click();
    await expect(page.getByText("All four matching users are selected.")).toBeVisible();

    // Excluding one row narrows the count from all-matching to a REDUCED count.
    await rowCheckbox(page, "bob@example.edu").uncheck();
    await expect(page.getByText("Three of four matching users are selected.")).toBeVisible();

    // The self row was never counted among the four matching users at all —
    // it must stay unchecked/disabled throughout.
    const selfBox = rowCheckbox(page, "admin@example.edu");
    await expect(selfBox).toBeDisabled();
    await expect(selfBox).not.toBeChecked();
  });
});

test.describe("search clears the selection", () => {
  test("typing in the search box clears the selection and toasts why", async ({ page }) => {
    await openUsersBulk(page);
    await rowCheckbox(page, "alice@example.edu").check();
    await rowCheckbox(page, "bob@example.edu").check();
    await expect(page.getByRole("checkbox", { name: "Select all users on this page" }))
      .toHaveJSProperty("indeterminate", true);

    await page.getByRole("searchbox", { name: "Search email or note" }).fill("car");

    await expect(page.locator(".toast-msg")).toHaveText(
      "Selection cleared because the search changed.");
    // Clearing the search brings every row back; none should still read checked.
    await page.getByRole("button", { name: "Clear search" }).click();
    await expect(rowCheckbox(page, "alice@example.edu")).not.toBeChecked();
    await expect(rowCheckbox(page, "bob@example.edu")).not.toBeChecked();
  });
});

test.describe("own-row checkbox is disabled", () => {
  test("the signed-in admin's own row checkbox is disabled with the exact reason", async ({ page }) => {
    await openUsersBulk(page);
    const selfBox = rowCheckbox(page, "admin@example.edu");
    await expect(selfBox).toBeDisabled();
    await expect(selfBox).toHaveAccessibleName(
      "You cannot select your own account for bulk actions.");
  });
});

test.describe("full bulk flow", () => {
  test("promote: BulkBar action -> confirm (explicit label, never OK/Yes/Confirm) "
    + "-> processing -> result toast -> the table refreshes", async ({ page }) => {
    const users = await openUsersBulk(page, { delayMs: 300 });
    await rowCheckbox(page, "alice@example.edu").check();
    await rowCheckbox(page, "bob@example.edu").check();
    await rowCheckbox(page, "carol@example.edu").check();

    const promoteBtn = page.getByRole("button", { name: "Promote 3 users" });
    await expect(promoteBtn).toBeVisible();
    await promoteBtn.click();

    const dialog = page.getByRole("dialog"); // promote = neutral variant, never alertdialog
    await expect(dialog).toBeVisible();
    // Never a generic label — always the explicit action.
    await expect(dialog.getByRole("button", { name: "OK", exact: true })).toHaveCount(0);
    await expect(dialog.getByRole("button", { name: "Yes", exact: true })).toHaveCount(0);
    await expect(dialog.getByRole("button", { name: "Confirm", exact: true })).toHaveCount(0);
    const confirmBtn = dialog.getByRole("button", { name: "Promote 3 users", exact: true });
    await expect(confirmBtn).toBeVisible();

    await confirmBtn.click();
    // Processing: the SAME generic ConfirmModal plumbing confirm-modal.spec.js
    // already proves — this only confirms the bulk action actually routes
    // through it (aria-busy, not a bespoke bulk-only modal).
    await expect(confirmBtn).toHaveAttribute("aria-busy", "true");
    await expect(dialog).toHaveCount(0, { timeout: 5000 }); // closes once it resolves

    await expect(page.locator(".toast-msg")).toHaveText("Three users promoted.");
    expect(users.bulkCalls).toEqual([
      { action: "promote", emails: ["alice@example.edu", "bob@example.edu", "carol@example.edu"] },
    ]);

    // The table refreshed: all three now show as admins.
    for (const email of ["alice@example.edu", "bob@example.edu", "carol@example.edu"]) {
      await expect(page.getByRole("row", { name: new RegExp(email.replace(/\./g, "\\.")) }))
        .toContainText("✓ Admin");
    }
    // The selection is cleared on success.
    const header = page.getByRole("checkbox", { name: "Select all users on this page" });
    await expect(header).not.toBeChecked();
    expect(await header.evaluate((el) => el.indeterminate)).toBe(false);
  });

  test("cancel preserves the selection and fires no request", async ({ page }) => {
    const users = await openUsersBulk(page);
    await rowCheckbox(page, "alice@example.edu").check();
    await rowCheckbox(page, "bob@example.edu").check();

    await page.getByRole("button", { name: "Promote 2 users" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toHaveCount(0);

    expect(users.bulkCalls).toEqual([]);
    await expect(rowCheckbox(page, "alice@example.edu")).toBeChecked();
    await expect(rowCheckbox(page, "bob@example.edu")).toBeChecked();
  });

  test("on partial failure, the failed row stays selected while the "
    + "successful row's selection clears", async ({ page }) => {
    const users = await openUsersBulk(page, { forceFailed: new Set(["bob@example.edu"]) });
    await rowCheckbox(page, "alice@example.edu").check();
    await rowCheckbox(page, "bob@example.edu").check();

    await page.getByRole("button", { name: "Promote 2 users" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Promote 2 users", exact: true }).click();

    await expect(page.locator(".toast-msg")).toHaveText(
      "One user promoted. One could not be promoted and is still selected.");
    await expect(page.locator(".toast.error")).toBeVisible();

    await expect(rowCheckbox(page, "alice@example.edu")).not.toBeChecked();
    await expect(rowCheckbox(page, "bob@example.edu")).toBeChecked();
    expect(users.bulkCalls).toEqual([
      { action: "promote", emails: ["alice@example.edu", "bob@example.edu"] },
    ]);
  });
});

test.describe("cross-table refresh", () => {
  test("bulk approve refreshes BOTH the pending table and the users table", async ({ page }) => {
    await mockMe(page, ADMIN);
    await mockConversations(page, []);
    const users = await mockAllowlistBulk(page, [
      { email: "admin@example.edu", note: "me", is_admin: true, last_login: 1_700_000_000 },
    ]);
    const pending = await mockAccessRequestsBulk(page, [
      { id: 1, email: "newcomer@example.edu", reason: null, status: "pending", created_at: 1_700_000_000 },
    ], {
      onApprove: (row) => users.addRow(
        { email: row.email, note: null, is_admin: false, last_login: null }),
    });
    await mockDeniedRequestsBulk(page, []);
    await page.goto("/");
    await page.getByRole("link", { name: "Admin" }).click();

    await page.getByRole("row", { name: /newcomer@example\.edu/ })
      .getByRole("checkbox").check();
    await page.getByRole("button", { name: "Approve 1 request" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Approve 1 request", exact: true }).click();

    await expect(page.locator(".toast-msg")).toHaveText("One request approved.");
    // Pending table empties back to its zero-state...
    await expect(page.getByText("No access requests are awaiting review.")).toBeVisible();
    // ...AND the newly-approved address now appears in the Users table, with
    // NO extra reload action from the admin.
    await expect(page.getByRole("cell", { name: "newcomer@example.edu", exact: true })).toBeVisible();
    expect(pending.bulkCalls).toEqual([{ action: "approve", ids: [1] }]);
  });
});
