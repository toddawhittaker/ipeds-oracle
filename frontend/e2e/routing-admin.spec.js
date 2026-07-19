import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockAllowlist,
  mockAccessRequests,
  mockDeniedRequests,
  mockImportCatalog,
  mockImportJobs,
  mockLogs,
} from "./mocks.js";

// Client-side routing for Admin:
//   /admin       -> redirect /admin/users
//   /admin/:tab  -> Admin, tab from the URL. Valid tabs: users, imports,
//                   usage, skills, logs. Unknown tab -> redirect /admin/users.
//
// "Allowlist" is renamed to "Users" in the UI label + route + internal tab
// key ONLY -- the underlying GET/POST /api/admin/allowlist endpoints are
// UNCHANGED (that's the point: it proves the backend diff is zero), so every
// test below still uses mockAllowlist/mockAccessRequests/mockDeniedRequests
// verbatim. Originally written before the routing feature existed (TDD); the
// router has since landed (App.jsx/Admin.jsx), so these now exercise the
// real client-side routing implementation.

async function mockAdminBasics(page) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAllowlist(page, [
    { email: "user@example.edu", note: "staff", is_admin: false, last_login: 1700000000 },
  ]);
  await mockAccessRequests(page, []);
  await mockDeniedRequests(page, []);
}

test.describe("admin routing", () => {
  test("/admin redirects to /admin/users, renders the Users panel, and marks it aria-current", async ({ page }) => {
    await mockAdminBasics(page);

    await page.goto("/admin");

    await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/users");
    const usersSub = page.getByRole("link", { name: "Users" });
    await expect(usersSub).toHaveAttribute("aria-current", "page");
    // exact:true -- the Users table's leading selection checkbox (H1 a11y fix)
    // has accessible name "Select user user@example.edu", which otherwise
    // substring-collides with this cell under Playwright's default matching.
    await expect(page.getByRole("cell", { name: "user@example.edu", exact: true })).toBeVisible();
  });

  test("/admin/logs deep link renders the Logs panel with logs aria-current", async ({ page }) => {
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockConversations(page, []);
    await mockLogs(page, [
      { ts: 1700000000, level: "INFO", name: "app.main", msg: "startup complete" },
    ]);

    await page.goto("/admin/logs");

    const logsSub = page.getByRole("link", { name: "Logs" });
    await expect(logsSub).toHaveAttribute("aria-current", "page");
    await expect(page.getByRole("heading", { name: "Server logs" })).toBeVisible();
    await expect(page.getByText("startup complete")).toBeVisible();
  });

  // Code-review LOW (see no-data-onboarding.spec.js for the has_data:false
  // counterpart): confirms the /admin index route's has_data-conditional
  // target doesn't change this well-established, has-data behavior.
  test("an admin WITH data clicking 'Admin' lands on /admin/users (unchanged)", async ({ page }) => {
    await mockAdminBasics(page);

    await page.goto("/");
    await page.getByRole("link", { name: "Admin", exact: true }).click();

    await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/users");
  });

  test("/admin/bogus (unknown tab) redirects to /admin/users", async ({ page }) => {
    await mockAdminBasics(page);

    await page.goto("/admin/bogus");

    await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/users");
    await expect(page.getByRole("link", { name: "Users" })).toHaveAttribute("aria-current", "page");
  });

  test("clicking a subtab pushes /admin/:tab, and Back returns to the previous subtab", async ({ page }) => {
    await mockAdminBasics(page);
    await mockImportCatalog(page, { probed_at: 1700000000, partial: false, years: [] });
    await mockImportJobs(page, []);

    await page.goto("/admin/users");
    // Fail fast (expect's 5s default) rather than the full test timeout if
    // deep-linking /admin/users doesn't even render the Admin subtabs yet.
    const importsBtn = page.getByRole("link", { name: "Imports" });
    await expect(importsBtn).toBeVisible();
    await importsBtn.click();

    await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/imports");
    await expect(page.getByRole("link", { name: "Imports" })).toHaveAttribute("aria-current", "page");

    await page.goBack();
    await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/users");
    await expect(page.getByRole("link", { name: "Users" })).toHaveAttribute("aria-current", "page");
  });

  test("subtab label AND panel heading read 'Users' while the mocked allowlist endpoints stay unchanged", async ({ page }) => {
    await mockAdminBasics(page);

    await page.goto("/admin/users");

    // Renamed in the UI only -- the mocks above hit the pre-existing
    // /api/admin/allowlist, /access-requests, /access-requests/denied paths
    // verbatim, and the panel still renders their data correctly.
    await expect(page.getByRole("link", { name: "Users" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Users" })).toBeVisible();
    // exact:true -- the Users table's leading selection checkbox (H1 a11y fix)
    // has accessible name "Select user user@example.edu", which otherwise
    // substring-collides with this cell under Playwright's default matching.
    await expect(page.getByRole("cell", { name: "user@example.edu", exact: true })).toBeVisible();
  });
});
