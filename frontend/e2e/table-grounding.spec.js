import { expect, test } from "@playwright/test";

import { contrastRatio } from "./contrast.js";
import {
  mockAttention, mockConversation, mockConversations, mockMe, mockStreamChat, mockVersion,
} from "./mocks.js";

// Browser truth for "did this answer's numbers come from the query?".
//
// The server already graded every numeric MEASURE cell of an answer's tables
// against the rows the turn actually queried (grounding.check_table), but the
// verdict only ever reached usage_log — an admin stat. The person about to copy
// those numbers into a report saw nothing. This is the reader-facing half.
//
// The silence cases matter as much as the mark. tableTrustNote is POSITIVE-ONLY
// and that is measured, not lazy: a "% change" column is computed across a row
// while every reconciler op runs down a column, so a table whose every number is
// CORRECT grades `partial` — or `unmatched` when that derived column is the only
// measure. A caution keyed on either would call correct answers wrong. Pinned
// server-side in backend/tests/test_grounding.py ("KNOWN BLIND SPOT") and in
// tabletruth.test.js; these specs hold the line in the browser.

const USER = { email: "user@example.edu", is_admin: false };

const TABLE = [
  "| Institution | Awards |",
  "| --- | --- |",
  "| Alpha University | 120 |",
  "| Beta College | 340 |",
].join("\n");

function convo(extra) {
  return [
    { id: 1, role: "user", content: "top institutions by awards" },
    {
      id: 2, role: "assistant", content: `Here are the leaders.\n\n${TABLE}`,
      sql_log: ["SELECT instnm, awards FROM c_a"],
      ...extra,
    },
  ];
}

async function open(page, extra) {
  await mockMe(page, USER);
  await mockVersion(page);
  await mockAttention(page);
  await mockConversations(page, [{ id: 5, title: "Awards", updated_at: 0 }]);
  await mockConversation(page, 5, convo(extra));
  await page.goto("/chat/5");
  await expect(page.getByRole("table")).toBeVisible();
}

const mark = (page) => page.locator(".table-trust");

test.describe("table grounding mark", () => {
  test("a fully reproduced answer says so, with the count, after a reload", async ({ page }) => {
    await open(page, {
      table_grounding: "matched", table_cells_checked: 4, table_cells_matched: 4,
    });
    await expect(mark(page)).toBeVisible();
    await expect(mark(page)).toContainText("4 values reproduced from the query result");
    // Reproduction, not correctness — the promise has to stay narrow.
    await expect(mark(page)).toHaveAttribute("title", /not that the query/i);
  });

  test("the mark is NOT inside the copy surface", async ({ page }) => {
    // Same contract as the hero figure: copying an answer must yield the answer,
    // not our annotation. `.md` is the node the copy actions target.
    await open(page, {
      table_grounding: "matched", table_cells_checked: 4, table_cells_matched: 4,
    });
    await expect(mark(page)).toBeVisible();
    expect(await page.locator(".md .table-trust").count()).toBe(0);
  });

  test("a partial or unmatched verdict shows NOTHING — no mark and no warning",
    async ({ page }) => {
      // The false-alarm guard. A correct answer carrying a row-wise % change
      // column lands here, so anything rendered would be an accusation.
      for (const [status, checked, matched] of [["partial", 22, 9], ["unmatched", 15, 0]]) {
        await open(page, {
          table_grounding: status, table_cells_checked: checked, table_cells_matched: matched,
        });
        await expect(page.getByRole("table")).toBeVisible();
        expect(await mark(page).count()).toBe(0);
        // Nothing warning-shaped anywhere in the answer either.
        expect(await page.locator(".msg.assistant .warn").count()).toBe(0);
      }
    });

  test("an ungraded answer shows nothing", async ({ page }) => {
    // `unchecked` (no retained rows to compare) and a pre-migration/cached
    // message (all null) are both "we didn't look" — never a claim.
    await open(page, { table_grounding: "unchecked", table_cells_checked: 0 });
    expect(await mark(page).count()).toBe(0);
    await open(page, {});
    await expect(page.getByRole("table")).toBeVisible();
    expect(await mark(page).count()).toBe(0);
  });

  test("a live turn shows the mark without waiting for a reload", async ({ page }) => {
    // The done-event half. Without it the mark would only appear on reopen,
    // which is exactly the gap #215 closed for the figure.
    await mockMe(page, USER);
    await mockVersion(page);
    await mockAttention(page);
    await mockConversations(page, []);
    await mockStreamChat(page, {
      conversationId: 9, messageId: 2, userMessageId: 1,
      answer: `Here are the leaders.\n\n${TABLE}`,
      tableGrounding: "matched", tableCellsChecked: 4,
    });
    await page.goto("/");
    await page.getByRole("textbox", { name: /ask/i }).fill("top institutions by awards");
    await page.keyboard.press("Enter");
    await expect(mark(page)).toBeVisible();
    await expect(mark(page)).toContainText("4 values reproduced");
  });

  for (const theme of ["light", "dark"]) {
    test(`the mark meets AA contrast in the ${theme} theme`, async ({ page }) => {
      // The axe scan cannot cover this element: it only renders when the mocked
      // conversation carries a grounding verdict, which a11y.spec.js's fixtures
      // don't. So the scan is green whatever colour this ends up. Measured
      // directly for that reason -- and the light theme has only ~0.07 of
      // headroom over the 4.5 floor, so a token retune could cross it silently.
      await page.emulateMedia({ colorScheme: theme });
      await open(page, {
        table_grounding: "matched", table_cells_checked: 4, table_cells_matched: 4,
      });
      await expect(mark(page)).toBeVisible();
      const ratio = await contrastRatio(page, ".table-trust");
      expect(ratio, `.table-trust contrast was ${ratio?.toFixed(2)}:1 in ${theme}`)
        .toBeGreaterThanOrEqual(4.5);
    });
  }

  test("a live turn the server could not verify stays silent", async ({ page }) => {
    await mockMe(page, USER);
    await mockVersion(page);
    await mockAttention(page);
    await mockConversations(page, []);
    await mockStreamChat(page, {
      conversationId: 9, messageId: 2, userMessageId: 1,
      answer: `Here are the leaders.\n\n${TABLE}`,
      tableGrounding: "partial", tableCellsChecked: 4,
    });
    await page.goto("/");
    await page.getByRole("textbox", { name: /ask/i }).fill("top institutions by awards");
    await page.keyboard.press("Enter");
    await expect(page.getByRole("table")).toBeVisible();
    expect(await mark(page).count()).toBe(0);
  });
});
