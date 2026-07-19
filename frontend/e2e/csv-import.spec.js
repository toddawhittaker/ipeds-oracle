import { test, expect } from "@playwright/test";
import { mockMe, mockConversations, mockAccessRequests, mockDeniedRequests } from "./mocks.js";

// Browser truth for the Users-tab CSV bulk import. The parse/normalize/dedupe/
// validate math is unit-tested in frontend/src/csvimport.test.js (vitest); here we
// cover what only a browser gives: the drag visual state, the file-input ->
// parse -> summary flow, the Confirm POST payload, the rendered error report, an
// unsupported file type, and the hoverable/focusable help popover (WCAG 1.4.13).
//
// A real OS file DROP can't be synthesized in Playwright, so (as in
// manual-import.spec.js) the drag visual is dispatched directly and file
// selection uses the input — which is also the accessible/keyboard path.

async function openImporter(page, { rows = [], bulk } = {}) {
  const bulkResponse = bulk || { ok: true, added: 0, admins_granted: 0, skipped: [] };
  const posts = [];
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAccessRequests(page, []);
  await mockDeniedRequests(page, []);
  await page.route("**/api/admin/allowlist", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(rows) }));
  // Registered after the list route so it wins for the /bulk sub-path.
  await page.route("**/api/admin/allowlist/bulk", (route) => {
    posts.push(route.request().postDataJSON());
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(bulkResponse) });
  });
  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await expect(page.getByRole("heading", { name: "Users" })).toBeVisible();
  await page.getByText("Import from CSV").click(); // expand the <details>
  return { posts };
}

const CSV = `email,note,admin
alex@example.com,Chair,yes
bob@example.com,,
alex@example.com,dup,
,noemail,
bad-email,x,
`;

test("dragging a file over the CSV drop zone toggles its active state", async ({ page }) => {
  await openImporter(page);
  const zone = page.locator(".csv-dropzone");
  await expect(zone).not.toHaveClass(/dragging/);
  await zone.dispatchEvent("dragenter");
  await expect(zone).toHaveClass(/dragging/);
  await zone.dispatchEvent("dragleave");
  await expect(zone).not.toHaveClass(/dragging/);
});

test("selecting a CSV parses it into a summary, then Confirm POSTs the ready rows", async ({ page }) => {
  const { posts } = await openImporter(page, {
    bulk: { ok: true, added: 2, admins_granted: 1, skipped: [], mail_configured: true } });

  await page.setInputFiles("#csv-file", { name: "roster.csv", mimeType: "text/csv", buffer: Buffer.from(CSV) });

  // Summary counts (2 ready, 1 dup, 2 invalid, 1 admin, 5 total).
  const summary = page.locator(".csv-summary");
  await expect(summary).toContainText("Total rows detected: 5");
  await expect(summary).toContainText("Users ready to add: 2");
  await expect(summary).toContainText("Existing or duplicate: 1");
  await expect(summary).toContainText("Invalid rows: 2");
  await expect(summary).toContainText("Receiving administrator access: 1");

  await page.getByRole("button", { name: /Add 2 users/ }).click();

  // The POST carried exactly the two ready rows (email lowercased, admin parsed,
  // blank note defaulted to "Imported on ...").
  expect(posts).toHaveLength(1);
  expect(posts[0].users).toEqual([
    { email: "alex@example.com", note: "Chair", is_admin: true },
    { email: "bob@example.com", note: expect.stringMatching(/^Imported on /), is_admin: false },
  ]);

  // Result + error report (2 invalid + 1 duplicate = 3 skipped rows). With mail
  // configured, the result tells the admin the imported users were emailed an
  // approval notice (no magic link — they request a sign-in link themselves).
  await expect(page.locator(".csv-result")).toContainText("2 users added");
  await expect(page.locator(".csv-result")).toContainText("emailed an approval notice");
  const reportRows = page.locator(".csv-report tbody tr");
  await expect(reportRows).toHaveCount(3);
  await expect(page.locator(".csv-report")).toContainText("missing email");
  await expect(page.locator(".csv-report")).toContainText("invalid email");
  await expect(page.locator(".csv-report")).toContainText("duplicate in file");

  // WCAG 2.4.3: the "Add N" button just unmounted, so focus must land on a
  // stable anchor in the result, not fall to <body>.
  await expect(page.getByRole("button", { name: "Import another file" })).toBeFocused();
});

test("an unsupported file type is rejected with an error and not parsed", async ({ page }) => {
  await openImporter(page);
  await page.setInputFiles("#csv-file", { name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("hello") });
  await expect(page.getByRole("alert")).toContainText(/not a \.csv/i);
  await expect(page.locator(".csv-summary")).toHaveCount(0);
});

test("the format help popover opens on focus, survives the pointer entering it, and closes on Escape", async ({ page }) => {
  await openImporter(page);
  const trigger = page.getByRole("button", { name: "CSV format help" });
  const pop = page.locator(".help-popover");

  await expect(pop).toBeHidden();
  await trigger.focus();
  await expect(pop).toBeVisible();
  await expect(pop).toContainText("yes, y, t, true, 1, x"); // the accepted-true set
  // Moving the pointer into the popover keeps it open (WCAG 1.4.13 hoverable).
  await pop.hover();
  await expect(pop).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(pop).toBeHidden();
});

test("the click that follows a focus-open does not toggle the popover shut (touch tap)", async ({ page }) => {
  await openImporter(page);
  const trigger = page.getByRole("button", { name: "CSV format help" });
  const pop = page.locator(".help-popover");
  // A touch tap fires focus (opens) then click; the click must be swallowed, not
  // toggle it back shut, or the help is unreachable on touch devices.
  await trigger.focus();
  await expect(pop).toBeVisible();
  await trigger.click();
  await expect(pop).toBeVisible();
});
