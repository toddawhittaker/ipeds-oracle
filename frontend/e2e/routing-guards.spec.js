import { test, expect } from "@playwright/test";
import { mockMe, mockConversations } from "./mocks.js";

// Route guards that don't depend on onboarding state (see
// no-data-onboarding.spec.js for the has_data:false-specific routing
// contract). Written before the feature exists (TDD): expected RED against
// the current (routerless) App.jsx, which doesn't look at window.location.pathname
// at all outside of /verify.

test.describe("route guards", () => {
  test("a non-admin deep-linking /admin/users lands on / with no Admin panel and fires no admin API request", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    const adminRequests = [];
    page.on("request", (req) => {
      if (/\/api\/admin\//.test(new URL(req.url()).pathname)) adminRequests.push(req.url());
    });

    await page.goto("/admin/users");

    await expect.poll(() => new URL(page.url()).pathname).toBe("/");
    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Users" })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Server logs" })).toHaveCount(0);
    await page.waitForTimeout(200); // let any errant admin fetch land before asserting zero
    expect(adminRequests).toEqual([]);
  });

  test("a non-admin deep-linking bare /admin also lands on / with no admin API request", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    const adminRequests = [];
    page.on("request", (req) => {
      if (/\/api\/admin\//.test(new URL(req.url()).pathname)) adminRequests.push(req.url());
    });

    await page.goto("/admin");

    await expect.poll(() => new URL(page.url()).pathname).toBe("/");
    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeVisible();
    await page.waitForTimeout(200);
    expect(adminRequests).toEqual([]);
  });

  test("an entirely unknown path redirects to /", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);

    await page.goto("/totally/unknown");

    await expect.poll(() => new URL(page.url()).pathname).toBe("/");
    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeVisible();
  });
});
