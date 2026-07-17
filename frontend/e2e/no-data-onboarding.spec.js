import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockImportCatalog,
  mockImportJobs,
  mockImportJobPoll,
  mockIntegrate,
} from "./mocks.js";

// Fresh-deploy / no-data onboarding (SPEC-nodata.md):
//   - an admin with no dataset loaded lands on Admin -> Imports (not Users)
//     with a prominent "no dataset loaded yet" CTA above the normal catalog UI.
//   - a non-admin with no dataset loaded sees a no-data notice in Chat instead
//     of the usual example-prompt chips.
//
// `has_data: false` is passed explicitly to mockMe -- every OTHER spec in this
// suite relies on mockMe's default of has_data: true (see mocks.js) to keep
// rendering Chat/Admin exactly as before this feature existed.
//
// Client-side routing (react-router-dom v6) makes this an explicit URL
// contract, not just a view-state one: the once-on-load redirect lands
// specifically on /admin/imports, fires ONLY when the admin landed on bare /
// (a deep link to /chat/:id or /admin/:other-tab must NOT be yanked), and
// must not re-fire on a later has_data flip (refreshMe). Rewritten (still the
// sole owner: test-engineer) as part of the routing work -- these URL
// assertions are new/RED against the current (routerless) App.jsx; the
// pre-existing view-state assertions they sit alongside were already green
// and must stay green once routing lands.

const NO_DATA_CATALOG = {
  probed_at: 1_700_000_000,
  partial: false,
  years: [
    { start_year: 2023, year: 2024, year_label: "2023-24", status: "final",
      integrated: false, available: true, release: "Final", selectable: true },
    { start_year: 2024, year: 2025, year_label: "2024-25", status: "provisional",
      integrated: false, available: true, release: "Provisional", selectable: true },
  ],
};

test("admin + has_data:false lands on Admin/Imports with the no-dataset CTA", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true, has_data: false });
  await mockConversations(page, []);
  await mockImportCatalog(page, NO_DATA_CATALOG);
  await mockImportJobs(page, []);

  await page.goto("/");

  // No manual navigation click -- an admin with no data must land directly on
  // the Admin view, Imports subtab, without having to find their own way there.
  await expect(page.getByRole("link", { name: "Admin" })).toHaveAttribute(
    "aria-current", "page",
  );
  await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/imports");
  await expect(
    page.getByText(/No dataset loaded yet.*pick one or more years/i),
  ).toBeVisible();

  // Additive banner, not a replacement -- the normal catalog UI (year cards)
  // must still render underneath it.
  await expect(page.locator('[data-year="2023"]')).toBeVisible();
});

test("admin + has_data:false deep-linking /chat/3 STAYS on /chat/3 (no-data redirect only fires from bare /)", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true, has_data: false });
  await mockConversations(page, [{ id: 3, title: "Old chat" }]);
  await page.route("**/api/chat/conversations/3", async (route) => {
    await route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify([
        { role: "user", content: "Old chat" },
        { role: "assistant", content: "Old answer." },
      ]),
    });
  });

  await page.goto("/chat/3");

  // Must stay put -- the once-on-load onboarding redirect is scoped to
  // landing on bare /, not every route a no-data admin might deep-link to.
  expect(new URL(page.url()).pathname).toBe("/chat/3");
  await expect(page.getByText("Old answer.")).toBeVisible();
  await expect(page.getByRole("link", { name: "Admin" })).not.toHaveAttribute(
    "aria-current", "page",
  );
});

test("non-admin + has_data:false sees the Chat no-data notice, no example chips", async ({ page }) => {
  await mockMe(page, { email: "user@example.edu", is_admin: false, has_data: false });
  await mockConversations(page, []);

  await page.goto("/");

  await expect(
    page.getByRole("heading", { name: /No IPEDS data loaded yet/i }),
  ).toBeVisible();
  await expect(
    page.getByText(/administrator needs to load a dataset/i),
  ).toBeVisible();

  // The normal empty-state example prompts (e.g. "Registered Nursing") must
  // NOT render in the no-data state.
  await expect(page.getByRole("button", { name: /Registered Nursing/i })).toHaveCount(0);
});

test("admin + has_data:false clicking to Chat sees the admin-flavored no-data notice, and stays on / (redirect does not re-fire)", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true, has_data: false });
  await mockConversations(page, []);
  await mockImportCatalog(page, NO_DATA_CATALOG);
  await mockImportJobs(page, []);

  await page.goto("/");
  await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/imports");

  await page.getByRole("link", { name: "Chat", exact: true }).click();

  await expect(
    page.getByRole("heading", { name: /No IPEDS data loaded yet/i }),
  ).toBeVisible();
  await expect(
    page.getByText(/Admin.*Imports tab to load a year/i),
  ).toBeVisible();

  // The once-on-load onboarding redirect must not re-fire and bounce the
  // admin straight back to /admin/imports just because they navigated here.
  expect(new URL(page.url()).pathname).toBe("/");
  await page.waitForTimeout(300);
  expect(new URL(page.url()).pathname).toBe("/");
});

// Code-review LOW: the old `initialTab` scheme made EVERY "Admin" click land
// a no-data admin on Imports (Admin remounted each time). Routing replaced it
// with `/admin` -> unconditional <Navigate to="/admin/users">, so a no-data
// admin who clicks Chat and then clicks Admin again now lands on an empty
// Users tab -- while the Chat empty-state CTA (Chat.jsx) still tells them to
// "Head to the Admin -> Imports tab". The /admin index route must stay
// conditional on has_data.
test("no-data admin who clicks Chat then Admin lands back on /admin/imports, not an empty Users tab", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true, has_data: false });
  await mockConversations(page, []);
  await mockImportCatalog(page, NO_DATA_CATALOG);
  await mockImportJobs(page, []);

  await page.goto("/");
  await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/imports");

  await page.getByRole("link", { name: "Chat", exact: true }).click();
  expect(new URL(page.url()).pathname).toBe("/");

  await page.getByRole("link", { name: "Admin", exact: true }).click();

  await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/imports");
});

// Code-review Medium fix: has_data can flip mid-session (an admin integrates
// the first year without reloading the page), so App.jsx wires Admin's
// onDataChanged -> a fresh GET /api/auth/me once an import job reaches the
// terminal "swapped" status (see Admin.jsx watch()). This locks that
// re-fetch: exactly one extra /me call after the swap, and has_data:true from
// that second response flips Chat out of the no-data notice.
test("an integrate reaching 'swapped' re-fetches /me and clears the Chat no-data notice", async ({ page }) => {
  let meCalls = 0;
  await page.route("**/api/auth/me", async (route) => {
    meCalls += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        email: "admin@example.edu", is_admin: true, has_data: meCalls > 1,
      }),
    });
  });
  await mockConversations(page, []);
  await mockImportCatalog(page, NO_DATA_CATALOG);
  await mockImportJobs(page, []);
  const integrate = await mockIntegrate(page, { jobId: 501, status: "pending" });
  await mockImportJobPoll(page, 501, [
    { id: 501, filename: "integrate:2023", status: "running", log: "", report: null, updated_at: 1 },
    { id: 501, filename: "integrate:2023", status: "swapped", log: "done", report: "ok", updated_at: 2 },
  ]);

  await page.goto("/");
  // has_data:false on the first /me -> lands directly on Admin/Imports.
  await expect(
    page.getByText(/No dataset loaded yet.*pick one or more years/i),
  ).toBeVisible();
  expect(meCalls).toBe(1);
  await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/imports");

  await page.getByRole("checkbox", { name: "Integrate 2023-24 (Final)" }).click();
  await page.getByRole("button", { name: /^Integrate selected \(\d+\)$/ }).click();

  await expect.poll(() => integrate.posts.length).toBe(1);
  await expect(page.getByText("swapped")).toBeVisible();

  // The swap must trigger exactly one additional /me fetch.
  await expect.poll(() => meCalls).toBe(2);

  // The refreshMe() re-fetch that flips has_data must NOT yank the admin's
  // current view/URL out from under them -- they're still looking at the
  // swapped job's result on Admin/Imports, not bounced anywhere else.
  expect(new URL(page.url()).pathname).toBe("/admin/imports");

  // has_data is now true (from the second /me response) -- Chat's no-data
  // notice must be gone, replaced by the normal examples empty-state.
  await page.getByRole("link", { name: "Chat", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: /No IPEDS data loaded yet/i }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: /Registered Nursing/i }).first(),
  ).toBeVisible();
});
