import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockImportJobs,
  mockImportJobPoll,
  mockImportCatalog,
  mockIntegrate,
  mockDeintegrate,
} from "./mocks.js";

// FEATURE A — the "trashcan" (remove an already-integrated year) and
// FEATURE B — the determinate rebuild progress bar. Both features are not
// implemented yet (Admin.jsx has no `.year-remove` button and no
// [data-testid="rebuild-progress"] block), so every test below is expected to
// fail red until the implementer ships them — see the spec contract:
//   * a `.year-remove` button, aria-label `Remove {year_label} from the
//     database`, appears as a SIBLING of an integrated/update year's
//     `.year-card` tile (never on a non-integrated card), and is absent when
//     locked (a job is running).
//   * clicking it opens the app-styled confirmation modal (role="alertdialog",
//     confirm button "Remove year"), then DELETEs
//     /api/admin/import/year/{start_year} and watches the returned job like
//     any other import/integrate job.
//   * when a polled job's `progress` JSON carries a `rebuild` key
//     ({tables_total, tables_done, pct}), the job panel renders a determinate
//     `[data-testid="rebuild-progress"]` progress bar
//     (role="progressbar", aria-valuemin/max/now=pct, "X / Y tables" text).

async function openImportsTab(page) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockImportJobs(page, []);
  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByRole("link", { name: "Imports" }).click();
}

const CATALOG = {
  probed_at: 1_700_000_000,
  partial: false,
  years: [
    { start_year: 2022, year: 2023, year_label: "2022-23", status: "integrated",
      integrated: true, available: true, release: "Final", selectable: false },
    { start_year: 2023, year: 2024, year_label: "2023-24", status: "final",
      integrated: false, available: true, release: "Final", selectable: true },
  ],
};

test.describe("trashcan: remove an integrated year", () => {
  test("a remove button is visible on an integrated year and absent on a non-integrated one", async ({ page }) => {
    await mockImportCatalog(page, CATALOG);
    await openImportsTab(page);

    const removeIntegrated = page.getByRole("button", { name: "Remove 2022-23 from the database" });
    await expect(removeIntegrated).toBeVisible();
    await expect(page.locator(".year-remove")).toHaveCount(1);

    await expect(page.getByRole("button", { name: "Remove 2023-24 from the database" })).toHaveCount(0);
  });

  test("confirm -> DELETE fires for the right start_year -> job poll -> success notice", async ({ page }) => {
    await mockImportCatalog(page, CATALOG);
    const del = await mockDeintegrate(page, { jobId: 55, status: "pending" });
    await mockImportJobPoll(page, 55, [
      { id: 55, filename: "deintegrate:2022", status: "running", log: "removing…", report: null, updated_at: 1 },
      { id: 55, filename: "deintegrate:2022", status: "swapped", log: "done", report: "ok", updated_at: 2 },
    ]);
    await openImportsTab(page);

    await page.getByRole("button", { name: "Remove 2022-23 from the database" }).click();
    const dialog = page.getByRole("alertdialog");
    // The modal explains the consequence, then a specific "Remove year" confirm.
    await expect(dialog).toContainText(/rebuilds|can't be undone/i);
    await dialog.getByRole("button", { name: "Remove year" }).click();

    await expect.poll(() => del.calls.length).toBe(1);
    expect(del.calls[0]).toBe(2022);

    await expect(page.getByText("swapped")).toBeVisible();
    await expect(page.locator(".notice").first()).toBeVisible();
  });

  test("cancelling the confirm modal does not fire a DELETE", async ({ page }) => {
    await mockImportCatalog(page, CATALOG);
    const del = await mockDeintegrate(page, { jobId: 56, status: "pending" });
    await openImportsTab(page);

    await page.getByRole("button", { name: "Remove 2022-23 from the database" }).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toHaveCount(0);
    await page.waitForTimeout(200);

    expect(del.calls.length).toBe(0);
  });

  test("a 409 response while another import is running shows the already-running notice", async ({ page }) => {
    await mockImportCatalog(page, CATALOG);
    await mockDeintegrate(page, { httpStatus: 409 });
    await openImportsTab(page);

    await page.getByRole("button", { name: "Remove 2022-23 from the database" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Remove year" }).click();

    // The already-running detail surfaces as the modal's in-modal error (the
    // modal stays open on failure).
    await expect(page.getByText(/already running/i)).toBeVisible();
  });
});

test.describe("rebuild progress bar", () => {
  test("renders a determinate progress bar from progress.rebuild", async ({ page }) => {
    await mockImportCatalog(page, CATALOG);
    await mockIntegrate(page, { jobId: 91, status: "pending" });
    await mockImportJobPoll(page, 91, [
      {
        id: 91, filename: "integrate:2023", status: "running", log: "", report: null,
        updated_at: 1,
        progress: JSON.stringify({
          overall: { phase: "building", message: "Rebuilding the staging database…" },
          years: {},
          rebuild: { tables_total: 40, tables_done: 10, pct: 25 },
        }),
      },
    ]);
    await openImportsTab(page);

    await page.getByRole("checkbox", { name: "Integrate 2023-24 (Final)" }).click();
    await page.getByRole("button", { name: /^Integrate selected \(\d+\)$/ }).click();

    const bar = page.getByTestId("rebuild-progress");
    await expect(bar).toBeVisible();
    const progressbar = bar.getByRole("progressbar");
    await expect(progressbar).toHaveAttribute("aria-valuemin", "0");
    await expect(progressbar).toHaveAttribute("aria-valuemax", "100");
    await expect(progressbar).toHaveAttribute("aria-valuenow", "25");
    await expect(bar).toContainText("10 / 40 tables");
  });

  test("is absent when the job has no rebuild progress at all", async ({ page }) => {
    await mockImportCatalog(page, CATALOG);
    await mockIntegrate(page, { jobId: 92, status: "pending" });
    await mockImportJobPoll(page, 92, [
      {
        id: 92, filename: "integrate:2023", status: "running", log: "", report: null,
        updated_at: 1,
        progress: JSON.stringify({
          overall: { phase: "downloading", message: "Fetching 1 year(s) from NCES…" },
          years: {
            2023: { start_year: 2023, year_label: "2023-24", step: "downloading",
                   downloaded_bytes: 0, total_bytes: 100, pct: 0 },
          },
        }),
      },
    ]);
    await openImportsTab(page);

    await page.getByRole("checkbox", { name: "Integrate 2023-24 (Final)" }).click();
    await page.getByRole("button", { name: /^Integrate selected \(\d+\)$/ }).click();

    await expect(page.getByTestId("import-progress")).toBeVisible();
    await expect(page.getByTestId("rebuild-progress")).toHaveCount(0);
  });
});
