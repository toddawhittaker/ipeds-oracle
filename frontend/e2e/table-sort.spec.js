import { test, expect } from "@playwright/test";
import { mockMe, mockConversations, mockConversation } from "./mocks.js";

// Click-to-sort result tables. Display-only: the pure sort logic lives in
// src/tabledata.test.js (sortRows / columnIsNumeric); this pins the browser
// flow — clicking a header reorders the visible rows and updates aria-sort,
// cycling asc → desc → original (query) order.

// A comparable ranking table (entity rows), so it also carries the leading
// compare checkbox column — the University cell is therefore the 2nd <td>.
const RANKING = "Largest public universities:\n\n"
  + "| University | Enrollment |\n|---|---|\n"
  + "| Ohio State | 60,000 |\n| Michigan | 50,000 |\n| Penn State | 48,000 |\n";

async function open(page) {
  await mockMe(page, { email: "u@example.edu", is_admin: false });
  await mockConversations(page, []);
  await mockConversation(page, 9, [
    { role: "user", content: "largest public universities?" },
    { role: "assistant", content: RANKING },
  ]);
  await page.goto("/chat/9");
  await expect(page.getByRole("table")).toBeVisible();
}

// The University column top-to-bottom (2nd cell — the 1st is the compare checkbox).
const order = (page) => page.locator(".md tbody tr td:nth-child(2)").allInnerTexts();

test("a numeric header sorts the displayed rows asc → desc → original", async ({ page }) => {
  await open(page);
  expect(await order(page)).toEqual(["Ohio State", "Michigan", "Penn State"]); // query order

  const enroll = page.getByRole("button", { name: "Enrollment", exact: true });
  const header = page.getByRole("columnheader", { name: "Enrollment" });

  await enroll.click(); // asc by enrollment: 48k, 50k, 60k
  expect(await order(page)).toEqual(["Penn State", "Michigan", "Ohio State"]);
  await expect(header).toHaveAttribute("aria-sort", "ascending");

  await enroll.click(); // desc: 60k, 50k, 48k
  expect(await order(page)).toEqual(["Ohio State", "Michigan", "Penn State"]);
  await expect(header).toHaveAttribute("aria-sort", "descending");

  await enroll.click(); // third click restores the original order
  expect(await order(page)).toEqual(["Ohio State", "Michigan", "Penn State"]);
  await expect(header).toHaveAttribute("aria-sort", "none");
});

test("a text header sorts alphabetically", async ({ page }) => {
  await open(page);
  await page.getByRole("button", { name: "University", exact: true }).click();
  expect(await order(page)).toEqual(["Michigan", "Ohio State", "Penn State"]);
});

test("cells render inline markdown — a link stays clickable, a total stays bold", async ({ page }) => {
  // code-#8: SortableTable renders cells from their hast nodes, so inline markup
  // in a data cell (a website link, a bold total) is preserved, not flattened.
  const md = "Sites:\n\n| Institution | Note |\n|---|---|\n"
    + "| [UT Austin](https://utexas.edu) | **flagship** |\n"
    + "| Rice | private |\n";
  await mockMe(page, { email: "u@example.edu", is_admin: false });
  await mockConversations(page, []);
  await mockConversation(page, 9, [{ role: "assistant", content: md }]);
  await page.goto("/chat/9");
  await expect(page.getByRole("table")).toBeVisible();

  const link = page.getByRole("link", { name: "UT Austin" });
  await expect(link).toHaveAttribute("href", "https://utexas.edu");
  await expect(page.locator("td strong").filter({ hasText: "flagship" })).toBeVisible();
});
