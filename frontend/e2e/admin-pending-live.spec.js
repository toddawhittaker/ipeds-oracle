import { test, expect } from "@playwright/test";
import {
  gotoAdmin,
  mockMe,
  mockConversations,
  mockAllowlist,
  mockAccessRequests,
  mockDeniedRequests,
} from "./mocks.js";

// The Allowlist tab refreshes its three tables live so a request filed by
// someone ELSE (POST /api/auth/request) -- or a change made in another admin
// session -- shows up without a manual page reload. Two mechanisms share one
// load(): an instant refresh when the admin returns to the tab
// (visibilitychange / window focus) and a light background poll while visible.
// These specs drive the deterministic visibility path (the poll runs the same
// load(), on a timer). Regression: before this, the list was fetched once on
// mount and only re-fetched after the admin's OWN action, so a newly-arrived
// pending request was invisible until F5.

async function openAllowlist(page, { reqs = [], denied = [], allowlist = [] } = {}) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAllowlist(page, allowlist);
  const reqsHandle = await mockAccessRequests(page, reqs);
  await mockDeniedRequests(page, denied);
  await page.goto("/");
  await gotoAdmin(page);
  return { reqsHandle };
}

const NEWCOMER = {
  id: 7, email: "newcomer@example.edu", reason: null, status: "pending", created_at: 1_700_000_000,
};

test.describe("Allowlist live refresh", () => {
  test("a request filed while the panel is open appears on tab return -- no reload", async ({ page }) => {
    const { reqsHandle } = await openAllowlist(page, { reqs: [] });
    await page.getByRole("tab", { name: /Pending requests/ }).click();

    // Nothing pending yet -> the pending panel's zero-state.
    await expect(page.getByText("No access requests are awaiting review.")).toBeVisible();

    // A request arrives from someone else; the admin took no action here.
    reqsHandle.setList([NEWCOMER]);

    // Returning to the tab fires the same load() the poll uses -- no navigation.
    await page.evaluate(() => globalThis.document.dispatchEvent(new Event("visibilitychange")));

    // The new row appears with its actions, and the zero-state is gone -- all
    // without a page reload (this test never calls page.reload()).
    await expect(page.getByRole("cell", { name: "newcomer@example.edu", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Approve request from newcomer@example.edu" })).toBeVisible();
    await expect(page.getByText("No access requests are awaiting review.")).toHaveCount(0);
  });

  test("a live refresh does not steal focus from the pending search box", async ({ page }) => {
    // With a pending row present the pending table (and its search box) render.
    await openAllowlist(page, { reqs: [NEWCOMER] });
    await page.getByRole("tab", { name: /Pending requests/ }).click();

    const search = page.getByRole("searchbox", { name: "Search pending requests by email" });
    await search.focus();
    await expect(search).toBeFocused();

    // A background refresh re-renders the table; focus must stay put (the
    // focus-restore-vs-reload race drops it to <body> when a reload commits on
    // top of a focus move -- a live poll must never do that).
    await page.evaluate(() => globalThis.document.dispatchEvent(new Event("visibilitychange")));
    await page.waitForTimeout(150);
    await expect(search).toBeFocused();
  });
});
