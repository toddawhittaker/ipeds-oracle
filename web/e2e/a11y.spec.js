import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import {
  mockMe,
  mockRequestLink,
  mockAuthConfig,
  mockConversations,
  mockConversation,
  mockStreamChat,
  mockAllowlist,
  mockAccessRequests,
  mockImportJobs,
} from "./mocks.js";

// Coverage for the a11y fixes the implementer landed across App.jsx, Chat.jsx,
// Login.jsx, Admin.jsx and Markdown.jsx. Every assertion here uses role/label/
// aria selectors against the real rendered app (via the existing /api/** mocks)
// rather than CSS, so a regression that removes an aria attribute or a <label>
// association fails the test, not just a visual/CSS check.

const SQL = "SELECT stabbr, SUM(x) AS total FROM c_a WHERE cipcode='51.3801' AND awlevel=3 GROUP BY stabbr";
const ANSWER_MD =
  "Here are Associate's degrees in Registered Nursing by state:\n\n" +
  "| State | Total |\n| --- | --- |\n| CA | 100 |\n| NY | 50 |\n";

// Shared setup for the chat-answer-dependent tests below: ask a question and
// wait for the streamed answer + follow-up conversation fetch (which attaches
// the message id) to land, same sequencing as chat-happy-path.spec.js.
async function askAndUnlockAnswer(page, { convId = 42, msgId = 7 } = {}) {
  await mockMe(page, { email: "user@example.edu", is_admin: false });
  const convos = await mockConversations(page, []);
  await mockStreamChat(page, { conversationId: convId, sql: [SQL], answer: ANSWER_MD, messageId: msgId });
  await mockConversation(page, convId, [
    { role: "user", content: "Associate's degrees in Registered Nursing by state" },
    { role: "assistant", id: msgId, content: ANSWER_MD, sql_log: [SQL] },
  ]);

  await page.goto("/");
  await page.getByPlaceholder("Ask about IPEDS data…").fill(
    "Associate's degrees in Registered Nursing by state"
  );
  convos.setList([{ id: convId, title: "Associate's degrees in Registered Nursing by state" }]);
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("table")).toBeVisible();
}

test.describe("conversation list items", () => {
  test("are real buttons reachable by accessible name; active one gets aria-current", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [
      { id: 1, title: "CA nursing associate's degrees" },
      { id: 2, title: "CS bachelor's degrees" },
    ]);
    await mockConversation(page, 1, [{ role: "user", content: "CA nursing associate's degrees" }]);

    await page.goto("/");

    // Before the a11y fix these were click-only <div>s with no button role.
    const convoBtn = page.getByRole("button", { name: "CA nursing associate's degrees" });
    await expect(convoBtn).toBeVisible();
    await expect(convoBtn).not.toHaveAttribute("aria-current", "page");

    await convoBtn.click();
    await expect(convoBtn).toHaveAttribute("aria-current", "page");
  });
});

test.describe("streamed answer live region", () => {
  test("assistant answer container has aria-live", async ({ page }) => {
    await askAndUnlockAnswer(page);

    // The div wrapping an assistant message's content (Chat.jsx) carries
    // aria-live so screen readers announce the streamed answer; it has no
    // implicit ARIA role of its own, so we check the attribute directly
    // rather than via getByRole.
    const liveRegion = page.locator(".msg.assistant .bubble > div[aria-live]");
    await expect(liveRegion).toHaveAttribute("aria-live", "polite");
  });
});

test.describe("labeled inputs", () => {
  test("Login email field is reachable via role+label", async ({ page }) => {
    await mockMe(page, null);
    await mockAuthConfig(page, "");
    await page.goto("/");

    await expect(page.getByRole("textbox", { name: /email/i })).toBeVisible();
    await expect(page.getByLabel(/email/i)).toBeVisible();
  });

  test("Chat composer is reachable by its label", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    await page.goto("/");

    await expect(page.getByLabel(/ask about ipeds data/i)).toBeVisible();
  });

  test("Admin allowlist email input is reachable by label", async ({ page }) => {
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockConversations(page, []);
    await mockAllowlist(page, []);
    await mockAccessRequests(page, []);

    await page.goto("/");
    await page.getByRole("button", { name: "Admin" }).click();

    await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
  });
});

test.describe("tabs selected state", () => {
  test("active primary nav tab and active Admin subtab expose aria-current", async ({ page }) => {
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockConversations(page, []);
    await mockAllowlist(page, []);
    await mockAccessRequests(page, []);
    await mockImportJobs(page, []);

    await page.goto("/");

    const chatTab = page.getByRole("button", { name: "Chat", exact: true });
    const adminTab = page.getByRole("button", { name: "Admin" });
    await expect(chatTab).toHaveAttribute("aria-current", "page");
    await expect(adminTab).not.toHaveAttribute("aria-current", "page");

    await adminTab.click();
    await expect(adminTab).toHaveAttribute("aria-current", "page");
    await expect(chatTab).not.toHaveAttribute("aria-current", "page");

    const allowlistSub = page.getByRole("button", { name: "Allowlist" });
    const importsSub = page.getByRole("button", { name: "Imports" });
    await expect(allowlistSub).toHaveAttribute("aria-current", "page");
    await expect(importsSub).not.toHaveAttribute("aria-current", "page");

    await importsSub.click();
    await expect(importsSub).toHaveAttribute("aria-current", "page");
    await expect(allowlistSub).not.toHaveAttribute("aria-current", "page");
  });
});

test.describe("result table region", () => {
  test("markdown result-table wrapper is a focusable, labeled region", async ({ page }) => {
    await askAndUnlockAnswer(page);

    const region = page.getByRole("region", { name: "Result table" });
    await expect(region).toBeVisible();
    await expect(region).toHaveAttribute("tabindex", "0");
    await expect(region.locator("table")).toBeVisible();
  });
});

test.describe("Admin landmark + login alert", () => {
  test("Admin view has a main landmark", async ({ page }) => {
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockConversations(page, []);
    await mockAllowlist(page, []);
    await mockAccessRequests(page, []);

    await page.goto("/");
    await page.getByRole("button", { name: "Admin" }).click();

    await expect(page.getByRole("main")).toBeVisible();
  });

  test("Login notice becomes an alert after a link request", async ({ page }) => {
    await mockMe(page, null);
    await mockAuthConfig(page, "");
    await mockRequestLink(page, "Check your email for a sign-in link.");

    await page.goto("/");
    await page.getByPlaceholder("you@yourschool.edu").fill("admin@example.edu");
    await page.getByRole("button", { name: "Email me a sign-in link" }).click();

    await expect(page.getByRole("alert")).toHaveText("Check your email for a sign-in link.");
  });
});

// axe-core smoke tests. @axe-core/playwright installed cleanly offline (npm
// registry was reachable), so these run as part of the normal suite rather
// than being skipped. Scoped to *critical*-impact violations only, since a
// broad zero-violations assertion would be brittle against third-party
// markup (react-markdown output, etc.) this suite doesn't control.
test.describe("axe smoke scan", () => {
  test("Login screen has no critical violations", async ({ page }) => {
    await mockMe(page, null);
    await mockAuthConfig(page, "");
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "IPEDS Query" })).toBeVisible();

    const results = await new AxeBuilder({ page }).analyze();
    const critical = results.violations.filter((v) => v.impact === "critical");
    expect(critical, JSON.stringify(critical, null, 2)).toEqual([]);
  });

  test("Chat screen has no critical violations", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    await page.goto("/");
    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeVisible();

    const results = await new AxeBuilder({ page }).analyze();
    const critical = results.violations.filter((v) => v.impact === "critical");
    expect(critical, JSON.stringify(critical, null, 2)).toEqual([]);
  });
});
