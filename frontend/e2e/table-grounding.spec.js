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
// Two verdicts render: `matched` reassures, `partial`/`unmatched` ask the reader
// to check. The caution is phrased as an INSTRUCTION ("Check N values against the
// SQL or CSV") rather than a claim, because every time it fired on real data it
// was a gap in the checker rather than a model error — an instruction survives
// being wrong, a verdict does not.
//
// What still must NEVER render is the third case: a verdict with nothing behind
// it. `unchecked`/`no_table` compared nothing, and a status whose counts are
// NULL (a pre-migration message) is not a failure — Number(null) being 0 makes
// that one look exactly like "0 of N matched". Those silence cases are asserted
// below alongside the caution, because they are the same false-alarm risk the
// caution was held back for. See frontend/src/tabletruth.js for the wording
// contract.

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

  test("a partial verdict asks the reader to check, naming the count",
    async ({ page }) => {
      await open(page, {
        table_grounding: "partial", table_cells_checked: 22, table_cells_matched: 9,
      });
      await expect(mark(page)).toBeVisible();
      await expect(mark(page)).toHaveClass(/warn/);
      // 13 needs checking, not the 9 that passed; the total keeps the scale honest.
      await expect(mark(page)).toContainText("13 of 22");
      // An INSTRUCTION, never a verdict on the numbers. Every real firing so far
      // was a gap in the checker, so the line has to survive being wrong.
      await expect(mark(page)).toContainText(/^Check\b/);
      await expect(mark(page)).toContainText(/SQL or CSV/);
    });

  test("an unmatched verdict cautions without reading as a failed answer",
    async ({ page }) => {
      // The answer is still an answer; some of its numbers want checking. It must
      // not borrow the --danger treatment a genuinely failed turn uses, or every
      // caution reads as "this reply broke".
      await open(page, {
        table_grounding: "unmatched", table_cells_checked: 15, table_cells_matched: 0,
      });
      await expect(mark(page)).toBeVisible();
      await expect(mark(page)).toContainText("15 values");
      expect(await page.locator(".msg.assistant.failed").count()).toBe(0);
    });

  test("a failure verdict with NULL counts stays silent", async ({ page }) => {
    // The false-alarm guard that survives the caution. A pre-migration message
    // carries a status with NULL counts; Number(null) is 0, so a naive check
    // reads it as "0 of N matched" and accuses an answer nothing ever graded.
    await open(page, { table_grounding: "unmatched", table_cells_checked: 15 });
    await expect(page.getByRole("table")).toBeVisible();
    expect(await mark(page).count()).toBe(0);
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
    test(`the CAUTION meets AA contrast in the ${theme} theme`, async ({ page }) => {
      // --warn on the bubble background is a different pairing from the ✓ mark's
      // --ok, and axe cannot cover either: the element only renders when the
      // mocked conversation carries a verdict, which a11y.spec.js's fixtures
      // don't. A caution nobody can read is worse than no caution.
      await page.emulateMedia({ colorScheme: theme });
      await open(page, {
        table_grounding: "partial", table_cells_checked: 22, table_cells_matched: 9,
      });
      await expect(mark(page)).toBeVisible();
      const ratio = await contrastRatio(page, ".table-trust");
      expect(ratio, `.table-trust.warn contrast was ${ratio?.toFixed(2)}:1 in ${theme}`)
        .toBeGreaterThanOrEqual(4.5);
    });

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

  test("a live turn the server could not verify cautions without a reload",
    async ({ page }) => {
      // The done-event half of the caution. table_cells_matched rides that event
      // and the client used to DROP it, so the caution could name a count on
      // reload but not on the turn that produced it.
      await mockMe(page, USER);
      await mockVersion(page);
      await mockAttention(page);
      await mockConversations(page, []);
      await mockStreamChat(page, {
        conversationId: 9, messageId: 2, userMessageId: 1,
        answer: `Here are the leaders.\n\n${TABLE}`,
        tableGrounding: "partial", tableCellsChecked: 4, tableCellsMatched: 1,
      });
      await page.goto("/");
      await page.getByRole("textbox", { name: /ask/i }).fill("top institutions by awards");
      await page.keyboard.press("Enter");
      await expect(mark(page)).toBeVisible();
      await expect(mark(page)).toHaveClass(/warn/);
      await expect(mark(page)).toContainText("3 of 4");
    });
});
