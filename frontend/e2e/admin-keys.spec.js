import { test, expect } from "@playwright/test";
import { mockAdminKeys, mockConversations, mockMe, mockVersion } from "./mocks.js";

// Browser truth for Admin → Keys. The pure table config (what the search covers,
// how never-used rows sort) is unit-tested in frontend/src/apikeys.test.js; this
// covers the tab existing at all, the mint-for-someone-else form, the shared
// reveal dialog naming the recipient, revoke through the confirm modal, and the
// bulk revoke over a row selection (the selection MODEL itself is pinned in
// src/selection.test.js and e2e/admin-bulk-actions.spec.js — what is tested here
// is this table's own wiring into it).

const ROWS = [
  { id: 1, email: "prof@example.edu", last4: "1111", label: "Laptop",
    created_at: 1_700_000_000, created_by: null, last_used_at: 1_700_000_400, revoked_at: null },
  { id: 2, email: "dean@example.edu", last4: "2222", label: null,
    created_at: 1_699_000_000, created_by: "admin@example.edu", last_used_at: null, revoked_at: null },
];

async function openKeys(page, rows = ROWS, opts) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockVersion(page);
  await mockConversations(page, []);
  const api = await mockAdminKeys(page, rows, opts);
  await page.goto("/admin/keys");
  await expect(page.getByRole("heading", { name: "API keys" })).toBeVisible();
  return api;
}

test("the Keys tab is in the admin nav and lists every user's keys", async ({ page }) => {
  await openKeys(page);

  await expect(page.getByRole("link", { name: "Keys" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "prof@example.edu", exact: true })).toBeVisible();
  await expect(page.getByRole("cell", { name: "dean@example.edu", exact: true })).toBeVisible();
  // Masked, never the whole value — the admin table is the widest audience this
  // data ever has.
  await expect(page.getByText("ipeds_mcp_…1111")).toBeVisible();
  // A key that has never been presented reads as an em dash, not as 1970.
  const deanRow = page.getByRole("row", { name: /dean@example\.edu/ });
  await expect(deanRow).toContainText("—");
});

test("searching finds a row by the key fragment alone", async ({ page }) => {
  await openKeys(page);

  // The case the last4 search field exists for: a key seen in a log or a config
  // file, with no idea whose it is.
  await page.getByLabel("Search email, label or key").fill("2222");
  await expect(page.getByRole("cell", { name: "dean@example.edu", exact: true })).toBeVisible();
  await expect(page.getByRole("cell", { name: "prof@example.edu", exact: true })).toHaveCount(0);
});

test("minting for a user reveals the key once and names who to hand it to", async ({ page }) => {
  const secret = "ipeds_mcp_minted-for-someone-5678";
  const api = await openKeys(page, ROWS, { secret });

  await page.getByLabel("Email to mint a key for").fill("new@example.edu");
  await page.getByLabel("Label for the new key").fill("Their laptop");
  await page.getByRole("button", { name: "Create key" }).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByTestId("revealed-key")).toHaveValue(secret);
  // The admin cannot read this value back, so the dialog has to say whose it is
  // — otherwise a mint for the wrong address is discovered only by the person it
  // does not work for. It says so in the TITLE, which used to be the constant
  // "Copy your API key now": on this path the key is not the reader's, and a
  // dialog that calls it theirs muddies who is responsible for delivering it.
  await expect(dialog.getByRole("heading"))
    .toHaveText("Copy this key for new@example.edu");
  // And the duty is body text, not the tail of a muted footnote — the admin
  // guide leads with it.
  await expect(dialog).toContainText("Send it to them over a channel you trust");
  expect(api.mints).toEqual([{ email: "new@example.edu", label: "Their laptop" }]);

  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(page.getByRole("cell", { name: "new@example.edu", exact: true })).toBeVisible();
  await expect(page.getByText(secret)).toHaveCount(0);
});

test("a rejected mint surfaces the server's own reason", async ({ page }) => {
  await openKeys(page, ROWS, {
    httpStatus: 400,
    detail: "That address has never signed in, so it has no account to attach a key to.",
  });

  await page.getByLabel("Email to mint a key for").fill("stranger@example.edu");
  await page.getByRole("button", { name: "Create key" }).click();

  // "Couldn't create that key" would hide the difference between "never signed
  // in" and "not on the allowlist", and those have different fixes.
  await expect(page.locator(".toast-msg")).toContainText(/never signed in/);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("revoking anyone's key keeps the row and drops its action", async ({ page }) => {
  const api = await openKeys(page);

  await page.getByRole("button", { name: /Revoke key ipeds_mcp_…1111 for prof@example\.edu/ }).click();
  const modal = page.getByRole("alertdialog");
  await expect(modal).toContainText("ipeds_mcp_…1111");
  await modal.getByRole("button", { name: "Revoke key" }).click();

  await expect(page.locator(".toast-msg")).toHaveText("Key revoked.");
  expect(api.getRows().find((r) => r.id === 1).revoked_at).not.toBe(null);
  const profRow = page.getByRole("row", { name: /prof@example\.edu/ });
  await expect(profRow).toContainText("Revoked");
  await expect(profRow.getByRole("button", { name: /^Revoke key/ })).toHaveCount(0);
  // The other key is untouched — a revoke must not read as "revoke everything".
  await expect(page.getByRole("row", { name: /dean@example\.edu/ })
    .getByRole("button", { name: /^Revoke key/ })).toBeVisible();
});

test("a failed load renders a visible error, never an empty key table", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockVersion(page);
  await mockConversations(page, []);
  await page.route("**/api/admin/keys", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    return route.fulfill({ status: 500, contentType: "application/json",
      body: JSON.stringify({ detail: "The database is unavailable." }) });
  });
  await page.goto("/admin/keys");

  // "No API keys yet." would tell an admin auditing access that nobody holds one
  // — the dangerous lie, and the same rule the Users tables follow.
  await expect(page.getByRole("alert")).toContainText("The database is unavailable.");
  await expect(page.getByText("No API keys yet.")).toHaveCount(0);
});

// --- bulk revoke -------------------------------------------------------------

function rowCheckbox(page, email) {
  return page.getByRole("row", { name: new RegExp(email.replace(/\./g, "\\.")) })
    .getByRole("checkbox");
}

test("bulk revoke acts on every selected live key, and says what it did",
  async ({ page }) => {
    const api = await openKeys(page);

    await rowCheckbox(page, "prof@example.edu").check();
    await rowCheckbox(page, "dean@example.edu").check();
    await expect(page.locator("[aria-live]").filter({ hasText: "Two keys selected" }))
      .toBeVisible();

    await page.getByRole("button", { name: "Revoke", exact: true }).click();
    const modal = page.getByRole("alertdialog");
    // The count in the dialog is what makes a bulk destructive action
    // answerable — "revoke these?" with a selection off screen is not.
    await expect(modal).toContainText("Two keys are selected.");
    await expect(modal).toContainText("can't be undone");
    await modal.getByRole("button", { name: "Revoke 2 keys" }).click();

    await expect(page.locator(".toast-msg")).toHaveText("Two keys revoked.");
    expect(api.bulkCalls).toEqual([{ action: "revoke", ids: [1, 2] }]);
    expect(api.getRows().every((r) => r.revoked_at != null)).toBe(true);
    // The rows STAY, flipped to Revoked — this table is the record of what a
    // withdrawn key could reach.
    await expect(page.getByRole("row", { name: /prof@example\.edu/ })).toContainText("Revoked");
  });

test("an already-revoked key is not sent, and the dialog says why",
  async ({ page }) => {
    // One live, one already revoked: selecting both must send ONE id, not two,
    // and account for the other rather than silently dropping it.
    const api = await openKeys(page, [
      ROWS[0],
      { ...ROWS[1], revoked_at: 1_699_500_000 },
    ]);

    await rowCheckbox(page, "prof@example.edu").check();
    await rowCheckbox(page, "dean@example.edu").check();

    await page.getByRole("button", { name: "Revoke", exact: true }).click();
    const modal = page.getByRole("alertdialog");
    await expect(modal).toContainText("Two keys are selected.");
    await expect(modal).toContainText("One key will stop working immediately");
    await expect(modal).toContainText("One is already revoked and will not be changed.");
    await modal.getByRole("button", { name: "Revoke key" }).click();

    await expect(page.locator(".toast-msg")).toHaveText("One key revoked.");
    expect(api.bulkCalls).toEqual([{ action: "revoke", ids: [1] }]);
  });

test("with only revoked keys selected, the action is disabled rather than dead",
  async ({ page }) => {
    await openKeys(page, [{ ...ROWS[0], revoked_at: 1_699_500_000 }]);

    await rowCheckbox(page, "prof@example.edu").check();

    const revoke = page.getByRole("button", { name: "Revoke", exact: true });
    await expect(revoke).toBeDisabled();
    await expect(revoke).toHaveAttribute("title", "Selected keys are already revoked.");
  });

test("changing the search clears the selection instead of acting on rows "
  + "the admin can no longer see", async ({ page }) => {
    await openKeys(page);

    await rowCheckbox(page, "prof@example.edu").check();
    await expect(page.locator("[aria-live]").filter({ hasText: "One key selected" }))
      .toBeVisible();

    await page.getByLabel("Search email, label or key").fill("dean");

    await expect(page.getByRole("button", { name: "Revoke", exact: true })).toHaveCount(0);
    await expect(page.locator(".toast-msg"))
      .toHaveText("Selection cleared because the search changed.");
  });
