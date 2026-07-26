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

  // Streamed answer renders as markdown, including the GFM table. (This is a
  // by-state comparison table, so compare mode adds a "Compare CA" checkbox cell —
  // match the data cell exactly so it doesn't also resolve that checkbox cell.)
  await expect(page.getByRole("table")).toBeVisible();
  await expect(page.getByRole("cell", { name: "CA", exact: true })).toBeVisible();
  await expect(page.getByRole("cell", { name: "100" })).toBeVisible();

  // SQL log is behind a toggle button; clicking it reveals a full-width panel
  // with the formatted, syntax-highlighted query (the formatter re-spaces the
  // source, so assert on a stable literal that survives reformatting).
  const sqlToggle = page.getByRole("button", { name: "SQL", exact: true });
  await expect(sqlToggle).toBeVisible();
  await sqlToggle.click();
  await expect(page.locator(".trace-panel .sqlblock")).toContainText("51.3801");

  // Each rendered table has its own CSV download button, and it now NAMES what
  // it will do. The stream carries a message id (see the note at the top), so
  // this single-table answer gets the SERVER export of the full result — not a
  // dump of the rows on screen. That distinction was invisible before.
  await expect(page.getByRole("button", { name: /Download full result/i })).toBeVisible();

  // The table has a numeric column, so "Chart this" is offered; toggling it
  // reveals the chart with a compact line/bar type <select>.
  const chartBtn = page.getByRole("button", { name: "Chart this" });
  await expect(chartBtn).toBeVisible();
  await chartBtn.click();
  const chartType = page.getByRole("combobox", { name: "Chart type" });
  await expect(chartType).toBeVisible();

  // The chart's role="img" must wrap ONLY the graphic. ARIA makes every
  // descendant of a role="img" presentational, so when it sat on the outer
  // <figure> it removed this <select>, the delta badge, and the data-label/
  // copy/maximize buttons from the accessibility tree — on screen, unreachable
  // by a screen reader. This asserts CONTAINMENT deliberately: Playwright's
  // role engine does not prune presentational children, so the getByRole
  // assertion two lines up passed happily while the bug was live and cannot be
  // the regression test for it.
  await expect(page.locator('[role="img"] .chart-head')).toHaveCount(0);
  await expect(page.locator("figure.chart .chart-graphic[role='img']")).toHaveCount(1);

  // The chart is rasterized to a PNG (hidden <img>) for clean HTML copy/paste.
  await expect(page.locator("img.chart-export-img"))
    .toHaveAttribute("src", /^data:image\/png/, { timeout: 5000 });

  // Switching the type must survive a re-render (e.g. a copy) — regression for
  // the chart remounting and snapping back to its default.
  await chartType.selectOption("bar");
  await expect(chartType).toHaveValue("bar");
  await page.getByRole("button", { name: "Copy", exact: true }).click();
  await page.getByRole("menuitem", { name: "Copy Markdown" }).click();
  await expect(chartType).toHaveValue("bar");
});

// EVERY field the `done` event carries must reach the rendered turn, live.
//
// The persisted-answer field list is hand-maintained across ~10 sites, and #236
// mechanized the BACKEND half — one list drives the INSERT, the SELECT and the
// done payload, asserted against the real messages schema. Nothing does that for
// the frontend: Chat.jsx's stream handler declares ~12 accumulators and spreads
// them into one 400-character object literal, all by hand.
//
// The failure is ASYMMETRIC, which is why it keeps getting through review: the
// RELOAD path inherits new fields for free (`...m` spread) while this live path
// enumerates them. A dropped field therefore renders CORRECTLY after a refresh
// and wrongly only on the turn that produced it — the hardest shape to notice,
// and precisely how `table_cells_matched` and `results_truncated` both shipped
// broken.
//
// This is the cheap half of that gate: one live turn carrying everything, with
// an assertion per field. It cannot prove the two ends agree the way the backend
// schema diff does, but it does catch a value that exists, is correct, and
// simply never reaches the component.
test("every field on the done event reaches the LIVE turn, not just a reload",
  async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    await mockStreamChat(page, {
      conversationId: CONV_ID, messageId: MSG_ID, userMessageId: MSG_ID - 1,
      sql: [SQL], answer: ANSWER_MD,
      // Value verbatim, with separators — Figure renders what the MODEL wrote
      // (the prompt asks for thousands separators); it applies no locale
      // formatting of its own.
      figure: { value: "12,345", unit: "degrees", label: "national total" },
      suggestions: ["Which state grew fastest?"],
      durationMs: 4200,
      figureGrounding: "exact",
      tableGrounding: "partial", tableCellsChecked: 22, tableCellsMatched: 9,
      resultsTruncated: true,
    });
    await mockConversation(page, CONV_ID, []);   // never reloaded; this is the live path

    await page.goto("/");
    await page.getByPlaceholder("Ask about IPEDS data…").fill("nursing degrees by state");
    await page.getByRole("button", { name: "Send" }).click();
    await expect(page.getByRole("table")).toBeVisible();

    // duration_ms -> "Thought for N seconds"
    await expect(page.getByText(/Thought for \d+ second/)).toBeVisible();
    // figure + figure_grounding -> the hero stat carrying its verified mark
    await expect(page.locator(".figure")).toContainText("12,345");
    // The mark lives in the figcaption, not the number itself.
    await expect(page.locator(".fig-verified")).toBeVisible();
    // table_grounding + BOTH counts -> the caution names the misses (13 of 22).
    // table_cells_matched is the field that shipped broken: the done event
    // carried it and Chat.jsx dropped it, so the count was right on reload and
    // absent live.
    await expect(page.locator(".table-trust")).toContainText("13 of 22");
    // suggestions -> the drill-down chips
    await expect(page.getByRole("button", { name: "Which state grew fastest?" })).toBeVisible();
    // results_truncated -> the "First 200 rows" caption. Until this PR the mock
    // could not even SEND this field, so the live path had no coverage at all.
    await expect(page.getByText(/First 200 rows/)).toBeVisible();
    // message_id -> the id that unlocks the server-side CSV
    await expect(page.getByRole("button", { name: /Download full result/i })).toBeVisible();
  });
