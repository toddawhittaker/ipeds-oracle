import { test, expect } from "@playwright/test";
import { mockMe, mockConversations, mockAccessRequests, mockDeniedRequests } from "./mocks.js";

// Browser truth for the admin Users table. The pure filter/sort/paginate math is
// unit-tested in frontend/src/userlist.test.js (vitest); here we cover what only a
// browser gives: typing into the live search box, click-to-sort headers with
// aria-sort, the page-size select + Prev/Next window, and the icon actions
// (labels per row, the confirm-on-remove flow, the PATCH/DELETE requests).

// A stateful allowlist API: GET returns the current list; PATCH flips is_admin;
// DELETE drops the row — so load()'s refetch reflects the mutation the way the
// real backend would. Returns captured PATCH bodies + a live view of the rows.
async function mockUsersApi(page, initialRows) {
  let rows = [...initialRows];
  const patches = [];
  const deletes = [];
  await page.route("**/api/admin/allowlist", async (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(rows) });
    }
    return route.continue();
  });
  await page.route("**/api/admin/allowlist/*", async (route) => {
    const req = route.request();
    const email = decodeURIComponent(req.url().split("/allowlist/")[1].split("?")[0]);
    if (req.method() === "PATCH") {
      const body = req.postDataJSON();
      rows = rows.map((r) => (r.email === email ? { ...r, is_admin: body.is_admin } : r));
      patches.push({ email, is_admin: body.is_admin });
      return route.fulfill({ status: 200, contentType: "application/json",
        body: JSON.stringify({ ok: true, email, is_admin: body.is_admin }) });
    }
    if (req.method() === "DELETE") {
      rows = rows.filter((r) => r.email !== email);
      deletes.push(email);
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true }) });
    }
    return route.continue();
  });
  return { patches, deletes, getRows: () => rows };
}

async function openUsers(page, rows, me = { email: "admin@example.edu", is_admin: true }) {
  await mockMe(page, me);
  await mockConversations(page, []);
  await mockAccessRequests(page, []);
  await mockDeniedRequests(page, []);
  const api = await mockUsersApi(page, rows);
  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await expect(page.getByRole("heading", { name: "Users" })).toBeVisible();
  return api;
}

// A bulk list for pagination; a small labelled set for search/sort.
function bulkRows(n) {
  return Array.from({ length: n }, (_, i) => ({
    email: `user${String(i).padStart(2, "0")}@example.edu`,
    note: `note ${i}`, is_admin: false, last_login: 1_700_000_000 + i,
  }));
}

test("search filters live, shows the miss message, and clears", async ({ page }) => {
  await openUsers(page, [
    { email: "alice@example.edu", note: "Registrar", is_admin: false, last_login: 1700000000 },
    { email: "bob@example.edu", note: "Provost office", is_admin: false, last_login: 1700000000 },
  ]);

  const search = page.getByRole("searchbox", { name: "Search email or note" });
  // Matches the NOTE column, case-insensitively.
  await search.fill("provost");
  await expect(page.getByRole("cell", { name: "bob@example.edu" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "alice@example.edu" })).toHaveCount(0);

  // A miss shows the search-specific empty message (not "list is empty").
  await search.fill("zzz");
  await expect(page.getByText("No users match your search.")).toBeVisible();

  // The in-field clear control restores the full list and refocuses the box.
  await page.getByRole("button", { name: "Clear search" }).click();
  await expect(page.getByRole("cell", { name: "alice@example.edu" })).toBeVisible();
  await expect(search).toBeFocused();
});

test("a non-admin's Admin cell is blank — never a literal 0", async ({ page }) => {
  // Regression: is_admin is a NUMBER, so `{r.is_admin && ...}` rendered a stray
  // "0" in every non-admin's Admin cell.
  await openUsers(page, [
    { email: "plain@example.edu", note: "staff", is_admin: 0, last_login: null },
  ]);
  const adminCell = page.getByRole("row", { name: /plain@example\.edu/ }).getByRole("cell").nth(2);
  await expect(adminCell).toHaveText("");
  // The Actions column carries a visible header, not just an sr-only label.
  await expect(page.getByRole("columnheader", { name: "Actions" })).toBeVisible();
});

test("clicking a header sorts and toggles direction with aria-sort", async ({ page }) => {
  await openUsers(page, [
    { email: "carol@example.edu", note: "c", is_admin: false, last_login: 1700000000 },
    { email: "alice@example.edu", note: "a", is_admin: false, last_login: 1700000000 },
    { email: "bob@example.edu", note: "b", is_admin: false, last_login: 1700000000 },
  ]);

  // Default is Email ascending.
  const emailHeader = page.getByRole("columnheader", { name: /Email/ });
  await expect(emailHeader).toHaveAttribute("aria-sort", "ascending");
  const firstCell = () => page.getByRole("row").nth(1).getByRole("cell").first();
  await expect(firstCell()).toHaveText("alice@example.edu");

  // Click Email -> descending; alphabetically-last row floats to the top.
  await page.getByRole("button", { name: /^Email/ }).click();
  await expect(emailHeader).toHaveAttribute("aria-sort", "descending");
  await expect(firstCell()).toHaveText("carol@example.edu");
  // The sort change is announced (aria-sort alone is silent on activation).
  await expect(page.locator(".pager [aria-live]")).toHaveText(/Sorted by email, descending/);
});

test("pagination pages through and disables Prev/Next at the ends", async ({ page }) => {
  await openUsers(page, bulkRows(30));

  // Default 25/page -> page 1 of 2, Prev disabled. (Range text lives in both a
  // visible span and an sr-only status region, so scope to the visible one.)
  const range = page.locator(".pager-range");
  const table = page.locator("table.grid.users");
  await expect(range).toHaveText("Showing 1–25 of 30 users");
  await expect(page.getByText("Page 1 of 2")).toBeVisible();
  await expect(page.getByRole("button", { name: "Previous page" })).toBeDisabled();
  // A full page needs no spacer.
  await expect(page.locator("tbody tr.filler")).toHaveCount(0);
  const fullHeight = (await table.boundingBox()).height;

  await page.getByRole("button", { name: "Next page" }).click();
  await expect(range).toHaveText("Showing 26–30 of 30 users");
  await expect(page.getByText("Page 2 of 2")).toBeVisible();
  await expect(page.getByRole("button", { name: "Next page" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Previous page" })).toBeEnabled();
  // The short last page (5 of 25 rows) is padded to full height with one empty
  // row per missing slot, so the pager below doesn't shift when you page back —
  // the cursor stays over "‹ Prev".
  await expect(page.locator("tbody tr.filler")).toHaveCount(20);
  // The real invariant: the table is the SAME height on the short last page as on
  // a full page (to the pixel), so nothing below it moves. Guards against a future
  // row-height/button-size change re-introducing the drift.
  expect((await table.boundingBox()).height).toBe(fullHeight);
});

test("changing page size returns to page 1 and resizes the window", async ({ page }) => {
  await openUsers(page, bulkRows(30));
  await page.getByRole("button", { name: "Next page" }).click();
  await expect(page.getByText("Page 2 of 2")).toBeVisible();

  // Bumping the page size snaps back to page 1 with a single, larger page.
  await page.getByRole("combobox", { name: "Users per page" }).selectOption("50");
  await expect(page.locator(".pager-range")).toHaveText("Showing 1–30 of 30 users");
  await expect(page.getByText("Page 1 of 1")).toBeVisible();
  // A single page is never padded (that would leave a big empty gap).
  await expect(page.locator("tbody tr.filler")).toHaveCount(0);
});

test("Make admin issues a PATCH and the row becomes an admin", async ({ page }) => {
  const api = await openUsers(page, [
    { email: "colleague@example.edu", note: "staff", is_admin: false, last_login: 1700000000 },
  ]);

  await page.getByRole("button", { name: "Promote admin" }).click();
  await expect(page.getByText("✓ Admin", { exact: true })).toBeVisible();
  // The action moved to Demote admin now that they're an admin.
  await expect(page.getByRole("button", { name: "Demote admin" })).toBeVisible();
  expect(api.patches).toEqual([{ email: "colleague@example.edu", is_admin: true }]);
});

test("promoting returns focus to the row's action button, not the top notice", async ({ page }) => {
  // Guards the focus-restore-vs-reload race: the row persists (shield swaps
  // Promote->Demote admin) and focus must land back on that button after the
  // reload — not on <body> (briefly-disabled button) or the top flash notice.
  await openUsers(page, [
    { email: "colleague@example.edu", note: "staff", is_admin: false, last_login: 1700000000 },
  ]);
  await page.getByRole("button", { name: "Promote admin" }).click();
  const removeAdmin = page.getByRole("button", { name: "Demote admin" });
  await expect(removeAdmin).toBeVisible();
  await expect(removeAdmin).toBeFocused();
});

test("paging to the last page moves focus off the now-disabled Next", async ({ page }) => {
  await openUsers(page, bulkRows(30)); // 2 pages at 25/page
  await page.getByRole("button", { name: "Next page" }).click();
  const next = page.getByRole("button", { name: "Next page" });
  await expect(next).toBeDisabled();
  // Focus handed to the still-enabled sibling rather than dropped to <body>.
  await expect(page.getByRole("button", { name: "Previous page" })).toBeFocused();
});

test("Remove user confirms by email, then deletes the row", async ({ page }) => {
  const api = await openUsers(page, [
    { email: "colleague@example.edu", note: "staff", is_admin: false, last_login: 1700000000 },
    { email: "other@example.edu", note: "x", is_admin: false, last_login: 1700000000 },
  ]);

  // The confirmation modal must name the affected email. Its confirm button is
  // also "Remove user", so scope to the dialog (the row button is aria-hidden
  // behind the inert background while the modal is open anyway).
  await page.getByRole("row", { name: /colleague@example\.edu/ })
    .getByRole("button", { name: "Remove user" }).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("colleague@example.edu");
  await dialog.getByRole("button", { name: "Remove user" }).click();

  await expect(page.getByRole("cell", { name: "colleague@example.edu" })).toHaveCount(0);
  await expect(page.getByRole("cell", { name: "other@example.edu" })).toBeVisible();
  expect(api.deletes).toEqual(["colleague@example.edu"]);
  // The trash button unmounted with its row; focus must land on the search box,
  // not drop to <body> (the toast never takes focus).
  await expect(page.getByRole("searchbox", { name: "Search email or note" })).toBeFocused();
});

test("an admin's trash button is disabled + explains why; removing is a no-op until demoted", async ({ page }) => {
  // colleague is a DIFFERENT admin than the signed-in one, so their row shows
  // actions (your own row shows none). You must demote before you can remove.
  const api = await openUsers(page, [
    { email: "colleague@example.edu", note: "staff", is_admin: true, last_login: 1700000000 },
  ]);

  const trash = page.getByRole("button", { name: "Can't remove an admin — demote first" });
  await expect(trash).toBeVisible();
  await expect(trash).toHaveAttribute("aria-disabled", "true");

  // aria-disabled (not `disabled`) means a click still reaches the handler, but
  // removeUser early-returns for an admin: no confirm dialog opens, no DELETE
  // fires. force:true bypasses Playwright's own enabled-actionability wait so we
  // exercise that guard the way a real click would.
  await trash.click({ force: true });
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await page.waitForTimeout(150);
  expect(api.deletes).toEqual([]);

  // Demote first -> the same button becomes a live, enabled "Remove user".
  await page.getByRole("button", { name: "Demote admin" }).click();
  const liveTrash = page.getByRole("button", { name: "Remove user" });
  await expect(liveTrash).toBeVisible();
  await expect(liveTrash).not.toHaveAttribute("aria-disabled", "true");
});
