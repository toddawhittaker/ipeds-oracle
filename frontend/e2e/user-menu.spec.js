import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockLogout,
  mockConversations,
  mockAttention,
  mockAllowlist,
  mockAccessRequests,
  mockDeniedRequests,
} from "./mocks.js";

// The top bar is now just the wordmark (a link home) and a user-badge menu.
// Everything that used to sit loose in the bar — Admin, the theme toggle, About,
// Sign out — lives in the menu under the avatar. Admin attention rides the avatar
// (a count pill) and the Admin menu item. This is a real ARIA menu button:
// arrow/Escape/click-outside keyboard behaviour is browser truth, so it's pinned
// here rather than in a jsdom unit test. (initials(email) itself is unit-tested in
// initials.test.js.)

async function signedIn(page, { admin = false, email, attention } = {}) {
  await mockMe(page, { email: email ?? (admin ? "admin@example.edu" : "jane.doe@example.edu"), is_admin: admin });
  await mockConversations(page, []);
  if (admin) {
    await mockAttention(page, attention ?? { users: 0, skills: 0, logs: 0 });
    // AdminRoute fetches these when the Admin item navigates to /admin/users.
    await mockAllowlist(page, []);
    await mockAccessRequests(page, []);
    await mockDeniedRequests(page, []);
  }
}

const avatar = (page) => page.getByRole("button", { name: /Account menu/ });

test("top bar holds only the wordmark link and the avatar — no loose controls", async ({ page }) => {
  await signedIn(page);
  await page.goto("/");

  const brand = page.getByRole("link", { name: "IPEDS Oracle, go to chat" });
  await expect(brand).toBeVisible();
  await expect(brand).toHaveAttribute("href", "/");
  await expect(avatar(page)).toBeVisible();

  // None of the old top-bar controls exist as bare, always-visible controls.
  await expect(page.getByRole("link", { name: "Chat", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Admin" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Sign out" })).toHaveCount(0);
});

test("the wordmark links home — clicking it from /admin returns to /", async ({ page }) => {
  await signedIn(page, { admin: true });
  await page.goto("/admin/users/current");
  await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/users/current");

  await page.getByRole("link", { name: "IPEDS Oracle, go to chat" }).click();
  await expect.poll(() => new URL(page.url()).pathname).toBe("/");
});

test("the avatar shows initials derived from the email", async ({ page }) => {
  await signedIn(page, { email: "jane.doe@example.edu" });
  await page.goto("/");
  await expect(avatar(page)).toContainText("JD");
});

test("non-admin menu: About, theme toggle, Sign out — but no Admin item", async ({ page }) => {
  await signedIn(page);
  await page.goto("/");

  await avatar(page).click();
  await expect(page.getByRole("menuitem", { name: "About IPEDS Oracle" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: /Switch to (dark|light) mode/ })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Sign out" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Admin" })).toHaveCount(0);
});

test("admin menu carries an Admin item, and attention shows on the avatar + the item", async ({ page }) => {
  await signedIn(page, { admin: true, attention: { users: 3, skills: 2, logs: 0 } });
  await page.goto("/");

  // Avatar advertises the count to assistive tech and shows a corner pill (5 total).
  await expect(avatar(page)).toHaveAttribute("aria-label", /5 items need attention/);
  await expect(page.locator(".avatar-badge")).toHaveText("5");

  await avatar(page).click();
  const adminItem = page.getByRole("menuitem", { name: "Admin" });
  await expect(adminItem).toBeVisible();
  await expect(adminItem.locator(".tab-badge.attention")).toHaveText("5");
  // It's a real anchor (href), not a navigate() button — so middle/⌘/ctrl-click
  // can open Admin in a new tab. Regression guard for that capability.
  await expect(adminItem).toHaveAttribute("href", "/admin");

  await adminItem.click();
  await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/users/current");
});

test("keyboard: opening focuses the first item; Escape closes and restores focus; click-outside closes", async ({ page }) => {
  await signedIn(page);
  await page.goto("/");

  await avatar(page).click();
  // First item (non-admin) is About.
  await expect(page.getByRole("menuitem", { name: "About IPEDS Oracle" })).toBeFocused();

  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(avatar(page)).toBeFocused();

  // Reopen, then click outside to dismiss.
  await avatar(page).click();
  await expect(page.getByRole("menu")).toBeVisible();
  await page.mouse.click(5, 5);
  await expect(page.getByRole("menu")).toHaveCount(0);
});

test("the theme toggle flips data-theme on <html> and persists it", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await signedIn(page);
  await page.goto("/");

  await avatar(page).click();
  await page.getByRole("menuitem", { name: "Switch to dark mode" }).click();

  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expect(await page.evaluate(() => localStorage.getItem("theme"))).toBe("dark");
  // The menu stays open on a theme toggle, and its label flips to the inverse.
  await expect(page.getByRole("menuitem", { name: "Switch to light mode" })).toBeVisible();
});

test("About opens an informational modal with the GitHub link; Close dismisses and restores focus", async ({ page }) => {
  await signedIn(page);
  await page.goto("/");

  await avatar(page).click();
  await page.getByRole("menuitem", { name: "About IPEDS Oracle" }).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("heading", { name: "About IPEDS Oracle" })).toBeVisible();
  // The source link is a GitHub icon (labeled), sitting in the actions row.
  await expect(dialog.getByRole("link", { name: "View the source code on GitHub" }))
    .toHaveAttribute("href", "https://github.com/toddawhittaker/ipeds-oracle");
  // "IPEDS dataset" links out to the NCES IPEDS home.
  await expect(dialog.getByRole("link", { name: "IPEDS dataset" }))
    .toHaveAttribute("href", "https://nces.ed.gov/ipeds/");
  // The user guide is linked for everyone; the admin guide is NOT shown to a non-admin.
  await expect(dialog.getByRole("link", { name: "Using IPEDS Oracle" }))
    .toHaveAttribute("href", /\/docs\/USER_GUIDE\.md$/);
  await expect(dialog.getByRole("link", { name: "Admin guide" })).toHaveCount(0);

  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  // Focus returns to the opener (the avatar).
  await expect(avatar(page)).toBeFocused();
});

test("About shows the Admin guide link only to an admin", async ({ page }) => {
  await signedIn(page, { admin: true });
  await page.goto("/");

  await avatar(page).click();
  await page.getByRole("menuitem", { name: "About IPEDS Oracle" }).click();

  const dialog = page.getByRole("dialog");
  // Both guides are linked for an admin.
  await expect(dialog.getByRole("link", { name: "Using IPEDS Oracle" }))
    .toHaveAttribute("href", /\/docs\/USER_GUIDE\.md$/);
  await expect(dialog.getByRole("link", { name: "Admin guide" }))
    .toHaveAttribute("href", /\/docs\/ADMIN_GUIDE\.md$/);
});

test("Sign out calls logout and returns to the Login screen", async ({ page }) => {
  await signedIn(page);
  const logout = await mockLogout(page);
  await page.goto("/");

  await avatar(page).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();

  await expect(page.getByRole("heading", { name: "IPEDS Oracle" })).toBeVisible();
  expect(logout.calls.length).toBe(1);
});
