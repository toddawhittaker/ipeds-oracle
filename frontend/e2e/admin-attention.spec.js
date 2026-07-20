import { test, expect } from "@playwright/test";
import {
  mockMe, mockConversations, mockAttention, mockMarkLogsSeen,
  mockAllowlist, mockAccessRequests, mockDeniedRequests, mockSkills, mockLogs,
} from "./mocks.js";

// Admin "attention" indicators: a total badge on the top-bar Admin button (live on
// EVERY page, including Chat, because it's fetched from the Shell) and a per-section
// count on the Admin nav for each area with a backlog (Users / Skills / Logs). The
// badge math is unit-tested in src/attention.test.js; this pins the browser truth —
// the Shell-level fetch, the nav rendering, and the Logs acknowledge flow.

// Everything the admin panel's sections fetch on mount, so navigating to /admin
// renders without a stray unmocked request.
async function adminMocks(page) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAllowlist(page, []);
  await mockAccessRequests(page, []);
  await mockDeniedRequests(page, []);
  await mockSkills(page, []);
  await mockLogs(page, []);
}

test("top-bar Admin badge is live on Chat, and each section shows its count", async ({ page }) => {
  await adminMocks(page);
  await mockAttention(page, { users: 2, skills: 3, logs: 5 });
  await mockMarkLogsSeen(page);

  // On Chat — the badge still reflects the total (proves the Shell-level fetch,
  // not a per-admin-tab one).
  await page.goto("/");
  const adminLink = page.getByRole("link", { name: "Admin, 10 items need attention" });
  await expect(adminLink).toBeVisible();
  await expect(adminLink).toContainText("10");

  // Into the admin panel: the section nav badges each area with a backlog.
  await page.goto("/admin/users/current");
  const nav = page.locator(".subtabs");
  await expect(nav.getByRole("link", { name: /^Users/ })).toContainText("2");
  await expect(nav.getByRole("link", { name: /^Skills/ })).toContainText("3");
  await expect(nav.getByRole("link", { name: /^Logs/ })).toContainText("5");
  // Areas with no actionable backlog carry no badge (no digits in the label).
  await expect(nav.getByRole("link", { name: "Imports" })).toHaveText("Imports");
  await expect(nav.getByRole("link", { name: "Usage" })).toHaveText("Usage");
});

test("viewing Logs acknowledges the problems: the Logs badge clears and the total drops", async ({ page }) => {
  await adminMocks(page);
  const att = await mockAttention(page, { users: 2, skills: 3, logs: 5 });
  const seen = await mockMarkLogsSeen(page);

  await page.goto("/admin/users/current");
  const nav = page.locator(".subtabs");
  await expect(nav.getByRole("link", { name: /^Logs/ })).toContainText("5");

  // Simulate the server clearing this admin's log problems once acknowledged:
  // the mark-seen the Logs tab fires on mount triggers a re-fetch (refreshAttention),
  // which now returns logs:0.
  att.set({ users: 2, skills: 3, logs: 0 });
  await nav.getByRole("link", { name: /^Logs/ }).click();

  // The tab fired the acknowledge...
  await expect.poll(() => seen.calls).toBeGreaterThan(0);
  // ...the Logs section badge is gone, and the top-bar total drops to 5 (2+3+0).
  await expect(page.locator(".subtabs").getByRole("link", { name: "Logs" })).toHaveText("Logs");
  await expect(page.getByRole("link", { name: "Admin, 5 items need attention" })).toBeVisible();
});

test("counts refresh the instant the tab regains focus (not only on the slow poll)", async ({ page }) => {
  // A backgrounded tab throttles setInterval, so a change made while the admin is
  // away wouldn't surface until a much-delayed tick — the "polling doesn't update,
  // only a refresh does" bug. The Shell re-fetches on visibility/focus. Tested on
  // Chat, where the only refresh path is that handler (no admin-tab poll running).
  await adminMocks(page);
  const att = await mockAttention(page, { users: 1, skills: 0, logs: 0 });
  await mockMarkLogsSeen(page);

  await page.goto("/");
  await expect(page.getByRole("link", { name: /^Admin,/ })).toContainText("1");

  // Data changes while the tab is (conceptually) backgrounded — the badge is stale.
  att.set({ users: 1, skills: 0, logs: 4 });
  await expect(page.locator(".tabs").getByRole("link", { name: /Admin/ })).toContainText("1");

  // Regaining focus re-fetches immediately → the badge is current (1+4=5).
  await page.evaluate(() => globalThis.dispatchEvent(new Event("focus")));
  await expect(page.getByRole("link", { name: "Admin, 5 items need attention" })).toBeVisible();
});

test("a large count collapses to a capped 99+ form", async ({ page }) => {
  await adminMocks(page);
  await mockAttention(page, { users: 0, skills: 250, logs: 0 });
  await mockMarkLogsSeen(page);

  await page.goto("/admin/users/current");
  await expect(page.getByRole("link", { name: /^Admin,/ })).toContainText("99+");
  await expect(page.locator(".subtabs").getByRole("link", { name: /^Skills/ })).toContainText("99+");
});

test("no work waiting means no badge anywhere", async ({ page }) => {
  await adminMocks(page);
  await mockAttention(page, { users: 0, skills: 0, logs: 0 });
  await mockMarkLogsSeen(page);

  await page.goto("/admin/users/current");
  // The Admin button is a plain link with no count and no attention aria-label.
  const adminLink = page.locator(".tabs").getByRole("link", { name: "Admin" });
  await expect(adminLink).toHaveText("Admin");
  // No section badge either.
  await expect(page.locator(".subtabs .tab-badge")).toHaveCount(0);
  await expect(page.locator(".tabs .tab-badge")).toHaveCount(0);
});
