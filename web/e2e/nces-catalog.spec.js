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
  await mockMe(page, { email: "admin@franklin.edu", is_admin: true });
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

  await page.getByRole("checkbox", { name: "Integrate 2023-24 (Final)" }).check();
  await expect(submit).toHaveText("Integrate selected (1)");
  await expect(submit).toBeEnabled();

  await page.getByRole("checkbox", { name: "Integrate 2024-25 (Provisional)" }).check();
  await expect(submit).toHaveText("Integrate selected (2)");

  // Unchecking back to zero must re-disable it.
  await page.getByRole("checkbox", { name: "Integrate 2023-24 (Final)" }).uncheck();
  await page.getByRole("checkbox", { name: "Integrate 2024-25 (Provisional)" }).uncheck();
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

  await page.getByRole("checkbox", { name: "Integrate 2023-24 (Final)" }).check();
  await page.getByRole("checkbox", { name: "Integrate 2024-25 (Provisional)" }).check();
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

  await page.getByRole("checkbox", { name: "Integrate 2023-24 (Final)" }).check();
  await page.getByRole("button", { name: /^Integrate selected \(\d+\)$/ }).click();

  await expect(page.getByText(/already running/i)).toBeVisible();
});
