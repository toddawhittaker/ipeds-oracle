import { test, expect } from "@playwright/test";
import { gotoAdmin, mockMe, mockConversations, mockImportJobs, mockImportCatalog } from "./mocks.js";

// Browser truth for the NCES download check on the Imports tab. The year probes
// are HEAD requests; a proxy can pass those and still sever a real download,
// which is how a year showed as available and the integrate then failed. The
// backend now reports a `reachability` verdict with the catalog, and the page
// must surface a blocked download as an alert BEFORE the admin starts a job,
// and stay quiet when downloads work.
const BASE = {
  probed_at: 1_700_000_000, partial: false,
  years: [{ start_year: 2022, year: 2023, year_label: "2022-23", status: "final",
            integrated: false, available: true, release: "Final", selectable: true,
            zip_bytes: 1000 }],
};

async function openImports(page, catalog) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockImportJobs(page, []);
  await mockImportCatalog(page, catalog);
  await page.goto("/");
  await gotoAdmin(page);
  await page.getByRole("link", { name: "Imports" }).click();
}

test("a blocked download is announced as an alert naming the network, with the year still listed", async ({ page }) => {
  await openImports(page, {
    ...BASE,
    reachability: { ok: false, detail: "the connection to nces.ed.gov opened but was closed without an HTTP response" },
  });
  const alert = page.getByRole("alert").filter({ hasText: "Downloads from NCES are blocked" });
  await expect(alert).toBeVisible();
  await expect(alert).toContainText("closed without an HTTP response");
  await expect(alert).toContainText("network problem");
  // The catalog itself still renders — the block is a warning, not an outage.
  await expect(page.getByText("2022-23")).toBeVisible();
});

test("a working download shows no alert", async ({ page }) => {
  await openImports(page, { ...BASE, reachability: { ok: true, detail: null } });
  await expect(page.getByText("2022-23")).toBeVisible();
  await expect(page.getByTestId("nces-download-check")).toHaveCount(0);
});
