import { test, expect } from "@playwright/test";
import { mockMe, mockConversations, mockAttention, mockMarkLogsSeen } from "./mocks.js";

// Admin → Usage: the Source filter picks which door onto the agent the numbers
// describe — the web chat, the MCP `ask` tool, or both (usage_log.source,
// migration 37). Before this, one blended total made a runaway script on the
// MCP endpoint indistinguishable from web traffic (issue #362).
//
// This is browser truth, not pure logic: the regression is a control that
// renders and toggles its pressed state while the effect never refires, so the
// buttons move and the numbers underneath silently do not. Only a real fetch
// can catch that, which is why it is here and not in vitest.

const TOTALS = {
  all: { queries: 30, spend: 3 },
  web: { queries: 20, spend: 1 },
  mcp: { queries: 10, spend: 2 },
};

/** Serves per-source totals and records the `source` param of every request. */
async function mockUsageBySource(page) {
  const asked = [];
  await page.route("**/api/admin/usage*", async (route) => {
    const src = new URL(route.request().url()).searchParams.get("source");
    asked.push(src);
    const t = TOTALS[src || "all"];
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        bucket: "day",
        series: [],
        top_users: [],
        totals: {
          queries: t.queries, tokens: 100, spend: t.spend,
          cache_hits: 0, escalations: 0, failures: 0,
          prompt_tokens: 100, cached_prompt_tokens: 0,
          first_call_prompt_tokens: 0, first_call_cached_prompt_tokens: 0,
          figures_checked: 0, figures_ungrounded: 0,
          table_cells_checked: 0, table_cells_matched: 0,
          emit_turns: 0, structured_turns: 0, leaked_turns: 0,
          exhausted_turns: 0, degraded_turns: 0,
        },
      }),
    });
  });
  return { get asked() { return asked; } };
}

async function gotoUsage(page) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAttention(page, { users: 0, skills: 0, logs: 0 });
  await mockMarkLogsSeen(page);
  const usage = await mockUsageBySource(page);
  await page.goto("/admin/usage");
  return usage;
}

/** The "Queries" stat card's value. */
function queriesValue(page) {
  return page.locator(".stat", { hasText: /^Queries/ }).locator(".v");
}

test("choosing a source refetches and the numbers actually change", async ({ page }) => {
  const usage = await gotoUsage(page);
  const group = page.getByRole("group", { name: "Source" });

  await expect(queriesValue(page)).toHaveText("30");
  // The default request carries no `source` at all, so an admin who never
  // touches this control sends exactly the request this endpoint always got.
  expect(usage.asked).toEqual([null]);

  await group.getByRole("button", { name: "MCP" }).click();
  await expect(queriesValue(page)).toHaveText("10");
  expect(usage.asked).toContain("mcp");

  await group.getByRole("button", { name: "Web chat" }).click();
  await expect(queriesValue(page)).toHaveText("20");
  expect(usage.asked).toContain("web");

  // Back to All: the value returns, proving the filter is a live query and not
  // a one-way narrowing the admin has to reload out of.
  await group.getByRole("button", { name: "All" }).click();
  await expect(queriesValue(page)).toHaveText("30");
});

test("the source control reports which door is selected", async ({ page }) => {
  await gotoUsage(page);
  const group = page.getByRole("group", { name: "Source" });
  const all = group.getByRole("button", { name: "All" });
  const mcp = group.getByRole("button", { name: "MCP" });

  // aria-pressed is the only thing telling a screen-reader user which door the
  // numbers describe — without it the three buttons are indistinguishable and
  // the figures are unattributed.
  await expect(all).toHaveAttribute("aria-pressed", "true");
  await expect(mcp).toHaveAttribute("aria-pressed", "false");

  await mcp.click();
  await expect(mcp).toHaveAttribute("aria-pressed", "true");
  await expect(all).toHaveAttribute("aria-pressed", "false");
});
