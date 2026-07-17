import { test, expect } from "@playwright/test";
import { mockMe, mockVerifyInfo, mockVerify, mockConversations } from "./mocks.js";

// /verify must behave EXACTLY as it does today once react-router-dom owns
// top-level routing: strip `token` from the URL/history on load, and never
// call GET /api/auth/me while the confirm page is up (a naive router setup
// that always fires the "am I logged in" check on mount, instead of carving
// /verify out first like the current isVerifyRoute() guard in App.jsx, would
// regress this). See auth-verify.spec.js for the pre-existing behavioral
// coverage (peek-then-confirm token flow) this file does not duplicate.

test.describe("/verify under the router", () => {
  test("never calls GET /api/auth/me while on /verify, strips the token, and lands on / after sign-in", async ({ page }) => {
    let meCalls = 0;
    await page.route("**/api/auth/me", async (route) => {
      meCalls += 1;
      await route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "unauthorized" }) });
    });
    await mockVerifyInfo(page, "prof@example.edu");
    const verify = await mockVerify(page, { is_admin: false });

    await page.goto("/verify?token=tok-abc-123");

    await expect(page.getByText("prof@example.edu")).toBeVisible();
    await expect.poll(() => new URL(page.url()).search).toBe("");
    expect(meCalls).toBe(0);

    // Re-point /me to a signed-in response for the post-sign-in reload, then
    // click through and confirm neither the URL nor a stray /me call
    // happened before that point.
    await mockMe(page, { email: "prof@example.edu", is_admin: false });
    await mockConversations(page, []);
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect.poll(() => verify.calls.length).toBe(1);
    await expect.poll(() => new URL(page.url()).pathname).toBe("/");
    await expect(page.getByRole("link", { name: "Chat", exact: true })).toBeVisible();
  });
});
