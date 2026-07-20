import { test, expect } from "@playwright/test";
import { mockMe, mockLogout, mockConversations, mockAttention } from "./mocks.js";

// Flow 2: app shell / roles. Admin-only surfaces render only when
// GET /api/auth/me returns is_admin: true (App.jsx). Since the redesign, the
// Admin surface is a menu item under the user badge, not a top-bar tab; the
// signed-in email is surfaced inside that menu. (The menu's keyboard/About/theme
// mechanics live in user-menu.spec.js — this file guards the role gating + email.)
const avatar = (page) => page.getByRole("button", { name: /Account menu/ });

test.describe("app shell / roles", () => {
  test("non-admin: the menu surfaces the email but has no Admin item", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);

    await page.goto("/");
    await avatar(page).click();

    await expect(page.getByText("user@example.edu")).toBeVisible();
    await expect(page.getByRole("menuitem", { name: "Admin" })).toHaveCount(0);
  });

  test("admin: the menu has an Admin item", async ({ page }) => {
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockConversations(page, []);
    await mockAttention(page, { users: 0, skills: 0, logs: 0 });

    await page.goto("/");
    await avatar(page).click();

    await expect(page.getByRole("menuitem", { name: "Admin" })).toBeVisible();
  });

  test("sign out calls logout and returns to the Login screen", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    const logout = await mockLogout(page);

    await page.goto("/");
    // App.jsx's sign-out handler awaits api.logout() then sets user to null
    // directly (it does not re-call /api/auth/me), so Login renders immediately.
    await avatar(page).click();
    await page.getByRole("menuitem", { name: "Sign out" }).click();

    await expect(page.getByRole("heading", { name: "IPEDS Oracle" })).toBeVisible();
    expect(logout.calls.length).toBe(1);
  });
});
