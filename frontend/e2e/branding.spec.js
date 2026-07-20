import { test, expect } from "@playwright/test";

// The wordmark logo replaces the plain "IPEDS Oracle" text on the login screen.
// It's an image with an accessible name so screen readers still announce the
// brand.

test("login screen shows the wordmark with an accessible name", async ({ page }) => {
  await page.route("**/api/auth/me", (r) => r.fulfill({ status: 401, body: "{}" }));
  await page.goto("/");
  // the wordmark carries the brand's accessible name (role=img + aria-label)
  await expect(page.getByRole("img", { name: "IPEDS Oracle" })).toBeVisible();
  // and the heading's accessible name is still "IPEDS Oracle" (from the wordmark)
  await expect(page.getByRole("heading", { name: "IPEDS Oracle" })).toBeVisible();
});
