import { test, expect } from "@playwright/test";
import { mockMe, mockConversations, mockAllowlist, mockAccessRequests } from "./mocks.js";

// The signed-in admin can promote/demote others, but their OWN admin row is a
// non-interactive "✓ admin (you)" label — you can't demote yourself from the UI.
test("admin cannot demote themselves from the allowlist UI", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAllowlist(page, [
    { email: "admin@example.edu", note: "owner", is_admin: true, last_login: 1700000000 },
    { email: "colleague@example.edu", note: "staff", is_admin: false, last_login: 1700000000 },
  ]);
  await mockAccessRequests(page, []);

  await page.goto("/");
  await page.getByRole("button", { name: "Admin" }).click();

  // Own row: a plain "(you)" label, NOT a toggle button.
  await expect(page.getByText("✓ admin (you)")).toBeVisible();
  await expect(
    page.getByRole("button", { name: /admin \(you\)/i }),
  ).toHaveCount(0);

  // Another user: a working "make admin" toggle button is present.
  await expect(page.getByRole("button", { name: "make admin" })).toBeVisible();
});
