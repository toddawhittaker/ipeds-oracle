import { test, expect } from "@playwright/test";
import { mockMe, mockRequestLink, mockAuthConfig } from "./mocks.js";

// Flow 1: auth/login. With GET /api/auth/me returning 401 (or any non-200),
// App.jsx treats the user as logged out and renders <Login/>.
test.describe("auth / login", () => {
  test("renders the login card when logged out, and requesting a link shows the notice", async ({ page }) => {
    await mockMe(page, null);
    await mockAuthConfig(page, "example.edu");
    await mockRequestLink(page, "Check your email for a sign-in link.");

    await page.goto("/");

    await expect(page.getByRole("heading", { name: "IPEDS Query" })).toBeVisible();
    await expect(
      page.getByText("Access is by invitation.", { exact: false })
    ).toBeVisible();

    // NOTE (a11y finding for the implementer): the email <input> in
    // web/src/Login.jsx has no associated <label> or aria-label, so
    // getByLabel() can't be used here. getByPlaceholder is the only stable
    // selector available today; adding a <label htmlFor="email"> would let
    // this move to the more robust getByLabel('Email'). The placeholder text
    // itself is driven by GET /api/auth/config's email_domain once it
    // resolves (see web/src/Login.jsx) — mocked here to "example.edu".
    const emailInput = page.getByPlaceholder("you@example.edu");
    await expect(emailInput).toBeVisible();

    await emailInput.fill("admin@example.edu");
    await page.getByRole("button", { name: "Email me a sign-in link" }).click();

    // Form is replaced by the .notice message on success.
    await expect(page.getByText("Check your email for a sign-in link.")).toBeVisible();
    await expect(emailInput).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Email me a sign-in link" })).toHaveCount(0);
  });

  test("shows a generic error notice when the request fails", async ({ page }) => {
    await mockMe(page, null);
    await mockAuthConfig(page, "example.edu");
    await page.route("**/api/auth/request", async (route) => {
      await route.fulfill({ status: 500, contentType: "text/plain", body: "boom" });
    });

    await page.goto("/");
    await page.getByPlaceholder("you@example.edu").fill("someone@example.edu");
    await page.getByRole("button", { name: "Email me a sign-in link" }).click();

    await expect(page.getByText("Something went wrong. Please try again.")).toBeVisible();
  });

  test("falls back to the generic hint when EMAIL_DOMAIN is unset", async ({ page }) => {
    await mockMe(page, null);
    await mockAuthConfig(page, "");

    await page.goto("/");

    // An empty email_domain must not override the generic fallback hint.
    await expect(page.getByPlaceholder("you@yourschool.edu")).toBeVisible();
  });

  test("falls back to the generic hint when /api/auth/config is unavailable", async ({ page }) => {
    await mockMe(page, null);
    await page.route("**/api/auth/config", async (route) => {
      await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "boom" }) });
    });

    await page.goto("/");

    // The fetch rejects (non-2xx) and Login.jsx's .catch(() => {}) leaves the
    // module-level FALLBACK_HINT in place — the field stays usable either way.
    await expect(page.getByPlaceholder("you@yourschool.edu")).toBeVisible();
  });
});
