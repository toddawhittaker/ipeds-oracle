import { test, expect } from "@playwright/test";
import { mockMe, mockLogout, mockConversations } from "./mocks.js";

// Flow 2: app shell / roles. The Admin tab only renders when
// GET /api/auth/me returns is_admin: true (App.jsx).
test.describe("app shell / roles", () => {
  test("non-admin sees Chat but no Admin tab", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);

    await page.goto("/");

    // exact: true — "Chat" is otherwise a substring match of "+ New chat" in the sidebar.
    await expect(page.getByRole("link", { name: "Chat", exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "Admin" })).toHaveCount(0);
    await expect(page.getByText("user@example.edu")).toBeVisible();
  });

  test("admin sees the Admin tab", async ({ page }) => {
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockConversations(page, []);

    await page.goto("/");

    await expect(page.getByRole("link", { name: "Admin" })).toBeVisible();
  });

  test("sign out calls logout and returns to the Login screen", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    const logout = await mockLogout(page);

    await page.goto("/");
    await expect(page.getByText("user@example.edu")).toBeVisible();

    // App.jsx's sign-out handler awaits api.logout() then sets user to null
    // directly (it does not re-call /api/auth/me), so Login renders immediately.
    await page.getByRole("button", { name: "Sign out" }).click();

    await expect(page.getByRole("heading", { name: "IPEDS Oracle" })).toBeVisible();
    expect(logout.calls.length).toBe(1);
  });
});
