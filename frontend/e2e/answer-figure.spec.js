import { test, expect } from "@playwright/test";
import { mockMe, mockConversations, mockStreamChat, mockConversation } from "./mocks.js";

// The signature "figure": a typeset hero statistic rendered ABOVE an answer when
// the model emitted one (backend parses its ```figure fence into a structured
// `figure` event / persisted column). Browser truth: it renders above the prose,
// stays OUT of the copy surface, survives a reload like sql_log/thinking, and is
// absent when the answer carries no figure. The pure normalizer is unit-tested in
// src/figure.test.js.

const FIGURE = {
  value: "7,679", unit: "degrees",
  label: "CS bachelor's · CA publics · 2024", source: "IPEDS Completions",
};
// A single-number answer's "brief": hero figure (above) + synopsis + a recent-years
// breakdown table + a trend chart — all composed in one answer. 3+ points so the
// fitted trend line (needs ≥3) and the %-change delta both render.
const ANSWER = "California publics awarded **7,679** CS degrees — up from 6,100 in 2022.\n\n"
  + "| Year | N |\n|---|---|\n| 2022 | 6,100 |\n| 2023 | 6,900 |\n| 2024 | 7,679 |\n\n"
  + "```chart\n"
  + '{"type":"line","x":"year","y":"n","title":"CS bachelors — CA publics",'
  + '"data":[{"year":2022,"n":6100},{"year":2023,"n":6900},{"year":2024,"n":7679}]}\n'
  + "```";

async function signedIn(page) {
  await mockMe(page, { email: "u@example.edu", is_admin: false, trust_llm_provider: true });
  await mockConversations(page, []);
}

test("a figure renders above the prose and outside the copy surface", async ({ page }) => {
  await signedIn(page);
  await mockStreamChat(page, {
    conversationId: 7, answer: ANSWER, figure: FIGURE, messageId: 1, userMessageId: 2 });
  await page.goto("/");
  await page.getByPlaceholder("Ask about IPEDS data…").fill("how many CS degrees?");
  await page.getByRole("button", { name: "Send" }).click();

  const fig = page.locator(".answer-figure");
  await expect(fig).toBeVisible();
  await expect(fig).toContainText("7,679");
  await expect(fig).toContainText("CS bachelor's · CA publics · 2024");
  await expect(fig).toContainText("IPEDS Completions");
  // Above the prose: the figure precedes the .md answer as an earlier sibling.
  await expect(page.locator(".answer-figure ~ .md")).toBeVisible();
  // The brief composes: hero figure + recent-years table + trend chart, with the
  // table and chart PAIRED side by side (.brief-figrow)...
  await expect(page.locator(".brief-figrow .table-block")).toBeVisible();
  await expect(page.locator(".brief-figrow figure.chart")).toBeVisible();
  // A few-column table shares the row (side by side), NOT stacked.
  await expect(page.locator(".brief-figrow.stacked")).toHaveCount(0);
  // ...and the redundant "Chart this" toggle dropped (a chart is already shown),
  // while Download CSV stays.
  await expect(page.getByRole("button", { name: "Chart this" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Download CSV" })).toBeVisible();
  // Trend intelligence: a %-change delta badge + the chart-type <select> defaulting
  // to "Line + trend" (trend on by default; it's a line subtype in the dropdown).
  await expect(page.locator(".chart-delta")).toContainText("%");
  await expect(page.getByRole("combobox", { name: "Chart type" })).toHaveValue("line-trend");
  // Outside the copy surface: the figure is NOT inside the .md node copy targets.
  await expect(page.locator(".md .answer-figure")).toHaveCount(0);
});

// A wider brief table can't share a row without its nowrap cells sliding UNDER the
// chart, so the pair STACKS — chart below the full-width table. 4 columns is the
// regression boundary (Markdown.jsx: headers.length > 3): a 4-column ranking table
// used to sit side-by-side and overlap the chart.
const WIDE_ANSWER = "Degrees by level and year.\n\n"
  + "| Year | Bachelor's | Master's | Total |\n"
  + "|---|---|---|---|\n"
  + "| 2022 | 6,100 | 2,000 | 8,100 |\n"
  + "| 2023 | 6,900 | 2,100 | 9,000 |\n"
  + "| 2024 | 7,679 | 2,200 | 9,879 |\n\n"
  + "```chart\n"
  + '{"type":"line","x":"year","y":"total","title":"Total degrees",'
  + '"data":[{"year":2022,"total":8100},{"year":2023,"total":9000},{"year":2024,"total":9879}]}\n'
  + "```";

test("a 4-column brief stacks the chart below the table (no overlap)", async ({ page }) => {
  await signedIn(page);
  await mockStreamChat(page, {
    conversationId: 12, answer: WIDE_ANSWER, figure: FIGURE, messageId: 1, userMessageId: 2 });
  await page.goto("/");
  await page.getByPlaceholder("Ask about IPEDS data…").fill("degrees by level?");
  await page.getByRole("button", { name: "Send" }).click();

  // Still one paired brief (table + chart), but flagged .stacked (4 columns > 3).
  await expect(page.locator(".brief-figrow.stacked")).toBeVisible();
  await expect(page.locator(".brief-figrow.stacked .table-block")).toBeVisible();
  await expect(page.locator(".brief-figrow.stacked figure.chart")).toBeVisible();
});

// A tall table (many rows) also stacks even with few columns — the chart is short,
// so side-by-side would strand it next to a long scroll of rows (Markdown.jsx:
// rows.length > 8).
const TALL_ANSWER = "Enrollment by year.\n\n"
  + "| Year | N |\n|---|---|\n"
  + Array.from({ length: 10 }, (_, i) => `| ${2015 + i} | ${1000 + i * 10} |`).join("\n")
  + "\n\n```chart\n"
  + '{"type":"line","x":"year","y":"n","title":"Enrollment",'
  + '"data":[{"year":2015,"n":1000},{"year":2016,"n":1010},{"year":2017,"n":1020}]}\n'
  + "```";

test("a tall (many-row) brief stacks the chart below the table", async ({ page }) => {
  await signedIn(page);
  await mockStreamChat(page, {
    conversationId: 13, answer: TALL_ANSWER, figure: FIGURE, messageId: 1, userMessageId: 2 });
  await page.goto("/");
  await page.getByPlaceholder("Ask about IPEDS data…").fill("enrollment by year?");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.locator(".brief-figrow.stacked")).toBeVisible();
});

test("a figure survives a reload (persisted like sql_log)", async ({ page }) => {
  await signedIn(page);
  await mockConversation(page, 9, [
    { role: "user", content: "how many CS degrees?" },
    { role: "assistant", content: ANSWER, sql_log: ["SELECT 1"], figure: FIGURE },
  ]);
  await page.goto("/chat/9");
  const fig = page.locator(".answer-figure");
  await expect(fig).toBeVisible();
  await expect(fig).toContainText("7,679");
  await expect(fig).toContainText("CS bachelor's · CA publics · 2024");
});

test("an answer with no figure renders no hero statistic", async ({ page }) => {
  await signedIn(page);
  await mockStreamChat(page, {
    conversationId: 8, answer: ANSWER, messageId: 1, userMessageId: 2 });
  await page.goto("/");
  await page.getByPlaceholder("Ask about IPEDS data…").fill("show me the top 20");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.getByRole("cell", { name: "6,900" })).toBeVisible(); // answer rendered
  await expect(page.locator(".answer-figure")).toHaveCount(0);
});

test("drill-down chips render and clicking one asks it as a follow-up", async ({ page }) => {
  await signedIn(page);
  const chat = await mockStreamChat(page, {
    conversationId: 7, answer: ANSWER, figure: FIGURE,
    suggestions: ["How does this compare to Texas?", "Which schools led in 2024?"],
    messageId: 1, userMessageId: 2 });
  await page.goto("/");
  await page.getByPlaceholder("Ask about IPEDS data…").fill("how many CS degrees?");
  await page.getByRole("button", { name: "Send" }).click();

  const chip = page.getByRole("button", { name: "How does this compare to Texas?" });
  await expect(chip).toBeVisible();
  // Clicking a chip submits it as a NEW turn — the stream POST carries the question.
  await chip.click();
  await expect.poll(() => chat.calls.length).toBe(2);
  expect(chat.calls[1].question).toBe("How does this compare to Texas?");
});

test("drill-down chips survive a reload (persisted like the figure)", async ({ page }) => {
  await signedIn(page);
  await mockConversation(page, 9, [
    { role: "user", content: "how many CS degrees?" },
    { role: "assistant", content: ANSWER, sql_log: ["SELECT 1"], figure: FIGURE,
      suggestions: ["Compare to Texas?", "Which schools led?"] },
  ]);
  await page.goto("/chat/9");
  await expect(page.getByRole("button", { name: "Compare to Texas?" })).toBeVisible();
});

// The chart-type dropdown lists all applicable options at once — picking "Bar"
// must NOT hide "Line + trend" (the old bug forced a detour back through "Line").
test("chart-type dropdown keeps Line + trend available even while Bar is selected", async ({ page }) => {
  await signedIn(page);
  await mockStreamChat(page, {
    conversationId: 20, answer: ANSWER, figure: FIGURE, messageId: 1, userMessageId: 2 });
  await page.goto("/");
  await page.getByPlaceholder("Ask about IPEDS data…").fill("cs?");
  await page.getByRole("button", { name: "Send" }).click();

  const sel = page.getByRole("combobox", { name: "Chart type" });
  await expect(sel.locator("option")).toHaveText(["Line", "Line + trend", "Bar"]);
  await sel.selectOption("bar");
  // Still all three — so you can jump straight from Bar to a trended line.
  await expect(sel.locator("option")).toHaveText(["Line", "Line + trend", "Bar"]);
  await sel.selectOption("line-trend");
  await expect(sel).toHaveValue("line-trend");
});

// Maximize opens the chart in a modal (browser truth: focus trap + return).
test("maximize opens the chart in a modal; Escape closes and restores focus", async ({ page }) => {
  await signedIn(page);
  await mockStreamChat(page, {
    conversationId: 21, answer: ANSWER, figure: FIGURE, messageId: 1, userMessageId: 2 });
  await page.goto("/");
  await page.getByPlaceholder("Ask about IPEDS data…").fill("cs?");
  await page.getByRole("button", { name: "Send" }).click();

  const maxBtn = page.getByRole("button", { name: "Maximize chart" });
  await expect(maxBtn).toBeVisible();
  await maxBtn.click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  // The modal's chart keeps its own type control but offers NO nested maximize.
  await expect(dialog.getByRole("combobox", { name: "Chart type" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Maximize chart" })).toHaveCount(0);

  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(maxBtn).toBeFocused(); // focus returns to the opener
});
