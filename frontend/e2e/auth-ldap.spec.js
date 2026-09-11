import { expect, test } from "@playwright/test";

import { mockAuthConfig, mockLdapSignIn, mockMe } from "./mocks.js";

// Browser truth for the directory door. The pure half — which config maps to
// which method — is loginmethod.test.js's.

test.describe("the LDAP door", () => {
  test.beforeEach(async ({ page }) => {
    await mockMe(page, null);
    await mockAuthConfig(page, { authMethod: "ldap" });
  });

  test("asks for a username and a password, not an email", async ({ page }) => {
    // The regression: rendering the magic-link form on a deployment that sends
    // no mail, so someone waits for a link that is never coming.
    await page.goto("/");
    await expect(page.getByPlaceholder("Username")).toBeVisible();
    await expect(page.getByPlaceholder("Password")).toBeVisible();
    await expect(page.getByRole("button", { name: /Email me a sign-in link/ }))
      .toHaveCount(0);
  });

  test("the password field is a real password field with the right autocomplete",
    async ({ page }) => {
      // Not cosmetic: type=text would show the password on screen and offer it
      // to autofill as a username, and password managers key on autocomplete.
      await page.goto("/");
      await expect(page.getByPlaceholder("Password"))
        .toHaveAttribute("type", "password");
      await expect(page.getByPlaceholder("Password"))
        .toHaveAttribute("autocomplete", "current-password");
      await expect(page.getByPlaceholder("Username"))
        .toHaveAttribute("autocomplete", "username");
    });

  test("submits once, sends what was typed, and reaches the signed-in app",
    async ({ page }) => {
      // The navigation half matters: without it a user posts valid credentials,
      // gets a session cookie, and sits on the form with the button flicked back
      // to "Sign in" and no message — a complete strand that a call-count
      // assertion alone cannot see.
      const ldap = await mockLdapSignIn(page);
      await page.goto("/");
      await page.getByPlaceholder("Username").fill("jdoe");
      await page.getByPlaceholder("Password").fill("s3cret");
      // The reload re-runs api.me(), so from here the user is signed in.
      await mockMe(page, { email: "jdoe@example.edu", is_admin: false });
      await page.getByRole("button", { name: "Sign in" }).click();

      await expect.poll(() => ldap.calls.length).toBe(1);
      expect(ldap.calls[0]).toEqual({ username: "jdoe", password: "s3cret" });
      await expect(page.getByRole("button", { name: /Account menu/ })).toBeVisible();
    });

  test("a second identical failure is still announced", async ({ page }) => {
    // The server answers every rejection with the SAME sentence by design, so
    // setting state to a value equal to itself makes React bail out and the
    // live region never changes. Repeated wrong passwords are the ordinary case
    // on this door, and a screen-reader user would hear nothing the second time.
    await mockLdapSignIn(page, { status: 401 });
    await page.goto("/");
    await page.getByPlaceholder("Username").fill("jdoe");
    await page.getByPlaceholder("Password").fill("wrong-once");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("alert")).toBeVisible();

    await page.getByPlaceholder("Password").fill("wrong-twice");
    await page.getByRole("button", { name: "Sign in" }).click();
    // The region must go away and come back, which is what makes it announce.
    await expect.poll(async () => page.getByRole("alert").count()).toBe(1);
    await expect(page.getByRole("alert"))
      .toContainText("Check your username and password");
  });

  test("a refusal shows the server's neutral message and stays usable",
    async ({ page }) => {
      await mockLdapSignIn(page, { status: 401 });
      await page.goto("/");
      await page.getByPlaceholder("Username").fill("jdoe");
      await page.getByPlaceholder("Password").fill("wrong");
      await page.getByRole("button", { name: "Sign in" }).click();

      await expect(page.getByRole("alert"))
        .toContainText("Check your username and password");
      // aria-disabled, not `disabled`: disabling the control someone just
      // pressed sends focus to <body>, and they are about to press it again.
      await expect(page.getByRole("button", { name: "Sign in" }))
        .toHaveAttribute("aria-disabled", "false");
      await expect(page.getByPlaceholder("Username")).toHaveValue("jdoe");
    });
});
