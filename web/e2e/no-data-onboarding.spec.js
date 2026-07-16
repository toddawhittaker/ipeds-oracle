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
//   - an admin with no dataset loaded lands on Admin -> Imports (not Allowlist)
//     with a prominent "no dataset loaded yet" CTA above the normal catalog UI.
//   - a non-admin with no dataset loaded sees a no-data notice in Chat instead
//     of the usual example-prompt chips.
//
// `has_data: false` is passed explicitly to mockMe -- every OTHER spec in this
// suite relies on mockMe's default of has_data: true (see mocks.js) to keep
// rendering Chat/Admin exactly as before this feature existed.

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
  await expect(page.getByRole("button", { name: "Admin" })).toHaveAttribute(
    "aria-current", "page",
  );
  await expect(
    page.getByText(/No dataset loaded yet.*pick one or more years/i),
  ).toBeVisible();

  // Additive banner, not a replacement -- the normal catalog UI (year cards)
  // must still render underneath it.
  await expect(page.locator('[data-year="2023"]')).toBeVisible();
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

test("admin + has_data:false clicking to Chat sees the admin-flavored no-data notice", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true, has_data: false });
  await mockConversations(page, []);
  await mockImportCatalog(page, NO_DATA_CATALOG);
  await mockImportJobs(page, []);

  await page.goto("/");
  await page.getByRole("button", { name: "Chat", exact: true }).click();

  await expect(
    page.getByRole("heading", { name: /No IPEDS data loaded yet/i }),
  ).toBeVisible();
  await expect(
    page.getByText(/Admin.*Imports tab to load a year/i),
  ).toBeVisible();
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

  await page.getByRole("checkbox", { name: "Integrate 2023-24 (Final)" }).click();
  await page.getByRole("button", { name: /^Integrate selected \(\d+\)$/ }).click();

  await expect.poll(() => integrate.posts.length).toBe(1);
  await expect(page.getByText("swapped")).toBeVisible();

  // The swap must trigger exactly one additional /me fetch.
  await expect.poll(() => meCalls).toBe(2);

  // has_data is now true (from the second /me response) -- Chat's no-data
  // notice must be gone, replaced by the normal examples empty-state.
  await page.getByRole("button", { name: "Chat", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: /No IPEDS data loaded yet/i }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: /Registered Nursing/i }).first(),
  ).toBeVisible();
});
