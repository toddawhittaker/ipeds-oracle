import { test, expect } from "@playwright/test";
import { mockMe, mockConversations } from "./mocks.js";

// The wordmark logo replaces the plain "IPEDS Query" text on the login screen
// and in the chat top bar. It's an image with an accessible name so screen
// readers still announce the brand; the favicon is wired in the document head.

test("login screen shows the wordmark with an accessible name", async ({ page }) => {
  await page.route("**/api/auth/me", (r) => r.fulfill({ status: 401, body: "{}" }));
  await page.goto("/");
  // the wordmark carries the brand's accessible name (role=img + aria-label)
  await expect(page.getByRole("img", { name: "IPEDS Query" })).toBeVisible();
  // and the heading's accessible name is still "IPEDS Query" (from the wordmark)
  await expect(page.getByRole("heading", { name: "IPEDS Query" })).toBeVisible();
});

test("chat top bar shows the wordmark", async ({ page }) => {
  await mockMe(page, { email: "user@example.edu", is_admin: false });
  await mockConversations(page, []);
  await page.goto("/");
  await expect(page.getByText("user@example.edu")).toBeVisible();
  await expect(page.getByRole("img", { name: "IPEDS Query" })).toBeVisible();
});

test("favicon is wired into the document head", async ({ page }) => {
  await page.route("**/api/auth/me", (r) => r.fulfill({ status: 401, body: "{}" }));
  await page.goto("/");
  const iconHref = await page.locator('link[rel="icon"]').first().getAttribute("href");
  expect(iconHref).toContain("favicon");
  await expect(page.locator('link[rel="apple-touch-icon"]')).toHaveCount(1);
});
