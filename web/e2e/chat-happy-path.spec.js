import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockConversation,
  mockStreamChat,
  mockFeedback,
} from "./mocks.js";

// Flow 3: chat happy path. Signed in, empty conversation list, ask a
// question, watch it stream, then confirm the `done` event's message_id
// (see Chat.jsx submit()) attaches the id that unlocks feedback + CSV download.
const CONV_ID = 42;
const MSG_ID = 7;
const SQL = "SELECT stabbr, SUM(x) AS total FROM c_a WHERE cipcode='51.3801' AND awlevel=3 GROUP BY stabbr";
const ANSWER_MD =
  "Here are Associate's degrees in Registered Nursing by state:\n\n" +
  "| State | Total |\n| --- | --- |\n| CA | 100 |\n| NY | 50 |\n";

test("asking a question streams a markdown answer with a table, exposes the SQL log, and unlocks feedback + CSV after reload", async ({ page }) => {
  await mockMe(page, { email: "user@franklin.edu", is_admin: false });
  const convos = await mockConversations(page, []);
  await mockStreamChat(page, { conversationId: CONV_ID, sql: [SQL], answer: ANSWER_MD, messageId: MSG_ID });
  const feedback = await mockFeedback(page);

  // The stream's `done` event carries message_id; the app attaches it to the
  // assistant message so the 👍/👎 buttons and CSV link render — no reload needed.
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

  // The done event's message_id unlocks feedback + CSV controls.
  const csvLink = page.getByRole("link", { name: "Download CSV" });
  await expect(csvLink).toBeVisible();
  await expect(csvLink).toHaveAttribute("href", `/api/chat/messages/${MSG_ID}/download.csv`);

  // exact: true — "Helpful" is otherwise a substring match of "Not helpful".
  const upvote = page.getByTitle("Helpful", { exact: true });
  const downvote = page.getByTitle("Not helpful");
  await expect(upvote).toBeVisible();
  await expect(downvote).toBeVisible();

  await upvote.click();

  await expect.poll(() => feedback.posts.length).toBe(1);
  expect(feedback.posts[0]).toEqual({ value: 1 });
  await expect(upvote).toHaveClass(/\bon\b/);
});
