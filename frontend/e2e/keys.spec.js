import { test, expect } from "@playwright/test";
import { mockApiKeys, mockConversations, mockMe, mockVersion } from "./mocks.js";

// Browser truth for a user's own API keys at /keys. The pure display + ordering
// logic is unit-tested in frontend/src/apikeys.test.js (vitest); here we cover
// what only a browser gives: reaching the page from the account menu, the
// one-shot reveal dialog and the fact the value is gone after a reload, and the
// revoke path through the shared confirm modal.

const KEY = { id: 7, last4: "9f2a", label: "Work laptop", created_at: 1_700_000_000,
              created_by: null, last_used_at: 1_700_000_400, revoked_at: null };

async function openKeys(page, rows = [], opts) {
  await mockMe(page, { email: "user@example.edu", is_admin: false });
  await mockVersion(page);
  await mockConversations(page, []);
  const api = await mockApiKeys(page, rows, opts);
  await page.goto("/keys");
  await expect(page.getByRole("heading", { name: "API keys" })).toBeVisible();
  return api;
}

test("the account menu reaches /keys, and the page announces the navigation", async ({ page }) => {
  await mockMe(page, { email: "user@example.edu", is_admin: false });
  await mockVersion(page);
  await mockConversations(page, []);
  await mockApiKeys(page, [KEY]);
  await page.goto("/");

  await page.getByRole("button", { name: /Account menu/ }).click();
  await page.getByRole("menuitem", { name: "API keys" }).click();

  await expect.poll(() => new URL(page.url()).pathname).toBe("/keys");
  await expect(page.getByRole("heading", { name: "API keys" })).toBeVisible();
  // Swapping the main content is a silent navigation without this — the same
  // gap the announcer exists for on Chat <-> Admin.
  await expect(page.getByTestId("route-announcer")).toContainText(/api keys/i);
});

test("a listed key shows only its last four characters", async ({ page }) => {
  await openKeys(page, [KEY]);
  // The identification aid, never the credential. The server sends four
  // characters and this is the screen a user is most likely to be screen-sharing
  // while asking for help.
  await expect(page.getByText("ipeds_mcp_…9f2a")).toBeVisible();
  await expect(page.getByText("Work laptop")).toBeVisible();
});

test("minting shows the raw key once, and a reload cannot get it back", async ({ page }) => {
  const secret = "ipeds_mcp_only-shown-once-abcd";
  const api = await openKeys(page, [], { secret });

  await page.getByLabel("Label for the new key").fill("CI runner");
  await page.getByRole("button", { name: "Create key" }).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByTestId("revealed-key")).toHaveText(secret);
  expect(api.mints).toEqual(["CI runner"]);

  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);

  // The row is there; the secret is not. This is the whole contract of the
  // screen: nothing stores the raw key, so a UI that could re-render it would
  // mean the server had kept it.
  await expect(page.getByText("CI runner")).toBeVisible();
  await expect(page.getByText(secret)).toHaveCount(0);

  await page.reload();
  await expect(page.getByText("CI runner")).toBeVisible();
  await expect(page.getByText(secret)).toHaveCount(0);
});

test("the reveal dialog traps focus and returns it to the page on Done", async ({ page }) => {
  await openKeys(page, []);
  const create = page.getByRole("button", { name: "Create key" });
  await create.click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toHaveAttribute("aria-modal", "true");
  // Focus lands inside the dialog, not on the button that is now behind an
  // inert background.
  await expect(dialog.getByRole("button", { name: "Done" })).toBeFocused();
  // The background really is inert, so nothing behind it is reachable.
  await expect(page.locator(".app")).toHaveAttribute("inert", "");

  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.locator(".app")).not.toHaveAttribute("inert", "");
  await expect(create).toBeFocused();
});

test("revoke goes through the confirm modal, toasts, and leaves the row listed as revoked",
  async ({ page }) => {
    const api = await openKeys(page, [KEY]);

    await page.getByRole("button", { name: /^Revoke key ipeds_mcp_…9f2a$/ }).click();

    const modal = page.getByRole("alertdialog");
    await expect(modal).toBeVisible();
    // Naming the key in the body is what makes the confirmation answerable —
    // "revoke this key?" with three keys on screen is not.
    await expect(modal).toContainText("ipeds_mcp_…9f2a");
    await modal.getByRole("button", { name: "Revoke key" }).click();

    await expect(page.locator(".toast-msg")).toHaveText("Key revoked.");
    // Revoked, not removed: the row is the record of what the withdrawn key
    // could reach, and its Revoke action is gone.
    await expect(page.locator(".keyrow-state")).toHaveText("Revoked");
    await expect(page.getByRole("button", { name: /^Revoke key/ })).toHaveCount(0);
    expect(api.getRows()[0].revoked_at).not.toBe(null);
  });

test("cancelling the confirm modal revokes nothing", async ({ page }) => {
  const api = await openKeys(page, [KEY]);

  await page.getByRole("button", { name: /^Revoke key/ }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Cancel" }).click();

  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  expect(api.getRows()[0].revoked_at).toBe(null);
  await expect(page.getByRole("button", { name: /^Revoke key/ })).toBeVisible();
});

test("a failed load renders a visible error, never an empty-looking key list", async ({ page }) => {
  await mockMe(page, { email: "user@example.edu", is_admin: false });
  await mockVersion(page);
  await mockConversations(page, []);
  await page.route("**/api/keys", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    return route.fulfill({ status: 500, contentType: "application/json",
      body: JSON.stringify({ detail: "The database is unavailable." }) });
  });
  await page.goto("/keys");

  // "You don't have any API keys yet" would be a confirmed fact the app does not
  // have — and it points the user at minting another key, the wrong fix.
  await expect(page.getByRole("alert")).toContainText("The database is unavailable.");
  await expect(page.getByText("You don’t have any API keys yet.")).toHaveCount(0);
});

test("a failed mint says so instead of opening an empty reveal dialog", async ({ page }) => {
  await openKeys(page, [], { httpStatus: 429, detail: "Too many keys. Revoke one first." });

  await page.getByRole("button", { name: "Create key" }).click();

  await expect(page.locator(".toast-msg")).toHaveText("Too many keys. Revoke one first.");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

// The clipboard, both ways. This value is unrecoverable, so "the copy button
// silently did nothing" is the one failure on this screen that costs the user
// the key itself — and `navigator.clipboard` really is undefined in a non-secure
// context, which is the documented self-host case (plain http on a LAN IP).
async function stubClipboard(page, { succeed }) {
  await page.addInitScript(({ ok }) => {
    globalThis.__copied = [];
    // Delete the real API so the execCommand fallback is the path under test,
    // exactly as it is over plain http.
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    globalThis.document.execCommand = (cmd) => {
      if (cmd === "copy") {
        globalThis.__copied.push(globalThis.document.activeElement?.value ?? "");
        return ok;
      }
      return false;
    };
  }, { ok: succeed });
}

test("the copy button reaches the clipboard through the non-secure-context fallback",
  async ({ page }) => {
    const secret = "ipeds_mcp_fallback-path-value-abcd";
    await stubClipboard(page, { succeed: true });
    await openKeys(page, [], { secret });

    await page.getByRole("button", { name: "Create key" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("button", { name: "Copy API key" }).click();

    expect(await page.evaluate(() => globalThis.__copied)).toEqual([secret]);
    // Announced, not only shown: the icon swap is silent to a screen reader and
    // there is no toast on the success path.
    await expect(dialog.locator("[aria-live=polite]")).toHaveText("API key copied.");
  });

test("a refused copy says so instead of silently failing", async ({ page }) => {
  await stubClipboard(page, { succeed: false });
  await openKeys(page, []);

  await page.getByRole("button", { name: "Create key" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Copy API key" }).click();

  // The wording names the escape hatch — select the text by hand — because the
  // dialog is the only place this value will ever appear.
  await expect(page.locator(".toast-msg")).toContainText("copy it manually");
  // ...and the dialog stays open, so that escape hatch is still reachable.
  await expect(page.getByRole("dialog")).toBeVisible();
});
