import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockVerifyInfo,
  mockVerify,
  mockConversations,
} from "./mocks.js";

// Flow: the magic-link email points at /verify?token=…. The page peeks
// (non-consuming) to name the account, then a deliberate "Sign in" click POSTs
// the token to consume it. A GET/prefetch of the link therefore never burns it.
test.describe("auth / verify (magic-link confirm)", () => {
  test("confirms the account, then signs in on click", async ({ page }) => {
    await mockVerifyInfo(page, "prof@franklin.edu");
    const verify = await mockVerify(page, { is_admin: false });
    // After sign-in the page reloads to "/"; be ready to render signed-in.
    await mockMe(page, { email: "prof@franklin.edu", is_admin: false });
    await mockConversations(page, []);

    await page.goto("/verify?token=tok-abc-123");

    // Names the account and shows a confirm button (no auto-consume).
    await expect(page.getByText("prof@franklin.edu")).toBeVisible();
    const signIn = page.getByRole("button", { name: "Sign in" });
    await expect(signIn).toBeVisible();

    // Token must be stripped from the URL immediately (not left in history).
    await expect.poll(() => new URL(page.url()).search).toBe("");

    // Nothing consumed until the deliberate click.
    expect(verify.calls).toHaveLength(0);

    await signIn.click();

    // The click POSTs the token verbatim…
    await expect.poll(() => verify.calls.length).toBe(1);
    expect(verify.calls[0]).toEqual({ token: "tok-abc-123" });

    // …and we land in the signed-in app shell.
    await expect(page.getByRole("button", { name: "Chat", exact: true })).toBeVisible();
    await expect(page.getByText("prof@franklin.edu")).toBeVisible();
  });

  test("shows an error for an invalid or expired link", async ({ page }) => {
    await mockVerifyInfo(page, null, { status: 400 });

    await page.goto("/verify?token=dead-token");

    await expect(page.getByRole("alert")).toContainText(
      "invalid or has expired"
    );
    await expect(page.getByRole("link", { name: "Return to sign in" })).toBeVisible();
    // No confirm button when the link is bad.
    await expect(page.getByRole("button", { name: "Sign in" })).toHaveCount(0);
  });
});
