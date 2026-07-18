import { test, expect } from "@playwright/test";
import { mockMe, mockConversations, mockAllowlist, mockAccessRequests } from "./mocks.js";

// The signed-in admin can promote/demote others, but their OWN row shows a
// non-interactive "✓ Admin (you)" status and an EMPTY Actions cell — you can
// neither demote nor remove yourself from the UI (the backend also 400s both).
test("admin cannot demote or remove themselves from the allowlist UI", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAllowlist(page, [
    { email: "admin@example.edu", note: "owner", is_admin: true, last_login: 1700000000 },
    { email: "colleague@example.edu", note: "staff", is_admin: false, last_login: 1700000000 },
  ]);
  await mockAccessRequests(page, []);

  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();

  // Own row: a plain status label, and no self-directed action buttons at all.
  await expect(page.getByText("✓ Admin (you)")).toBeVisible();
  await expect(page.getByRole("button", { name: "Demote admin" })).toHaveCount(0);
  // The only Remove user button is the colleague's, never the signed-in admin's.
  await expect(page.getByRole("button", { name: "Remove user" })).toHaveCount(1);

  // Another user: the Promote admin action is present and labelled.
  await expect(page.getByRole("button", { name: "Promote admin" })).toBeVisible();
});
