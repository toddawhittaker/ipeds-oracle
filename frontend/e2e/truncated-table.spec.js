import { expect, test } from "@playwright/test";

import { mockAttention, mockConversation, mockConversations, mockMe, mockVersion } from "./mocks.js";

// Browser truth for "a truncated table tells the truth about itself".
//
// A 200-row PAGE of a larger result was byte-identical on screen to a complete
// 200-row result, and clicking a header to sort it returned "the biggest of the
// first 200" under a lit accent caret. The only disclosure was whatever sentence
// the model remembered to write.

const USER = { email: "user@example.edu", is_admin: false };

const TABLE = [
  "| Institution | Awards |",
  "| --- | --- |",
  "| Alpha University | 120 |",
  "| Beta College | 340 |",
  "| Gamma Institute | 90 |",
].join("\n");

function convo({ truncated }) {
  return [
    { id: 1, role: "user", content: "top institutions by awards" },
    {
      id: 2, role: "assistant", content: `Here are the leaders.\n\n${TABLE}`,
      sql_log: ["SELECT instnm, awards FROM c_a"],
      results_truncated: truncated ? 1 : 0,
    },
  ];
}

async function open(page, { truncated }) {
  await mockMe(page, USER);
  await mockVersion(page);
  await mockAttention(page);
  await mockConversations(page, [{ id: 5, title: "Awards", updated_at: 0 }]);
  await mockConversation(page, 5, convo({ truncated }));
  await page.goto("/chat/5");
}

test.describe("a truncated result says so", () => {
  test("captions the table without inventing a total", async ({ page }) => {
    await open(page, { truncated: true });
    const caption = page.locator(".table-caption");
    await expect(caption).toBeVisible();
    await expect(caption).toContainText(/first 200 rows/i);
    await expect(caption).toContainText(/larger/i);
    // Nothing computes a real total, so the caption must never imply one.
    await expect(caption).not.toContainText(/of \d/i);
  });

  test("a complete result carries no caption at all", async ({ page }) => {
    await open(page, { truncated: false });
    await expect(page.locator(".table-caption")).toHaveCount(0);
    // …and the table still renders normally.
    await expect(page.locator("th .th-sort").first()).toBeVisible();
  });
});

test.describe("sorting a truncated table warns that it isn't a ranking", () => {
  test("the note appears on sort, in a warning tone, naming the cap",
    async ({ page }) => {
      await open(page, { truncated: true });
      // Nothing before the user sorts.
      await expect(page.locator(".sort-note")).toHaveCount(0);

      await page.locator("th .th-sort").filter({ hasText: "Awards" }).click();

      const note = page.locator(".sort-note");
      await expect(note).toBeVisible();
      await expect(note).toContainText(/not a ranking/i);
      await expect(note).toContainText("200");
      await expect(note).toHaveClass(/warn/);
    });

  test("a complete table keeps the mild note, not the warning", async ({ page }) => {
    await open(page, { truncated: false });
    await page.locator("th .th-sort").filter({ hasText: "Awards" }).click();
    const note = page.locator(".sort-note");
    await expect(note).toBeVisible();
    await expect(note).not.toContainText(/not a ranking/i);
    await expect(note).not.toHaveClass(/warn/);
  });
});

test.describe("the CSV button says what it will actually do", () => {
  test("an answer that ran NO query offers the rows on screen, not a server "
    + "export it cannot produce", async ({ page }) => {
    // FOUND LIVE (conversation 23, turn 3): a follow-up that reshaped the
    // previous table from context ran no SQL at all — sql_log was []. The
    // frontend chose the SERVER export purely from `messageId != null &&
    // tableCount === 1`, never asking whether a query existed, so the button
    // was offered and its only possible outcome was the server's 400,
    // "No query is associated with this answer."
    //
    // The table is still on screen, so the honest fallback is the client-side
    // export of those rows — and the LABEL is the tell: "these N rows" rather
    // than "full result".
    await mockMe(page, USER);
    await mockVersion(page);
    await mockAttention(page);
    await mockConversations(page, [{ id: 5, title: "Awards", updated_at: 0 }]);
    await mockConversation(page, 5, [
      { id: 1, role: "user", content: "regroup that by year" },
      { id: 2, role: "assistant", content: `Reshaped.\n\n${TABLE}`, sql_log: [] },
    ]);

    // Any hit on the server export is a failure of this contract.
    let serverCalls = 0;
    await page.route("**/download.csv**", async (route) => {
      serverCalls += 1;
      await route.fulfill({
        status: 400, contentType: "application/json",
        body: JSON.stringify({ detail: "No query is associated with this answer." }),
      });
    });

    await page.goto("/chat/5");
    await expect(page.getByRole("button", { name: /Download these 3 rows/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /Download full result/i })).toHaveCount(0);

    await page.getByRole("button", { name: /Download these 3 rows/i }).click();
    // Client-side export: no request, and therefore no error toast.
    expect(serverCalls).toBe(0);
    await expect(page.getByText(/No query is associated/)).toHaveCount(0);
  });


  test("a single-table answer offers the FULL result, and a failure toasts "
    + "instead of navigating away", async ({ page }) => {
    await open(page, { truncated: true });
    const btn = page.getByRole("button", { name: /Download full result/i });
    await expect(btn).toBeVisible();

    // THE REGRESSION: this used to be a bare <a href> with no download
    // attribute, so a 504 replaced the whole chat view with a JSON error page.
    await page.route("**/download.csv**", async (route) => {
      await route.fulfill({
        status: 504, contentType: "application/json",
        body: JSON.stringify({ detail: "The query took too long to export." }),
      });
    });
    await btn.click();

    await expect(page.locator(".toast")).toContainText(/too long/i);
    // The chat is still there — the whole point.
    await expect(page.getByText("Here are the leaders.")).toBeVisible();
    expect(new URL(page.url()).pathname).toBe("/chat/5");
  });
});
