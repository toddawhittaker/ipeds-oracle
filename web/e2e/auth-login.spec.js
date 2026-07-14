import { test, expect } from "@playwright/test";
import { mockMe, mockRequestLink } from "./mocks.js";

// Flow 1: auth/login. With GET /api/auth/me returning 401 (or any non-200),
// App.jsx treats the user as logged out and renders <Login/>.
test.describe("auth / login", () => {
  test("renders the login card when logged out, and requesting a link shows the notice", async ({ page }) => {
    await mockMe(page, null);
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
    // this move to the more robust getByLabel('Email').
    const emailInput = page.getByPlaceholder("you@franklin.edu");
    await expect(emailInput).toBeVisible();

    // Example prompts prime the user for what they can ask.
    await expect(
      page.getByText("Top 20 institutions awarding Associate's degrees", { exact: false })
    ).toBeVisible();
    await expect(
      page.getByText("Which states awarded the most Master's degrees in Education?", { exact: false })
    ).toBeVisible();

    await emailInput.fill("todd@thewhittakers.org");
    await page.getByRole("button", { name: "Email me a sign-in link" }).click();

    // Form is replaced by the .notice message on success.
    await expect(page.getByText("Check your email for a sign-in link.")).toBeVisible();
    await expect(emailInput).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Email me a sign-in link" })).toHaveCount(0);
  });

  test("shows a generic error notice when the request fails", async ({ page }) => {
    await mockMe(page, null);
    await page.route("**/api/auth/request", async (route) => {
      await route.fulfill({ status: 500, contentType: "text/plain", body: "boom" });
    });

    await page.goto("/");
    await page.getByPlaceholder("you@franklin.edu").fill("someone@franklin.edu");
    await page.getByRole("button", { name: "Email me a sign-in link" }).click();

    await expect(page.getByText("Something went wrong. Please try again.")).toBeVisible();
  });
});
