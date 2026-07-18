import { test, expect } from "@playwright/test";
import { mockMe, mockConversations, mockAccessRequests, mockDeniedRequests } from "./mocks.js";

// Browser truth for the app-wide toast (Toast.jsx). The Allowlist action copy is
// pinned elsewhere (admin-allowlist-flash.spec.js); here we cover the toast
// MECHANISM: it appears on an action, carries the right semantic color, and is
// manually dismissable — driven through a promote/demote so we don't depend on
// any one message.

async function openUsers(page, rows, { patchStatus = 200 } = {}) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAccessRequests(page, []);
  await mockDeniedRequests(page, []);
  let current = [...rows];
  await page.route("**/api/admin/allowlist", (r) =>
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(current) }));
  await page.route("**/api/admin/allowlist/*", (route) => {
    const email = decodeURIComponent(route.request().url().split("/allowlist/")[1].split("?")[0]);
    if (route.request().method() === "PATCH" && patchStatus === 200) {
      const body = route.request().postDataJSON();
      current = current.map((u) => (u.email === email ? { ...u, is_admin: body.is_admin } : u));
      return route.fulfill({ status: 200, contentType: "application/json",
        body: JSON.stringify({ ok: true, email, is_admin: body.is_admin }) });
    }
    return route.fulfill({ status: patchStatus, contentType: "application/json",
      body: JSON.stringify({ detail: "nope" }) });
  });
  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await expect(page.getByRole("heading", { name: "Users" })).toBeVisible();
}

const ONE = [{ email: "colleague@example.edu", note: "staff", is_admin: 0, last_login: null }];

test("a successful action shows an ok-colored toast that can be dismissed", async ({ page }) => {
  await openUsers(page, ONE);
  await page.getByRole("button", { name: "Promote admin" }).click();

  const toast = page.locator(".toast");
  await expect(toast).toHaveClass(/\bok\b/);
  await expect(toast).toContainText("is now an admin");

  await toast.getByRole("button", { name: "Dismiss" }).click();
  await expect(toast).toHaveCount(0);
});

test("a failed action shows an error-colored toast", async ({ page }) => {
  await openUsers(page, ONE, { patchStatus: 500 });
  await page.getByRole("button", { name: "Promote admin" }).click();

  const toast = page.locator(".toast");
  await expect(toast).toHaveClass(/\berror\b/);
  await expect(toast).toContainText(/could not|nope/i);
});

test("dismissing a mid-stack toast hands focus to a sibling, not <body>", async ({ page }) => {
  // Two persistent error toasts (errors don't auto-dismiss); dismissing the
  // first must move focus to the second's Dismiss button, never drop to <body>.
  await openUsers(page, [
    { email: "aa@example.edu", note: "", is_admin: 0, last_login: null },
    { email: "bb@example.edu", note: "", is_admin: 0, last_login: null },
  ], { patchStatus: 500 });

  await page.getByRole("row", { name: /aa@example\.edu/ }).getByRole("button", { name: "Promote admin" }).click();
  await page.getByRole("row", { name: /bb@example\.edu/ }).getByRole("button", { name: "Promote admin" }).click();

  const dismissers = page.getByRole("button", { name: "Dismiss" });
  await expect(dismissers).toHaveCount(2);
  await dismissers.first().click();
  await expect(dismissers).toHaveCount(1);
  await expect(dismissers).toBeFocused();
});
