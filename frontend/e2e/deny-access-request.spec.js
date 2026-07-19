import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockAllowlist,
  mockAccessRequests,
  mockDeniedRequests,
  mockDenyAccessRequest,
} from "./mocks.js";

// The Pending access requests TABLE (a <DataTable> config in Admin.jsx): compact
// icon Approve/Reject actions, an app-styled confirmation modal per action
// (neutral Approve, danger Reject — never window.confirm), the count badge on
// the Pending sub-tab (accent "attention" tone while there's something to
// review), and the zero-state when there isn't. Search/sort/pagination
// themselves are covered by the shared DataTable specs (users-table.spec.js) +
// datatable.test.js.
//
// Pending requests now live behind the Users → "Pending requests" sub-tab; the
// helper lands there so each test acts on the pending table directly.

const openPending = (page) => page.getByRole("tab", { name: /Pending requests/ }).click();

async function openAllowlistTab(page, { allowlist = [], reqs = [], denied = [], gotoPending = true } = {}) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAllowlist(page, allowlist);
  await mockAccessRequests(page, reqs);
  await mockDeniedRequests(page, denied);
  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  if (gotoPending) await openPending(page);
}

const TWO_PENDING = [
  { id: 1, email: "one@example.edu", reason: null, status: "pending", created_at: 1_700_000_000 },
  { id: 2, email: "two@example.edu", reason: null, status: "pending", created_at: 1_700_000_100 },
];

test.describe("pending access requests table", () => {
  test("Approve + Reject icon actions render per pending row", async ({ page }) => {
    await openAllowlistTab(page, { reqs: TWO_PENDING });

    for (const addr of ["one@example.edu", "two@example.edu"]) {
      await expect(page.getByRole("button", { name: `Approve request from ${addr}` })).toBeVisible();
      await expect(page.getByRole("button", { name: `Reject request from ${addr}` })).toBeVisible();
    }
    // Email shows in the table; the section is a proper table with an Actions column.
    await expect(page.getByRole("table", { name: "Pending access requests" })).toBeVisible();
    await expect(page.getByRole("cell", { name: "one@example.edu", exact: true })).toBeVisible();
  });

  test("the Pending tab's count badge carries the attention tone; the zero-state shows when empty", async ({ page }) => {
    await openAllowlistTab(page, { reqs: TWO_PENDING });
    // The count lives on the sub-tab; its accent "attention" tone (not an error
    // look) shows while requests await, and an SR-only line announces the count.
    const pendingTab = page.getByRole("tab", { name: /Pending requests/ });
    await expect(pendingTab).toContainText("2");
    await expect(pendingTab.locator(".usertab-badge.attention")).toBeVisible();
    await expect(page.getByText("2 access requests awaiting review")).toBeAttached();

    // With nothing pending: the badge drops its attention tone (neutral inactive
    // styling), and the pending panel shows the clear zero-state message.
    await openAllowlistTab(page, { reqs: [] });
    const emptyTab = page.getByRole("tab", { name: /Pending requests/ });
    await expect(emptyTab.locator(".usertab-badge.attention")).toHaveCount(0);
    await expect(page.getByText("No access requests are awaiting review.")).toBeVisible();
  });

  test("Reject: confirm -> POST fires the deny for the right address", async ({ page }) => {
    const deny = await mockDenyAccessRequest(page, { httpStatus: 200 });
    await openAllowlistTab(page, { reqs: TWO_PENDING });

    await page.getByRole("button", { name: "Reject request from one@example.edu" }).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("one@example.edu");
    await expect(dialog).toContainText(/block/i);
    await dialog.getByRole("button", { name: "Reject request" }).click();

    await expect.poll(() => deny.calls.length).toBe(1);
    expect(deny.calls[0]).toBe("one@example.edu");
  });

  test("Reject: cancelling the confirm modal fires no POST", async ({ page }) => {
    const deny = await mockDenyAccessRequest(page, { httpStatus: 200 });
    await openAllowlistTab(page, { reqs: TWO_PENDING });

    await page.getByRole("button", { name: "Reject request from one@example.edu" }).click();
    const dialog = page.getByRole("alertdialog");
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toHaveCount(0);
    await page.waitForTimeout(200);
    expect(deny.calls.length).toBe(0);
  });

  test("Reject: a failed deny shows an error toast + in-modal error and stays recoverable", async ({ page }) => {
    await mockDenyAccessRequest(page, { httpStatus: 500 });
    await openAllowlistTab(page, { reqs: TWO_PENDING });

    await page.getByRole("button", { name: "Reject request from one@example.edu" }).click();
    const dialog = page.getByRole("alertdialog");
    await dialog.getByRole("button", { name: "Reject request" }).click();

    await expect(page.locator(".toast")).toContainText(/Could not reject/i);
    await expect(dialog).toBeVisible();
    await expect(dialog.locator(".notice.error")).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Reject request from two@example.edu" })).toBeEnabled();
  });

  test("Reject: on success the row leaves the table and focus lands on the pending search", async ({ page }) => {
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockConversations(page, []);
    await mockAllowlist(page, []);
    await mockDeniedRequests(page, []);
    await mockDenyAccessRequest(page, { httpStatus: 200 });

    let call = 0;
    await page.route("**/api/admin/access-requests", async (route) => {
      call += 1;
      const body = call === 1 ? TWO_PENDING.slice(0, 1) : [];
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    });

    await page.goto("/");
    await page.getByRole("link", { name: "Admin" }).click();
    await openPending(page);
    await expect(page.getByRole("cell", { name: "one@example.edu", exact: true })).toBeVisible();

    await page.getByRole("button", { name: "Reject request from one@example.edu" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Reject request" }).click();

    // The last pending row is gone -> the table shows its zero-state but STAYS
    // mounted (it's the always-present Pending tab now), so focus lands on the
    // pending search box, never dropping to <body> (WCAG 2.4.3).
    await expect(page.getByText("No access requests are awaiting review.")).toBeVisible();
    await expect(page.getByRole("button", { name: /Reject request from/ })).toHaveCount(0);
    await expect(page.getByRole("searchbox", { name: "Search pending requests by email" })).toBeFocused();
  });

  test("Approve: confirm -> POST allowlist for the right address + success toast", async ({ page }) => {
    const allow = await mockAllowlist(page, [], { postBody: { ok: true, delivery: "emailed" } });
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockConversations(page, []);
    await mockAccessRequests(page, TWO_PENDING);
    await mockDeniedRequests(page, []);
    await page.goto("/");
    await page.getByRole("link", { name: "Admin" }).click();
    await openPending(page);

    await page.getByRole("button", { name: "Approve request from one@example.edu" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("one@example.edu");
    await dialog.getByRole("button", { name: "Approve access" }).click();

    await expect.poll(() => allow.posts.length).toBe(1);
    expect(allow.posts[0].email).toBe("one@example.edu");
    // The stored note is an audit trail derived from the acting admin (me.email)
    // and today's (locale-formatted) date -- not the old static "approved request"
    // string. Date shape is locale-dependent, so match structurally.
    expect(allow.posts[0].note).toMatch(/^approved on .+ by admin@example\.edu$/);
    await expect(page.locator(".toast")).toContainText(/approval email was sent to one@example.edu/i);
  });

  test("Approve: cancelling fires no allowlist POST", async ({ page }) => {
    const allow = await mockAllowlist(page, []);
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockConversations(page, []);
    await mockAccessRequests(page, TWO_PENDING);
    await mockDeniedRequests(page, []);
    await page.goto("/");
    await page.getByRole("link", { name: "Admin" }).click();
    await openPending(page);

    await page.getByRole("button", { name: "Approve request from one@example.edu" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
    await page.waitForTimeout(200);
    expect(allow.posts.length).toBe(0);
  });
});
