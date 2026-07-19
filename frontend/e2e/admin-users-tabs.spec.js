import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockAllowlist,
  mockAccessRequests,
  mockDeniedRequests,
} from "./mocks.js";

// The Admin → Users section is a TABBED interface: Current users / Pending
// requests / Blocked users, one panel visible at a time, each with its own
// path (/admin/users/<sub>). Browser truth that only Playwright can pin:
// path routing + Back/Forward, per-tab state survival across switches, the
// role=tablist/tab/tabpanel semantics + arrow-key nav, and the count badges.
// The pure sub-tab logic (resolveSubTab / subTabKeyForArrow / pendingBadgeTone)
// is unit-tested in src/usertabs.test.js.

const ADMIN = { email: "admin@example.edu", is_admin: true };

const USERS = [
  { email: "alice@example.edu", note: "staff", is_admin: false, last_login: 1_700_000_000 },
  { email: "bob@example.edu", note: "faculty", is_admin: false, last_login: 1_700_000_000 },
];
const PENDING = [
  { id: 1, email: "p1@example.edu", reason: null, status: "pending", created_at: 1_700_000_000 },
  { id: 2, email: "p2@example.edu", reason: null, status: "pending", created_at: 1_700_000_100 },
  { id: 3, email: "p3@example.edu", reason: null, status: "pending", created_at: 1_700_000_200 },
];
const DENIED = [
  { id: 9, canon_email: "blocked@example.edu", emails: ["blocked@example.edu"],
    created_at: 1_700_000_000, denied_at: 1_700_000_500 },
];

async function openUsers(page, { users = USERS, pending = PENDING, denied = DENIED, path } = {}) {
  await mockMe(page, ADMIN);
  await mockConversations(page, []);
  await mockAllowlist(page, users);
  await mockAccessRequests(page, pending);
  await mockDeniedRequests(page, denied);
  await page.goto(path || "/admin/users");
}

const tab = (page, name) => page.getByRole("tab", { name });
const path = (page) => new URL(page.url()).pathname;

test.describe("Users sub-tabs — routing", () => {
  test("bare /admin/users redirects to /current and shows only the Current panel", async ({ page }) => {
    await openUsers(page);

    await expect.poll(() => path(page)).toBe("/admin/users/current");
    // Current users table + add form are visible; the other panels are hidden
    // (a hidden [role=tabpanel] is out of the a11y tree, so getByRole can't see it).
    await expect(page.getByRole("cell", { name: "alice@example.edu", exact: true })).toBeVisible();
    await expect(tab(page, /Current users/)).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("cell", { name: "p1@example.edu", exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: /Allow new access request/ })).toHaveCount(0);
  });

  test("an invalid sub redirects to /current; a legacy /admin/pending alias redirects to the Pending tab", async ({ page }) => {
    await openUsers(page, { path: "/admin/users/bogus" });
    await expect.poll(() => path(page)).toBe("/admin/users/current");

    await openUsers(page, { path: "/admin/pending" });
    await expect.poll(() => path(page)).toBe("/admin/users/pending");
    await expect(tab(page, /Pending requests/)).toHaveAttribute("aria-selected", "true");
  });

  test("a deep link to /admin/users/blocked opens the Blocked tab directly", async ({ page }) => {
    await openUsers(page, { path: "/admin/users/blocked" });
    await expect(tab(page, /Blocked users/)).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("cell", { name: "blocked@example.edu", exact: true })).toBeVisible();
  });

  test("clicking a tab pushes its path; Back/Forward move between tab states", async ({ page }) => {
    await openUsers(page);
    await expect.poll(() => path(page)).toBe("/admin/users/current");

    await tab(page, /Pending requests/).click();
    await expect.poll(() => path(page)).toBe("/admin/users/pending");
    await expect(page.getByRole("cell", { name: "p1@example.edu", exact: true })).toBeVisible();
    // Current-panel content is now hidden.
    await expect(page.getByRole("cell", { name: "alice@example.edu", exact: true })).toHaveCount(0);

    await page.goBack();
    await expect.poll(() => path(page)).toBe("/admin/users/current");
    await expect(tab(page, /Current users/)).toHaveAttribute("aria-selected", "true");

    await page.goForward();
    await expect.poll(() => path(page)).toBe("/admin/users/pending");
    await expect(tab(page, /Pending requests/)).toHaveAttribute("aria-selected", "true");
  });
});

test.describe("Users sub-tabs — counts", () => {
  test("each tab shows its category total; Pending carries the attention tone only when non-empty", async ({ page }) => {
    await openUsers(page);

    // The count is part of the tab's accessible name (e.g. "Current users 2").
    await expect(tab(page, /Current users/)).toContainText("2");
    await expect(tab(page, /Pending requests/)).toContainText("3");
    await expect(tab(page, /Blocked users/)).toContainText("1");
    // Pending has work waiting -> accent "attention" badge (never an error tone).
    await expect(tab(page, /Pending requests/).locator(".usertab-badge.attention")).toBeVisible();

    // With nothing pending the badge exists (shows 0) but drops the attention tone.
    await openUsers(page, { pending: [] });
    const pend = tab(page, /Pending requests/);
    await expect(pend).toContainText("0");
    await expect(pend.locator(".usertab-badge.attention")).toHaveCount(0);
  });
});

test.describe("Users sub-tabs — per-tab state survives a switch", () => {
  test("a search typed on Current users is still there after visiting Pending and coming back", async ({ page }) => {
    await openUsers(page);

    const search = page.getByRole("searchbox", { name: "Search email or note" });
    await search.fill("alice");
    await expect(page.getByRole("cell", { name: "bob@example.edu", exact: true })).toHaveCount(0);

    // Leave to Pending and return — the panels stay mounted (hidden), so the
    // Current table's own search/sort/page state is preserved, not reset.
    await tab(page, /Pending requests/).click();
    await expect(page.getByRole("cell", { name: "p1@example.edu", exact: true })).toBeVisible();
    await tab(page, /Current users/).click();

    await expect(page.getByRole("searchbox", { name: "Search email or note" })).toHaveValue("alice");
    await expect(page.getByRole("cell", { name: "bob@example.edu", exact: true })).toHaveCount(0);
  });
});

test.describe("Users sub-tabs — keyboard + ARIA", () => {
  test("tablist/tab/tabpanel wiring is present and arrow/Home/End move the active tab", async ({ page }) => {
    await openUsers(page);

    await expect(page.getByRole("tablist", { name: "User management" })).toBeVisible();
    const current = tab(page, /Current users/);
    await expect(current).toHaveAttribute("aria-controls", "userpanel-current");
    // The visible panel is labelled by its tab.
    await expect(page.locator("#userpanel-current")).toHaveAttribute("aria-labelledby", "usertab-current");

    // Automatic activation: focus the active tab, then arrow through.
    await current.focus();
    await page.keyboard.press("ArrowRight");
    await expect.poll(() => path(page)).toBe("/admin/users/pending");
    await expect(tab(page, /Pending requests/)).toBeFocused();

    await page.keyboard.press("End");
    await expect.poll(() => path(page)).toBe("/admin/users/blocked");
    await expect(tab(page, /Blocked users/)).toBeFocused();

    await page.keyboard.press("Home");
    await expect.poll(() => path(page)).toBe("/admin/users/current");
    await expect(tab(page, /Current users/)).toBeFocused();

    // Left from the first tab wraps to the last.
    await page.keyboard.press("ArrowLeft");
    await expect.poll(() => path(page)).toBe("/admin/users/blocked");
  });
});
