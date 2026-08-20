import { test, expect } from "@playwright/test";
import {
  mockMe, mockVersion, mockAttention, mockConversations, mockAllowlist,
  mockAccessRequests, mockDeniedRequests, mockLogs,
} from "./mocks.js";

// Every admin search field is the SAME control (frontend/src/SearchBox.jsx).
//
// Admin -> Logs used to be a bare `type="search"` input leaning on the browser's
// own clear affordance, and that is not the same control: Chromium draws a bold
// blue ✕ where the app draws a muted grey one, Firefox draws nothing at all, and
// a role query for a "Clear search" button found ZERO on that page — so the
// control a screen-reader or keyboard user could reach on the Users table simply
// did not exist on Logs.
//
// This is browser truth on purpose: the defect lives in the rendered control and
// its focus behaviour, which jsdom would fake and get wrong.
//
// There are deliberately NO Escape-to-clear checks here. Chromium clears a
// `type="search"` field on Escape by itself, so such a test passes identically
// with SearchBox's own handler deleted — measured, by deleting it. The handler
// still earns its place (Firefox does not do this), but the guarantee is a
// one-browser suite away from being observable, and a test that cannot fail
// reads as coverage while providing none.

async function adminMocks(page) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockVersion(page);
  await mockAttention(page, { users: 0, skills: 0, logs: 0 });
  await mockConversations(page, []);
  await mockAllowlist(page, [
    { email: "prof@example.edu", note: "Faculty", is_admin: 0,
      last_login: 1_700_000_000, last_active: 1_700_000_000 },
  ]);
  await mockAccessRequests(page, []);
  await mockDeniedRequests(page, []);
  await mockLogs(page, [
    { ts: 1_750_000_000, level: "INFO", logger: "ipeds", msg: "server started" },
    { ts: 1_750_000_100, level: "WARNING", logger: "ipeds", msg: "a slow query ran" },
  ]);
}

// [path, the field's accessible name, a term that matches the seeded data]
const SEARCHES = [
  ["/admin/users/current", "Search email or note", "prof"],
  ["/admin/logs", "Search log messages", "slow"],
  ["/admin/keys", "Search email, label or key", "prof"],
];

for (const [path, label, term] of SEARCHES) {
  test(`${path}: the clear control is the app's own button, reachable by role`,
    async ({ page }) => {
      await adminMocks(page);
      await page.route("**/api/admin/keys", async (route) => route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify([{ id: 1, email: "prof@example.edu", last4: "9f2a",
                                label: "Laptop", created_at: 1, created_by: null,
                                last_used_at: null, revoked_at: null }]),
      }));
      await page.goto(path);
      const field = page.getByLabel(label);
      await expect(field).toBeVisible();

      // Empty: no dead control sitting in the field.
      expect(await page.getByRole("button", { name: "Clear search" }).count()).toBe(0);

      await field.fill(term);
      const clear = page.getByRole("button", { name: "Clear search" });
      // A real button in the a11y tree — the whole gap on Logs. The browser's
      // native type=search ✕ is a pseudo-element and answers this with 0.
      await expect(clear).toBeVisible();
      // ...and the app's own glyph inside it, not the UA's.
      await expect(clear.locator("svg")).toBeVisible();

      // Clearing empties the field AND returns focus to it, so a keyboard user
      // is left where they can type again rather than stranded on a button that
      // just unmounted.
      await clear.click();
      await expect(field).toHaveValue("");
      await expect(field).toBeFocused();
      expect(await page.getByRole("button", { name: "Clear search" }).count()).toBe(0);
    });

}

test("the field still matches the selector that suppresses the browser's own ✕",
  async ({ page }) => {
    // Two overlapping ✕ glyphs is what happens when the app's button is added to
    // a type=search field without the ::-webkit-search-cancel-button rule — and
    // that rule is scoped to `.searchwrap .logsearch`, a selector a refactor can
    // quietly move the markup out from under.
    //
    // The suppression itself is NOT directly observable here: Chromium answers
    // getComputedStyle(el, "::-webkit-search-cancel-button") with the host
    // input's own box and `appearance: auto` whatever the rule says, so a test
    // written against it passes identically with the rule deleted. Measured, not
    // assumed. What IS observable is whether the element still sits where the
    // rule can reach it, which is the realistic regression.
    await adminMocks(page);
    await page.goto("/admin/logs");
    const field = page.getByLabel("Search log messages");
    await expect(field).toBeVisible();
    const matches = await field.evaluate((el) => el.matches(".searchwrap input.logsearch"));
    expect(matches, "the search input moved out of the .searchwrap .logsearch "
                    + "selector that hides the browser's native clear control").toBe(true);
  });
