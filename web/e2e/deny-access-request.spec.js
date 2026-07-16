import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockAllowlist,
  mockAccessRequests,
  mockDenyAccessRequest,
} from "./mocks.js";

// Reject an access request (POST /api/admin/access-requests/{email}/deny).
// Not implemented yet -- Admin.jsx's Allowlist component has no Reject
// button, so every test below is expected to fail red until the implementer
// ships it. Contract (see .plan-deny.md):
//   * a Reject button, aria-label `Reject the access request from {email}`,
//     renders next to Approve for each pending request row.
//   * clicking it asks window.confirm, and only on accept POSTs
//     /api/admin/access-requests/{email}/deny for that address.
//   * dismissing the confirm fires no request.
//   * a failed deny (non-2xx) surfaces a `.notice` and does not wedge the UI.
//   * a successful deny reloads the pending-requests list.

async function openAllowlistTab(page, { allowlist = [], reqs = [] } = {}) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAllowlist(page, allowlist);
  await mockAccessRequests(page, reqs);
  await page.goto("/");
  await page.getByRole("button", { name: "Admin" }).click();
}

const TWO_PENDING = [
  { id: 1, email: "one@example.edu", reason: null, status: "pending", created_at: 1_700_000_000 },
  { id: 2, email: "two@example.edu", reason: null, status: "pending", created_at: 1_700_000_100 },
];

test.describe("reject an access request", () => {
  test("a Reject button renders beside Approve for each pending request", async ({ page }) => {
    await openAllowlistTab(page, { reqs: TWO_PENDING });

    await expect(
      page.getByRole("button", { name: "Reject the access request from one@example.edu" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Reject the access request from two@example.edu" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: /^Reject the access request from/ }),
    ).toHaveCount(2);

    // Approve is still there too -- Reject is additive, not a replacement.
    await expect(page.getByRole("button", { name: "Approve" }).first()).toBeVisible();
  });

  test("confirm -> POST fires the deny for the right address", async ({ page }) => {
    const deny = await mockDenyAccessRequest(page, { httpStatus: 200 });
    await openAllowlistTab(page, { reqs: TWO_PENDING });

    let dialogMessage = "";
    page.once("dialog", (dialog) => {
      dialogMessage = dialog.message();
      dialog.accept();
    });
    await page.getByRole("button", { name: "Reject the access request from one@example.edu" }).click();

    expect(dialogMessage).toMatch(/reject|request access again|allowlist/i);

    await expect.poll(() => deny.calls.length).toBe(1);
    expect(deny.calls[0]).toBe("one@example.edu");
  });

  test("dismissing the confirm dialog does not fire a POST", async ({ page }) => {
    const deny = await mockDenyAccessRequest(page, { httpStatus: 200 });
    await openAllowlistTab(page, { reqs: TWO_PENDING });

    page.once("dialog", (dialog) => dialog.dismiss());
    await page.getByRole("button", { name: "Reject the access request from one@example.edu" }).click();
    await page.waitForTimeout(200);

    expect(deny.calls.length).toBe(0);
  });

  test("a failed deny surfaces a notice and doesn't wedge the UI", async ({ page }) => {
    await mockDenyAccessRequest(page, { httpStatus: 500 });
    await openAllowlistTab(page, { reqs: TWO_PENDING });

    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "Reject the access request from one@example.edu" }).click();

    await expect(page.locator(".notice")).toContainText(/Could not reject/i);
    // The UI must still be responsive -- the other row's Reject button works.
    await expect(
      page.getByRole("button", { name: "Reject the access request from two@example.edu" }),
    ).toBeEnabled();
  });

  test("the list refreshes after a successful deny", async ({ page }) => {
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockConversations(page, []);
    await mockAllowlist(page, []);
    await mockDenyAccessRequest(page, { httpStatus: 200 });

    let call = 0;
    await page.route("**/api/admin/access-requests", async (route) => {
      call += 1;
      const body = call === 1 ? TWO_PENDING.slice(0, 1) : [];
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    });

    await page.goto("/");
    await page.getByRole("button", { name: "Admin" }).click();
    // Scope to the pending-request ROW (`.req`, per Admin.jsx), not the whole
    // page: a page-wide getByText would also match anything else on the page
    // that happens to contain this address (e.g. a flash message naming who
    // was rejected) and over-specify a UI decision this test isn't about.
    // The intent here is narrower and purely structural: the pending-request
    // row for this address is gone after a successful deny + reload.
    const pendingRow = page.locator(".req", { hasText: "one@example.edu" });
    await expect(pendingRow).toBeVisible();

    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "Reject the access request from one@example.edu" }).click();

    await expect(pendingRow).toHaveCount(0);
  });
});
