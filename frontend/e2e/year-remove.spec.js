import { test, expect } from "@playwright/test";
import {
  gotoAdmin,
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
  await gotoAdmin(page);
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

  test("a 409 with no locatable running job keeps the modal open showing the already-running error", async ({ page }) => {
    await mockImportCatalog(page, CATALOG);
    await mockDeintegrate(page, { httpStatus: 409 });
    await openImportsTab(page); // mockImportJobs is [] here -> nothing to hand off to

    await page.getByRole("button", { name: "Remove 2022-23 from the database" }).click();
    const dialog = page.getByRole("alertdialog");
    await dialog.getByRole("button", { name: "Remove year" }).click();

    // No job found to attach to, so it's a genuine failure: the modal stays open
    // with the already-running detail as its in-modal error.
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText(/already running/i)).toBeVisible();
  });

  test("a 409 with a job already in flight HANDS OFF: the modal closes and that job's progress surfaces", async ({ page }) => {
    // Regression guard (code review, PR #85): the already-running recovery must
    // NOT rethrow and trap the just-attached live job behind the inert error
    // modal. It closes the modal, shows the already-running notice, and watches
    // the running job so its progress is reachable.
    await mockImportCatalog(page, CATALOG);
    await mockDeintegrate(page, { httpStatus: 409 });
    await openImportsTab(page);
    // Settle the mount BEFORE swapping the jobs mock. The tab now adopts a job
    // that is already running when it mounts, so if the override landed while
    // the mount's own /import/jobs request was still in flight, the tab would
    // adopt job 77, lock the controls, and this trashcan would not exist. The
    // scenario under test is the other one: nothing running at mount, and the
    // 409 arrives only when the admin acts.
    await expect(page.getByRole("button", { name: "Remove 2022-23 from the database" }))
      .toBeVisible();
    // Override: a job IS mid-flight, and it polls to completion.
    await mockImportJobs(page, [
      { id: 77, filename: "integrate:2023", status: "running", log: "", report: null, updated_at: 1 },
    ]);
    await mockImportJobPoll(page, 77, [
      { id: 77, filename: "integrate:2023", status: "running", log: "working…", report: null, updated_at: 1 },
      { id: 77, filename: "integrate:2023", status: "swapped", log: "done", report: "ok", updated_at: 2 },
    ]);

    await page.getByRole("button", { name: "Remove 2022-23 from the database" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Remove year" }).click();

    // Handed off, not trapped: modal closed, notice shown, running job surfaced.
    await expect(page.getByRole("alertdialog")).toHaveCount(0);
    await expect(page.getByText(/already running/i)).toBeVisible();
    await expect(page.getByText("swapped")).toBeVisible();
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
    // Adding years is confirmed now, the same as removing one — see the
    // "adding years is confirmed" describe below.
    await page.getByRole("alertdialog").getByRole("button", { name: "Start rebuild" }).click();

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
    // Adding years is confirmed now, the same as removing one — see the
    // "adding years is confirmed" describe below.
    await page.getByRole("alertdialog").getByRole("button", { name: "Start rebuild" }).click();

    await expect(page.getByTestId("import-progress")).toBeVisible();
    await expect(page.getByTestId("rebuild-progress")).toHaveCount(0);
  });
});

test.describe("a job already running when the tab mounts", () => {
  // THE REGRESSION: `locked` derives from `active`, and `active` was only ever
  // set by watch() — which only ran for a job THIS session started or clicked
  // "view" on. So an admin who reloaded the tab, or a SECOND admin, saw the
  // ordinary catalog with "Integrate selected" enabled, manual upload enabled
  // and the trashcans live, while a full rebuild and atomic swap of the live
  // database was in progress. The only trace was a row reading `running` at the
  // bottom of a long page. The 409 hand-off means nothing corrupts, but
  // recovering from a wrong-looking-but-blocked click is not the same as never
  // presenting the wrong state.
  test("is adopted on mount: controls lock and the notice says it wasn't yours", async ({ page }) => {
    await mockImportCatalog(page, CATALOG);
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockConversations(page, []);
    // A job already in flight, started by somebody else.
    await mockImportJobs(page, [
      { id: 77, filename: "integrate:2023", status: "running", updated_at: 1_700_000_000 },
    ]);
    await mockImportJobPoll(page, 77, [
      { id: 77, filename: "integrate:2023", status: "running", log: "building…", report: null },
    ]);
    await page.goto("/");
    await gotoAdmin(page);
    await page.getByRole("link", { name: "Imports" }).click();

    // The notice names the situation rather than implying the admin did it.
    const notice = page.locator(".notice", { hasText: /import started by another session/i });
    await expect(notice).toBeVisible();

    // ...and the destructive controls are genuinely locked, not merely 409-safe.
    await expect(page.getByRole("button", { name: /Integrate selected/ })).toBeDisabled();
    await expect(page.locator(".year-remove")).toHaveCount(0);
  });
});

test.describe("adding years is confirmed, like removing one", () => {
  // Adding is the SAME operation as removing, with different inputs: a full
  // rebuild from the union ending in an atomic swap. Removing had a danger
  // modal; adding fired on a single click. That asymmetry teaches an admin that
  // the guarded one is the dangerous one.
  test("Integrate opens a confirmation and only starts the rebuild on confirm", async ({ page }) => {
    await mockImportCatalog(page, CATALOG);
    const integrate = await mockIntegrate(page, { jobId: 91 });
    await mockImportJobPoll(page, 91, [
      { id: 91, filename: "integrate:2023", status: "running", log: "…", report: null },
    ]);
    await openImportsTab(page);

    await page.locator(".year-card", { hasText: "2023-24" }).click();
    await page.getByRole("button", { name: /Integrate selected/ }).click();

    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText(/rebuild/i);
    // No request may have gone out yet — the modal is a gate, not a notice.
    expect(integrate.posts.length).toBe(0);

    await dialog.getByRole("button", { name: "Start rebuild" }).click();
    await expect.poll(() => integrate.posts.length).toBe(1);
  });

  test("cancelling the confirmation starts nothing", async ({ page }) => {
    await mockImportCatalog(page, CATALOG);
    const integrate = await mockIntegrate(page, { jobId: 92 });
    await openImportsTab(page);

    await page.locator(".year-card", { hasText: "2023-24" }).click();
    await page.getByRole("button", { name: /Integrate selected/ }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Cancel" }).click();

    await expect(page.getByRole("alertdialog")).toHaveCount(0);
    expect(integrate.posts.length).toBe(0);
  });
});
