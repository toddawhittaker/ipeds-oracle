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
  await expect(range).toHaveText("Showing 1–25 of 30 users");
  await expect(page.getByText("Page 1 of 2")).toBeVisible();
  await expect(page.getByRole("button", { name: "Previous page" })).toBeDisabled();

  await page.getByRole("button", { name: "Next page" }).click();
  await expect(range).toHaveText("Showing 26–30 of 30 users");
  await expect(page.getByText("Page 2 of 2")).toBeVisible();
  await expect(page.getByRole("button", { name: "Next page" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Previous page" })).toBeEnabled();
});

test("changing page size returns to page 1 and resizes the window", async ({ page }) => {
  await openUsers(page, bulkRows(30));
  await page.getByRole("button", { name: "Next page" }).click();
  await expect(page.getByText("Page 2 of 2")).toBeVisible();

  // Bumping the page size snaps back to page 1 with a single, larger page.
  await page.getByRole("combobox", { name: "Users per page" }).selectOption("50");
  await expect(page.locator(".pager-range")).toHaveText("Showing 1–30 of 30 users");
  await expect(page.getByText("Page 1 of 1")).toBeVisible();
});

test("Make admin issues a PATCH and the row becomes an admin", async ({ page }) => {
  const api = await openUsers(page, [
    { email: "colleague@example.edu", note: "staff", is_admin: false, last_login: 1700000000 },
  ]);

  await page.getByRole("button", { name: "Make admin" }).click();
  await expect(page.getByText("✓ Admin", { exact: true })).toBeVisible();
  // The action moved to Remove admin now that they're an admin.
  await expect(page.getByRole("button", { name: "Remove admin" })).toBeVisible();
  expect(api.patches).toEqual([{ email: "colleague@example.edu", is_admin: true }]);
});

test("promoting returns focus to the row's action button, not the top notice", async ({ page }) => {
  // Guards the focus-restore-vs-reload race: the row persists (shield swaps
  // Make->Remove admin) and focus must land back on that button after the
  // reload — not on <body> (briefly-disabled button) or the top flash notice.
  await openUsers(page, [
    { email: "colleague@example.edu", note: "staff", is_admin: false, last_login: 1700000000 },
  ]);
  await page.getByRole("button", { name: "Make admin" }).click();
  const removeAdmin = page.getByRole("button", { name: "Remove admin" });
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

  // The confirm dialog must name the affected email.
  let dialogMessage = "";
  page.on("dialog", (d) => { dialogMessage = d.message(); d.accept(); });

  await page.getByRole("row", { name: /colleague@example\.edu/ })
    .getByRole("button", { name: "Remove user" }).click();

  await expect(page.getByRole("cell", { name: "colleague@example.edu" })).toHaveCount(0);
  await expect(page.getByRole("cell", { name: "other@example.edu" })).toBeVisible();
  expect(dialogMessage).toContain("colleague@example.edu");
  expect(api.deletes).toEqual(["colleague@example.edu"]);
});
