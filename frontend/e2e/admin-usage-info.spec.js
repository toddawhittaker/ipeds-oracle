import { test, expect } from "@playwright/test";
import {
  mockMe, mockConversations, mockAttention, mockMarkLogsSeen, mockUsage,
} from "./mocks.js";

// Admin → Usage: each statistic carries an ⓘ info popover (what it measures +
// which way is good), and a privacy line above the grid states the numbers stay
// local. The popover MECHANICS (focus-open, hover-survive, Escape, touch-tap) are
// the shared HelpPopover, already pinned in csv-import.spec.js — this pins the
// Usage-specific wiring: the copy, the direction guidance, and the privacy note.

async function gotoUsage(page) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAttention(page, { users: 0, skills: 0, logs: 0 });
  await mockMarkLogsSeen(page);
  await mockUsage(page, {
    bucket: "day",
    series: [],
    top_users: [],
    totals: {
      queries: 120, tokens: 8400, spend: 1.23, cache_hits: 9,
      escalations: 2, failures: 1,
      prompt_tokens: 8400, cached_prompt_tokens: 4200,
      first_call_prompt_tokens: 2000, first_call_cached_prompt_tokens: 1500,
      figures_checked: 8, figures_ungrounded: 1,
      table_cells_checked: 20, table_cells_matched: 18,
      emit_turns: 50, structured_turns: 50, leaked_turns: 0,
      exhausted_turns: 3, degraded_turns: 1,
    },
  });
  await page.goto("/admin/usage");
}

test("the privacy note states the metrics stay local", async ({ page }) => {
  await gotoUsage(page);
  await expect(page.getByText(/never leaves this machine/)).toBeVisible();
  await expect(page.getByText(/sent to a central server/i)).toBeVisible();
});

test("a stat's info popover explains it and says which way is good", async ({ page }) => {
  await gotoUsage(page);

  // Focus a stat's info trigger and return ITS popover (scoped via
  // aria-describedby — focusing a new trigger doesn't instantly close the prior
  // one, so a global "visible popover" selector would briefly match two).
  async function focusPopover(name) {
    const trigger = page.getByRole("button", { name });
    await expect(trigger).toBeVisible();
    await trigger.focus();
    const id = await trigger.getAttribute("aria-describedby");
    return page.locator(`[id="${id}"]`);
  }

  // "Grounded figures" — a data-integrity rate where higher is better.
  const figures = await focusPopover("What “Grounded figures” measures");
  await expect(figures).toBeVisible();
  await expect(figures).toContainText("reproduce from those results");
  await expect(figures).toContainText("Higher is better.");
  await page.keyboard.press("Escape");
  await expect(figures).toBeHidden();

  // "Exhausted" — a count where lower is better, with the raise-the-ceiling tip.
  const exhausted = await focusPopover("What “Exhausted” measures");
  await expect(exhausted).toContainText("Lower is better.");
  await expect(exhausted).toContainText("LLM_MAX_TOOL_ITERS");

  // "Queries" — a plain count, neither direction is inherently good.
  const queries = await focusPopover("What “Queries” measures");
  await expect(queries).toContainText("neither high nor low");
});

test("a left-column stat's popover is nudged to stay inside the viewport", async ({ page }) => {
  // Regression: the popover anchors right:0 (grows leftward), so the top-left
  // "Queries" stat used to flow off the left edge. HelpPopover now nudges it back.
  await gotoUsage(page);
  const trigger = page.getByRole("button", { name: "What “Queries” measures" });
  await trigger.focus();
  const id = await trigger.getAttribute("aria-describedby");
  const pop = page.locator(`[id="${id}"]`);
  await expect(pop).toBeVisible();
  const box = await pop.boundingBox();
  expect(box.x).toBeGreaterThanOrEqual(0); // fully inside the left edge
  const viewport = page.viewportSize();
  expect(box.x + box.width).toBeLessThanOrEqual(viewport.width); // ...and the right
});
