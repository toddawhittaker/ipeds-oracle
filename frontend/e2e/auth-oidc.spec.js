import { expect, test } from "@playwright/test";

import { mockAuthConfig, mockMe, mockOidcStart } from "./mocks.js";

// Browser truth for the SSO door. The pure half — which method a config maps to,
// and what each error code says — is vitest's (loginmethod.test.js,
// authcopy.test.js). What needs a real browser is what those decisions DO: which
// form renders, that clicking navigates to the provider, and that an error code
// in the URL is both shown and cleaned away.

test.describe("the SSO door", () => {
  test.beforeEach(async ({ page }) => {
    await mockMe(page, null);
  });

  test("renders one SSO button and NO email field", async ({ page }) => {
    // The regression: rendering the magic-link form on a deployment that cannot
    // send magic links. Someone types an address, gets a promise of an email,
    // and waits for a link that is never coming.
    await mockAuthConfig(page, { authMethod: "oidc", ssoLabel: "Sign in with NetID" });
    await page.goto("/");

    await expect(page.getByRole("button", { name: "Sign in with NetID" })).toBeVisible();
    await expect(page.getByPlaceholder(/you@/)).toHaveCount(0);
    await expect(page.getByRole("button", { name: /Email me a sign-in link/ }))
      .toHaveCount(0);
    await expect(page.getByText(/single sign-on/i)).toBeVisible();
  });

  test("clicking asks the server once, then navigates to the provider",
    async ({ page }) => {
      const start = await mockOidcStart(page, {
        url: "https://idp.example.test/authorize?response_type=code&state=ST",
      });
      await mockAuthConfig(page, { authMethod: "oidc" });
      await page.goto("/");

      // The provider is a foreign origin the test cannot load, so watch the
      // attempted navigation rather than waiting for it to succeed.
      const navigated = page.waitForRequest(
        (r) => r.url().startsWith("https://idp.example.test/authorize"),
        { timeout: 5000 }).catch(() => null);

      await page.getByRole("button", { name: "Sign in with SSO" }).click();
      const req = await navigated;

      expect(start.calls).toHaveLength(1);
      expect(req, "the browser never went to the provider").toBeTruthy();
    });

  test("a failed start renders a message instead of stranding the visitor",
    async ({ page }) => {
      // A detail the CLIENT fallback does not contain. Matching /unavailable/i
      // would pass with `setMsg(e?.detail || …)` regressed to the bare
      // fallback, since both sentences carry that word.
      await mockOidcStart(page, {
        status: 502, detail: "Your administrator has not finished setting up SSO.",
      });
      await mockAuthConfig(page, { authMethod: "oidc" });
      await page.goto("/");

      await page.getByRole("button", { name: "Sign in with SSO" }).click();
      await expect(page.getByRole("alert"))
        .toContainText("Your administrator has not finished setting up SSO.");
      // Still usable AND still focusable: the button uses aria-disabled rather
      // than `disabled`, because disabling the control someone just activated
      // sends focus to <body>, and this is the card's only control.
      await expect(page.getByRole("button", { name: "Sign in with SSO" }))
        .toHaveAttribute("aria-disabled", "false");
    });

  test("an auth_error in the URL is shown and then cleaned away", async ({ page }) => {
    // Same hygiene Verify.jsx applies to its token: the code has been read, so a
    // reload or a copied link must not re-raise an error already dealt with.
    await mockAuthConfig(page, { authMethod: "oidc" });
    await page.goto("/?auth_error=not_authorized");

    await expect(page.getByRole("alert")).toContainText(/administrator/i);
    await expect.poll(() => new URL(page.url()).search).toBe("");
  });

  test("an unrecognised auth_error renders our words, never its own",
    async ({ page }) => {
      await mockAuthConfig(page, { authMethod: "oidc" });
      await page.goto("/?auth_error=%3Cscript%3Ealert(1)%3C%2Fscript%3E");

      const alert = page.getByRole("alert");
      await expect(alert).toBeVisible();
      await expect(alert).not.toContainText("script");
      await expect(alert).toContainText(/try again|administrator/i);
    });

  test("the SSO failure message takes focus so it is announced", async ({ page }) => {
    // The message is present in the first paint, and a role="alert" whose text
    // is already there when it mounts is announced unreliably. Focus is what
    // makes it reach a screen reader — and it is where a keyboard user needs to
    // be, next to the button they are about to press again.
    await mockAuthConfig(page, { authMethod: "oidc" });
    await page.goto("/?auth_error=denied");
    await expect(page.getByRole("alert")).toBeFocused();
  });
});
