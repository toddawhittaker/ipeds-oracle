import { test, expect } from "@playwright/test";
import { mockMe, mockConversations, mockImportJobs, mockImportCatalog } from "./mocks.js";

// Browser truth for the manual-upload drag-and-drop. The backend guard +
// multi-file rebuild logic is unit-tested in backend/tests/test_importer.py;
// here we cover what only a browser can: the drop zone's visual dragging state,
// and that the keyboard-accessible file input feeds a MULTI-file POST to
// /api/admin/import. (A real OS file *drop* can't be synthesized in Playwright,
// so the drag visual state is dispatched directly and file selection uses the
// input — which is also the accessible/keyboard path.)
const CATALOG = { probed_at: 1_700_000_000, partial: false, years: [] };

async function openManualUpload(page) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockImportJobs(page, []);
  await mockImportCatalog(page, CATALOG);
  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByRole("link", { name: "Imports" }).click();
  await page.getByText("Manual upload", { exact: false }).click(); // expand the <details>
}

test("drop zone shows a visual dragging state on dragover and clears on dragleave", async ({ page }) => {
  await openManualUpload(page);
  const zone = page.locator(".dropzone");
  await expect(zone).not.toHaveClass(/dragging/);
  await zone.dispatchEvent("dragover");
  await expect(zone).toHaveClass(/dragging/);
  await zone.dispatchEvent("dragleave");
  await expect(zone).not.toHaveClass(/dragging/);
});

test("selecting multiple .accdb files uploads them all as one batch", async ({ page }) => {
  await openManualUpload(page);

  let postBody = null;
  await page.route("**/api/admin/import", async (route) => {
    postBody = route.request().postData();
    await route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ job_id: 7, status: "pending" }),
    });
  });
  // Keep the post-upload job poll from hitting the real network.
  await page.route("**/api/admin/import/jobs/7", (route) =>
    route.fulfill({ status: 200, contentType: "application/json",
      body: JSON.stringify({ id: 7, status: "swapped" }) }));

  await page.setInputFiles("#import-file", [
    { name: "IPEDS202021.accdb", mimeType: "application/octet-stream", buffer: Buffer.from("a") },
    { name: "IPEDS202122.accdb", mimeType: "application/octet-stream", buffer: Buffer.from("b") },
  ]);

  // Both files are listed, and the submit reflects the count.
  await expect(page.getByText("IPEDS202021.accdb")).toBeVisible();
  await expect(page.getByText("IPEDS202122.accdb")).toBeVisible();
  const submit = page.getByRole("button", { name: /Rebuild from 2 files/ });
  await expect(submit).toBeEnabled();
  await submit.click();

  // The POST carried BOTH files as multipart parts named "files".
  await expect.poll(() => postBody).not.toBeNull();
  expect(postBody).toContain('name="files"');
  expect(postBody).toContain("IPEDS202021.accdb");
  expect(postBody).toContain("IPEDS202122.accdb");
});
