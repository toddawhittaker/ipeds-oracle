import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockAllowlist,
  mockAccessRequests,
  mockDeniedRequests,
  mockClearDenial,
} from "./mocks.js";

// The Blocked users TABLE (a <DataTable> config in Admin.jsx) and the unblock
// flow (GET /api/admin/access-requests/denied, DELETE .../{email}/denial).
//
// Contract:
//   * heading "Blocked users", hidden entirely when the denied list is empty
//     (but a LOAD FAILURE still shows a visible error — SEC #3).
//   * Email column shows canon_email (the ACTUALLY-blocked mailbox) as the
//     primary label with the original address(es) as context (SEC #1); Requested
//     and Denied are SEPARATE, honestly-labeled columns (SEC #4).
//   * the unblock action ("Allow new access request") opens a NEUTRAL
//     confirmation modal (this DELIBERATELY REVERSES the earlier no-confirm
//     decision, per the new spec) and only DELETEs the CANONICAL address on
//     confirm; the success toast states both negatives (no access, no email).
//
// SEC/A11Y notes carried from the previous (flex-list) UI:
//   * SEC #1 (HIGH): a +tag-only griefing denial must still surface the base
//     address that's actually blocked, without dropping the filed variant.
//   * SEC #2 (MEDIUM): Reject's confirm names the canonical (actually-blocked)
//     address, not the literal typed-in one.
//   * SEC #3 (LOW): a failed denied-list load must not look identical to "empty".
//   * SEC #4 (LOW): the request time is labeled "Requested"; the denial time is a
//     separate "Denied" column (backend migration 11's denied_at).
//   * WCAG 2.5.3 Label in Name: the icon action's tooltip is contained in its
//     accessible name.

async function openAllowlistTab(
  page, { allowlist = [], reqs = [], denied = [], deniedHttpStatus = 200 } = {},
) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAllowlist(page, allowlist);
  await mockAccessRequests(page, reqs);
  await mockDeniedRequests(page, denied, { httpStatus: deniedHttpStatus });
  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
}

const ONE_DENIED_GROUP = [
  {
    id: 4,
    canon_email: "victim@example.edu",
    emails: ["victim+1@example.edu", "victim@example.edu"],
    created_at: 1_700_000_000,
    denied_at: 1_700_000_500,
  },
];

const TAGGED_ONLY_GROUP = [
  {
    // The ACTUALLY-BLOCKED base address; the attacker filed ONLY the +tag
    // variant and the admin Rejected THAT, so canon_email is not itself among
    // `emails` (the tagged-only griefing case, verified against the backend).
    id: 12,
    canon_email: "onlytagged@example.edu",
    emails: ["onlytagged+newsletter@example.edu"],
    created_at: 1_700_000_000,
    denied_at: 1_700_000_500,
  },
];

const blockedTable = (page) => page.locator("table[aria-label='Blocked users'] tbody tr");
const unblockBtn = (page) => page.getByRole("button", { name: /allow new access request/i });

test.describe("blocked users table + unblock", () => {
  test("renders one unblock control per canonical group, showing the ORIGINAL addresses", async ({ page }) => {
    await openAllowlistTab(page, { denied: ONE_DENIED_GROUP });

    await expect(page.getByText("victim@example.edu", { exact: true })).toBeVisible();
    await expect(page.getByText("victim+1@example.edu")).toBeVisible();
    await expect(unblockBtn(page)).toHaveCount(1);
  });

  test("SEC #1: the actually-blocked (canonical) address is surfaced AND the filed variant is retained", async ({ page }) => {
    await openAllowlistTab(page, { denied: TAGGED_ONLY_GROUP });
    // Both the base (blocked) address and the filed +tag variant appear together
    // in the same row — an admin can connect "the request I saw" to "what's blocked".
    const row = blockedTable(page).filter({ hasText: "onlytagged" });
    await expect(row).toContainText("onlytagged@example.edu");
    await expect(row).toContainText("onlytagged+newsletter@example.edu");
  });

  test("the 'Blocked users' heading is absent when nothing is denied, present when a denial exists", async ({ page }) => {
    await openAllowlistTab(page, { denied: [] });
    await expect(page.getByRole("heading", { name: "Blocked users" })).toHaveCount(0);

    await openAllowlistTab(page, { denied: ONE_DENIED_GROUP });
    await expect(page.getByRole("heading", { name: "Blocked users" })).toBeVisible();
  });

  test("SEC #4: Requested and Denied are separate, honestly-labeled columns", async ({ page }) => {
    await openAllowlistTab(page, { denied: ONE_DENIED_GROUP });
    await expect(page.getByRole("columnheader", { name: /Requested/ })).toBeVisible();
    await expect(page.getByRole("columnheader", { name: /Denied/ })).toBeVisible();
  });

  test("Unblock opens a NEUTRAL confirmation modal, then DELETEs the CANONICAL address on confirm", async ({ page }) => {
    const clear = await mockClearDenial(page, { httpStatus: 200 });
    await openAllowlistTab(page, { denied: ONE_DENIED_GROUP });

    await unblockBtn(page).click();
    // Neutral (role="dialog", not the danger alertdialog), and it explains this
    // grants no access.
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(page.getByRole("alertdialog")).toHaveCount(0);
    await expect(dialog).toContainText("victim@example.edu");
    await expect(dialog).toContainText(/not approve access|must submit a new request/i);
    await dialog.getByRole("button", { name: "Allow new request" }).click();

    await expect.poll(() => clear.calls.length).toBe(1);
    // Keyed on canon_email, not either displayed original.
    expect(clear.calls[0]).toBe("victim@example.edu");
  });

  test("Unblock: clearing the LAST block moves focus to a stable control, not <body>", async ({ page }) => {
    // When the only blocked row is cleared, the whole Blocked section (and its
    // search box) unmounts — focus must fall back to the always-present add-email
    // input, never drop to <body> (WCAG 2.4.3).
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockConversations(page, []);
    await mockAllowlist(page, []);
    await mockAccessRequests(page, []);
    await mockClearDenial(page, { httpStatus: 200 });
    let call = 0;
    await page.route("**/api/admin/access-requests/denied", async (route) => {
      call += 1;
      const body = call === 1 ? ONE_DENIED_GROUP : [];
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    });
    await page.goto("/");
    await page.getByRole("link", { name: "Admin" }).click();

    await unblockBtn(page).click();
    await page.getByRole("dialog").getByRole("button", { name: "Allow new request" }).click();

    await expect(page.getByRole("heading", { name: "Blocked users" })).toHaveCount(0);
    await expect(page.getByLabel("Email", { exact: true })).toBeFocused();
  });

  test("Unblock: cancelling the modal fires no DELETE", async ({ page }) => {
    const clear = await mockClearDenial(page, { httpStatus: 200 });
    await openAllowlistTab(page, { denied: ONE_DENIED_GROUP });

    await unblockBtn(page).click();
    await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
    await page.waitForTimeout(200);
    expect(clear.calls.length).toBe(0);
  });

  test("Unblock success toast states BOTH negatives: no access granted, no email sent", async ({ page }) => {
    await mockClearDenial(page, { httpStatus: 200 });
    await openAllowlistTab(page, { denied: ONE_DENIED_GROUP });

    await unblockBtn(page).click();
    await page.getByRole("dialog").getByRole("button", { name: "Allow new request" }).click();

    await expect(page.locator(".toast")).toContainText(/not given access|no email/i);
  });

  test("Unblock: a failed unblock shows the block persists and stays recoverable", async ({ page }) => {
    await mockClearDenial(page, { httpStatus: 500 });
    await openAllowlistTab(page, { denied: ONE_DENIED_GROUP });

    await unblockBtn(page).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("button", { name: "Allow new request" }).click();

    await expect(page.locator(".toast")).toContainText(/still blocked from requesting access/i);
    await expect(dialog).toBeVisible();
    await expect(dialog.locator(".notice.error")).toBeVisible();
  });

  test("Reject's confirm names the canonical address and no longer claims allowlisting is the only escape (SEC #2)", async ({ page }) => {
    const reqs = [
      { id: 3, email: "victim+newsletter@example.edu", reason: null, status: "pending",
        created_at: 1_700_000_000 },
    ];
    await openAllowlistTab(page, { reqs });

    await page.getByRole("button", { name: "Reject request from victim+newsletter@example.edu" }).click();
    const dialog = page.getByRole("alertdialog");
    // SEC #2: names the address actually blocked (the base), not the typed +tag.
    await expect(dialog).toContainText("victim@example.edu");
    // The old, now-false "permanent unless allowlisted" claim is gone.
    await expect(dialog).not.toContainText(/unless you add them to the allowlist|can't be undone/i);
  });

  test("SEC #3: a failed denied-list load keeps the section visible with an error state, not silent absence", async ({ page }) => {
    await openAllowlistTab(page, { deniedHttpStatus: 500 });
    await expect(page.getByText(/could(n.t| not) load blocked addresses/i)).toBeVisible();
  });

  test("WCAG 2.5.3 (Label in Name): the unblock button's tooltip is contained in its accessible name", async ({ page }) => {
    await openAllowlistTab(page, { denied: ONE_DENIED_GROUP });

    const btn = unblockBtn(page);
    const tip = (await btn.getAttribute("data-tip"))?.trim() || "";
    expect(tip.length).toBeGreaterThan(0);
    const escaped = tip.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    await expect(btn).toHaveAccessibleName(new RegExp(escaped, "i"));
  });
});
