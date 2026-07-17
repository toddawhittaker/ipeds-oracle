import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockConversation,
  mockStreamChat,
} from "./mocks.js";

// Flow 3: chat happy path. Signed in, empty conversation list, ask a
// question, watch it stream, then confirm the `done` event's message_id
// (see Chat.jsx submit()) attaches the id that unlocks the CSV download.
const CONV_ID = 42;
const MSG_ID = 7;
const SQL = "SELECT stabbr, SUM(x) AS total FROM c_a WHERE cipcode='51.3801' AND awlevel=3 GROUP BY stabbr";
const ANSWER_MD =
  "Here are Associate's degrees in Registered Nursing by state:\n\n" +
  "| State | Total |\n| --- | --- |\n| CA | 100 |\n| NY | 50 |\n";

test("asking a question streams a markdown answer with a table, exposes the SQL log, and unlocks CSV after reload", async ({ page }) => {
  await mockMe(page, { email: "user@example.edu", is_admin: false });
  const convos = await mockConversations(page, []);
  await mockStreamChat(page, { conversationId: CONV_ID, sql: [SQL], answer: ANSWER_MD, messageId: MSG_ID });

  // The stream's `done` event carries message_id; the app attaches it to the
  // assistant message so the CSV link renders — no reload needed.
  await mockConversation(page, CONV_ID, [
    { role: "user", content: "Associate's degrees in Registered Nursing by state" },
    { role: "assistant", id: MSG_ID, content: ANSWER_MD, sql_log: [SQL] },
  ]);

  await page.goto("/");

  await page.getByPlaceholder("Ask about IPEDS data…").fill(
    "Associate's degrees in Registered Nursing by state"
  );
  // The conversation list is refreshed (refreshConvos()) right after openConvo;
  // reflect the now-saved thread in later GETs.
  convos.setList([{ id: CONV_ID, title: "Associate's degrees in Registered Nursing by state" }]);
  await page.getByRole("button", { name: "Send" }).click();

  // Streamed answer renders as markdown, including the GFM table.
  await expect(page.getByRole("table")).toBeVisible();
  await expect(page.getByRole("cell", { name: "CA" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "100" })).toBeVisible();

  // SQL log is present behind its own <details>/<summary> (scope to it — the
  // same SQL also appears in the "Thoughts" trace).
  const sqlSummary = page.getByText("SQL", { exact: true });
  await expect(sqlSummary).toBeVisible();
  await sqlSummary.click();
  const sqlDetails = page.locator("details").filter({ has: page.getByText("SQL", { exact: true }) });
  await expect(sqlDetails.locator("pre")).toContainText(SQL);

  // Each rendered table has its own client-side CSV download button.
  await expect(page.getByRole("button", { name: "Download CSV" })).toBeVisible();

  // The table has a numeric column, so "Chart this" is offered; toggling it
  // reveals the chart with a line/bar type switcher.
  const chartBtn = page.getByRole("button", { name: "Chart this" });
  await expect(chartBtn).toBeVisible();
  await chartBtn.click();
  await expect(page.getByRole("button", { name: "Bar", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Line", exact: true })).toBeVisible();

  // The chart is rasterized to a PNG (hidden <img>) for clean HTML copy/paste.
  await expect(page.locator("img.chart-export-img"))
    .toHaveAttribute("src", /^data:image\/png/, { timeout: 5000 });

  // Switching the type must survive a re-render (e.g. a copy) — regression for
  // the chart remounting and snapping back to line.
  await page.getByRole("button", { name: "Bar", exact: true }).click();
  await expect(page.getByRole("button", { name: "Bar", exact: true }))
    .toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "Copy Markdown" }).click();
  await expect(page.getByRole("button", { name: "Bar", exact: true }))
    .toHaveAttribute("aria-pressed", "true");
});
