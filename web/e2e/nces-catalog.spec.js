import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockImportJobs,
  mockImportJobPoll,
  mockImportCatalog,
  mockIntegrate,
} from "./mocks.js";

// The Imports tab's year catalog: one card per NCES start year. Selector
// contract pinned here (see the test-engineer's report for the full writeup):
//   * each card carries data-year={start_year} and data-status={status}
//   * a NON-selectable card (already integrated, or unavailable) has
//     aria-disabled="true" and contains no checkbox
//   * a selectable card's checkbox has
//     aria-label="Integrate {year_label} ({release})"
//   * the batch-submit button reads exactly "Integrate selected (N)" and is
//     disabled when N === 0
//
// Newer contract additions pinned below (disk headroom + progress + update +
// selected/focus styling — see individual test blocks for the full writeup):
//   * a disk meter at data-testid="disk-meter" grows as years are checked and
//     gets class "over" (+ disables the submit button) once the estimated
//     need exceeds disk.free_bytes from the catalog response
//   * a status:"update" card behaves like a selectable card (checkbox, badge
//     text "Update") even though integrated:true
//   * a selected card gets class "selected" + a child ".year-card__check";
//     merely FOCUSING a checkbox (no check) must NOT add "selected" — that's
//     a CSS-only :focus-within ring, not a JS-driven class
//   * per-year import progress (from a polled job's `progress` JSON) renders
//     one row per year at [data-testid="import-progress"] [data-year=...],
//     labeled with year_label + step; the numeric percent must NOT live
//     inside the aria-live/role=status region (only the overall phase text
//     is announced there)

const CATALOG = {
  probed_at: 1_700_000_000,
  partial: false,
  years: [
    { start_year: 2022, year: 2023, year_label: "2022-23", status: "integrated",
      integrated: true, available: true, release: "Final", selectable: false },
    { start_year: 2023, year: 2024, year_label: "2023-24", status: "final",
      integrated: false, available: true, release: "Final", selectable: true },
    { start_year: 2024, year: 2025, year_label: "2024-25", status: "provisional",
      integrated: false, available: true, release: "Provisional", selectable: true },
    { start_year: 2025, year: 2026, year_label: "2025-26", status: "unknown",
      integrated: false, available: false, release: null, selectable: false },
  ],
};

async function openImportsTab(page) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockImportJobs(page, []);
  await page.goto("/");
  await page.getByRole("button", { name: "Admin" }).click();
  await page.getByRole("button", { name: "Imports" }).click();
}

test("catalog renders a card per year with correct selectable/integrated state", async ({ page }) => {
  await mockImportCatalog(page, CATALOG);
  await openImportsTab(page);

  const integratedCard = page.locator('[data-year="2022"]');
  await expect(integratedCard).toHaveAttribute("data-status", "integrated");
  await expect(integratedCard).toHaveAttribute("aria-disabled", "true");
  await expect(integratedCard.locator('input[type="checkbox"]')).toHaveCount(0);

  const finalCard = page.locator('[data-year="2023"]');
  await expect(finalCard).toHaveAttribute("data-status", "final");
  await expect(finalCard).not.toHaveAttribute("aria-disabled", "true");
  await expect(page.getByRole("checkbox", { name: "Integrate 2023-24 (Final)" })).toBeVisible();

  const provisionalCard = page.locator('[data-year="2024"]');
  await expect(provisionalCard).toHaveAttribute("data-status", "provisional");
  await expect(page.getByRole("checkbox", { name: "Integrate 2024-25 (Provisional)" })).toBeVisible();

  const unavailableCard = page.locator('[data-year="2025"]');
  await expect(unavailableCard).toHaveAttribute("data-status", "unknown");
  await expect(unavailableCard).toHaveAttribute("aria-disabled", "true");
  await expect(unavailableCard.locator('input[type="checkbox"]')).toHaveCount(0);
});

test("selecting years updates the 'Integrate selected (N)' button, disabled at 0", async ({ page }) => {
  await mockImportCatalog(page, CATALOG);
  await openImportsTab(page);

  const submit = page.getByRole("button", { name: /^Integrate selected \(\d+\)$/ });
  await expect(submit).toHaveText("Integrate selected (0)");
  await expect(submit).toBeDisabled();

  await page.getByRole("checkbox", { name: "Integrate 2023-24 (Final)" }).click();
  await expect(submit).toHaveText("Integrate selected (1)");
  await expect(submit).toBeEnabled();

  await page.getByRole("checkbox", { name: "Integrate 2024-25 (Provisional)" }).click();
  await expect(submit).toHaveText("Integrate selected (2)");

  // Unchecking back to zero must re-disable it.
  await page.getByRole("checkbox", { name: "Integrate 2023-24 (Final)" }).click();
  await page.getByRole("checkbox", { name: "Integrate 2024-25 (Provisional)" }).click();
  await expect(submit).toHaveText("Integrate selected (0)");
  await expect(submit).toBeDisabled();
});

test("submitting selected years POSTs the right start_year list and job progress appears", async ({ page }) => {
  await mockImportCatalog(page, CATALOG);
  const integrate = await mockIntegrate(page, { jobId: 42, status: "pending" });
  await mockImportJobPoll(page, 42, [
    { id: 42, filename: "integrate", status: "running", log: "fetching…", report: null, updated_at: 1 },
    { id: 42, filename: "integrate", status: "swapped", log: "done", report: "ok", updated_at: 2 },
  ]);
  await openImportsTab(page);

  await page.getByRole("checkbox", { name: "Integrate 2023-24 (Final)" }).click();
  await page.getByRole("checkbox", { name: "Integrate 2024-25 (Provisional)" }).click();
  await page.getByRole("button", { name: /^Integrate selected \(\d+\)$/ }).click();

  await expect.poll(() => integrate.posts.length).toBe(1);
  expect(integrate.posts[0].years.slice().sort()).toEqual([2023, 2024]);

  // The job progress panel reflects the polled status, ending at "swapped".
  await expect(page.getByText("swapped")).toBeVisible();
});

test("a 409 response shows the already-running notice", async ({ page }) => {
  await mockImportCatalog(page, CATALOG);
  await mockIntegrate(page, { httpStatus: 409 });
  await openImportsTab(page);

  await page.getByRole("checkbox", { name: "Integrate 2023-24 (Final)" }).click();
  await page.getByRole("button", { name: /^Integrate selected \(\d+\)$/ }).click();

  await expect(page.getByText(/already running/i)).toBeVisible();
});

// ---------------------------------------------------------------------------
// Disk meter: grows as years are checked, turns "over" + disables submit once
// the estimated need exceeds disk.free_bytes.
//
// The numbers below are chosen so the exact arithmetic is known ahead of time
// (same formula as app/estimate.py / web/src/estimate.js — see
// eval/fixtures/estimate_cases.json for the shared ground truth): with
// already_integrated_count=0, live_db_bytes=0 (so per_year_db_bytes falls
// back to default_per_year_db_mb*MB = 380*1024*1024 = 398,458,880),
// expand_factor=3.0, safety_factor=1.2, and each selectable year's
// zip_bytes=50,000,000:
//   0 selected -> needed_with_safety_bytes = 0
//   1 selected -> needed_with_safety_bytes = 718,150,656
//   2 selected -> needed_with_safety_bytes = 1,436,301,312
// disk.free_bytes=1,000,000,000 sits strictly between the 1- and 2-selected
// thresholds, so checking a SECOND year is what must flip the meter "over".
// (DECISION, documented in the test-engineer's report: the client estimates
// against only the CHECKED years' zip_bytes, not the full rebuild union —
// this is a UX estimate, not the server's authoritative preflight check.)
// ---------------------------------------------------------------------------

const DISK_TEST_CATALOG = {
  probed_at: 1_700_000_000,
  partial: false,
  years: [
    { start_year: 2030, year: 2031, year_label: "2030-31", status: "final",
      integrated: false, available: true, release: "Final", selectable: true,
      zip_bytes: 50_000_000 },
    { start_year: 2031, year: 2032, year_label: "2031-32", status: "provisional",
      integrated: false, available: true, release: "Provisional", selectable: true,
      zip_bytes: 50_000_000 },
  ],
  disk: { free_bytes: 1_000_000_000, total_bytes: 2_000_000_000, used_bytes: 1_000_000_000 },
  calibration: {
    expand_factor: 3.0, default_per_year_db_mb: 380, bandwidth_mbps: 10.0,
    build_seconds_per_year: 60.0, safety_factor: 1.2,
    per_year_db_bytes: 398_458_880, live_db_bytes: 0, already_integrated_count: 0,
  },
};

test("disk meter grows as years are checked and turns over + disables submit past free space", async ({ page }) => {
  await mockImportCatalog(page, DISK_TEST_CATALOG);
  await openImportsTab(page);

  const meter = page.getByTestId("disk-meter");
  const submit = page.getByRole("button", { name: /^Integrate selected \(\d+\)$/ });

  // 0 selected: comfortably under free space.
  await expect(meter).not.toHaveClass(/over/);
  await expect(submit).toBeDisabled();

  // 1 selected (718,150,656 needed < 1,000,000,000 free): still fine.
  await page.getByRole("checkbox", { name: "Integrate 2030-31 (Final)" }).click();
  await expect(meter).not.toHaveClass(/over/);
  await expect(submit).toBeEnabled();

  // 2 selected (1,436,301,312 needed > 1,000,000,000 free): now over.
  await page.getByRole("checkbox", { name: "Integrate 2031-32 (Provisional)" }).click();
  await expect(meter).toHaveClass(/over/);
  await expect(submit).toBeDisabled();

  // Backing off to 1 selected must clear the "over" state again.
  await page.getByRole("checkbox", { name: "Integrate 2031-32 (Provisional)" }).click();
  await expect(meter).not.toHaveClass(/over/);
  await expect(submit).toBeEnabled();
});

// ---------------------------------------------------------------------------
// Per-file progress: one row per year, labeled by year_label + step. The
// numeric percent must live OUTSIDE the aria-live/role=status region — only
// the overall phase message is announced to assistive tech.
// ---------------------------------------------------------------------------

const PROGRESS_SEQUENCE = [
  {
    id: 77, filename: "integrate:2023,2024", status: "running", log: "", report: null,
    updated_at: 1,
    progress: JSON.stringify({
      overall: { phase: "downloading", message: "Downloading NCES releases…" },
      years: {
        2023: { start_year: 2023, year_label: "2023-24", step: "downloading",
               downloaded_bytes: 25_000_000, total_bytes: 50_000_000, pct: 50 },
        2024: { start_year: 2024, year_label: "2024-25", step: "queued",
               downloaded_bytes: 0, total_bytes: 60_000_000, pct: 0 },
      },
    }),
  },
  {
    id: 77, filename: "integrate:2023,2024", status: "swapped", log: "done", report: "ok",
    updated_at: 2,
    progress: JSON.stringify({
      overall: { phase: "done", message: "Import succeeded and is now live." },
      years: {
        2023: { start_year: 2023, year_label: "2023-24", step: "fetched",
               downloaded_bytes: 50_000_000, total_bytes: 50_000_000, pct: 100 },
        2024: { start_year: 2024, year_label: "2024-25", step: "fetched",
               downloaded_bytes: 60_000_000, total_bytes: 60_000_000, pct: 100 },
      },
    }),
  },
];

test("per-file progress renders one row per year (label + step); percent is not in the live region", async ({ page }) => {
  await mockImportCatalog(page, CATALOG);
  await mockIntegrate(page, { jobId: 77, status: "pending" });
  await mockImportJobPoll(page, 77, PROGRESS_SEQUENCE);
  await openImportsTab(page);

  await page.getByRole("checkbox", { name: "Integrate 2023-24 (Final)" }).click();
  await page.getByRole("checkbox", { name: "Integrate 2024-25 (Provisional)" }).click();
  await page.getByRole("button", { name: /^Integrate selected \(\d+\)$/ }).click();

  const progressPanel = page.getByTestId("import-progress");
  const row2023 = progressPanel.locator('[data-year="2023"]');
  const row2024 = progressPanel.locator('[data-year="2024"]');

  await expect(row2023).toContainText("2023-24");
  await expect(row2023).toContainText("downloading");
  await expect(row2023).toContainText("50%");
  // The visual bar fill must track pct — regression guard for the bug where it
  // sat at 0% the whole download.
  await expect(row2023.locator(".file-progress-fill")).toHaveAttribute("style", /width:\s*50%/);

  await expect(row2024).toContainText("2024-25");
  await expect(row2024).toContainText("queued");
  await expect(row2024.locator(".file-progress-fill")).toHaveAttribute("style", /width:\s*0%/);

  // The overall phase text IS announced… scoped to the active job's own
  // status region (inside .job), NOT a document-wide `.first()` match over
  // every [aria-live]/[role=status] node — the page can legitimately have
  // more than one (e.g. the "an import is running" banner is ALSO a
  // role="status" region for its own, unrelated reason: telling a
  // non-visual admin why the controls are locked). Scoping to .job's status
  // region is what specifically targets the overall-phase announcement this
  // test is about, and stays correct regardless of what other live regions
  // exist elsewhere on the page.
  const liveRegion = page.locator(".job").locator('[role="status"]');
  await expect(liveRegion).toHaveCount(1);
  await expect(liveRegion).toContainText(/downloading/i);
  // …but the per-year numeric percent must NOT be inside that live region.
  await expect(liveRegion).not.toContainText("50%");

  // Poll again -> reaches the terminal "done" phase with both years fetched.
  await expect(page.getByText("swapped")).toBeVisible();
  await expect(row2023).toContainText("fetched");
  await expect(row2024).toContainText("fetched");
  // A fetched year's bar is full.
  await expect(row2023.locator(".file-progress-fill")).toHaveAttribute("style", /width:\s*100%/);
});

// ---------------------------------------------------------------------------
// "Update available": an integrated year whose current NCES release is newer
// than what was integrated (status:"update") renders an Update badge and
// stays re-selectable.
// ---------------------------------------------------------------------------

const UPDATE_CATALOG = {
  probed_at: 1_700_000_000,
  partial: false,
  years: [
    { start_year: 2022, year: 2023, year_label: "2022-23", status: "update",
      integrated: true, available: true, release: "Final", selectable: true },
    { start_year: 2023, year: 2024, year_label: "2023-24", status: "integrated",
      integrated: true, available: true, release: "Final", selectable: false },
  ],
};

test("an 'update' status card shows an Update badge and is re-selectable", async ({ page }) => {
  await mockImportCatalog(page, UPDATE_CATALOG);
  await openImportsTab(page);

  const updateCard = page.locator('[data-year="2022"]');
  await expect(updateCard).toHaveAttribute("data-status", "update");
  await expect(updateCard).not.toHaveAttribute("aria-disabled", "true");
  // Exact (case-sensitive) match on the badge itself — a generic fallback
  // that just echoes the raw status string would render lowercase "update",
  // not the proper "Update" label, so this forces a real STATUS_TEXT entry
  // rather than passing on unstyled fallback text.
  await expect(updateCard.locator(".badge")).toHaveText(/Update/);

  const checkbox = page.getByRole("checkbox", { name: "Integrate 2022-23 (Final)" });
  await expect(checkbox).toBeVisible();

  const submit = page.getByRole("button", { name: /^Integrate selected \(\d+\)$/ });
  await checkbox.click();
  await expect(submit).toHaveText("Integrate selected (1)");

  // A plain (non-update) integrated card must still have no checkbox at all.
  const integratedCard = page.locator('[data-year="2023"]');
  await expect(integratedCard).toHaveAttribute("aria-disabled", "true");
  await expect(integratedCard.locator('input[type="checkbox"]')).toHaveCount(0);
});

// ---------------------------------------------------------------------------
// Selected vs. focused styling: selecting a card adds class "selected" plus
// a ".year-card__check" child glyph. Merely keyboard-focusing its checkbox
// (without checking it) must NOT add "selected" — the focus ring is a
// CSS-only :focus-within effect, not something the JS selection state drives.
// ---------------------------------------------------------------------------

test("selecting a card adds .selected + a check glyph; focusing alone does not select it", async ({ page }) => {
  await mockImportCatalog(page, CATALOG);
  await openImportsTab(page);

  const card = page.locator('[data-year="2023"]');
  const checkbox = page.getByRole("checkbox", { name: "Integrate 2023-24 (Final)" });

  // Focusing (e.g. via keyboard Tab) without checking must not select it.
  await checkbox.focus();
  await expect(card).not.toHaveClass(/(?:^|\s)selected(?:\s|$)/);
  await expect(card.locator(".year-card__check")).toHaveCount(0);

  // Checking DOES select it: class + check glyph both appear.
  await checkbox.click();
  await expect(card).toHaveClass(/(?:^|\s)selected(?:\s|$)/);
  await expect(card.locator(".year-card__check")).toHaveCount(1);

  // Unchecking removes both again.
  await checkbox.click();
  await expect(card).not.toHaveClass(/(?:^|\s)selected(?:\s|$)/);
  await expect(card.locator(".year-card__check")).toHaveCount(0);
});

// The card is the toggle now (no native checkbox), so a keyboard user must be
// able to focus it and flip selection with Space or Enter — the checkbox
// semantics we preserved via role=checkbox + aria-checked.
test("a selectable card toggles with Space/Enter and exposes aria-checked", async ({ page }) => {
  await mockImportCatalog(page, CATALOG);
  await openImportsTab(page);

  const card = page.getByRole("checkbox", { name: "Integrate 2023-24 (Final)" });
  await expect(card).toHaveAttribute("aria-checked", "false");

  await card.focus();
  await page.keyboard.press(" ");
  await expect(card).toHaveAttribute("aria-checked", "true");
  await expect(card).toHaveClass(/(?:^|\s)selected(?:\s|$)/);

  await page.keyboard.press(" ");
  await expect(card).toHaveAttribute("aria-checked", "false");

  await page.keyboard.press("Enter");
  await expect(card).toHaveAttribute("aria-checked", "true");
});
