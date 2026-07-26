import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { contrastRatio } from "./contrast.js";
import {
  gotoAdmin,
  mockMe,
  mockRequestLink,
  mockAuthConfig,
  mockConversations,
  mockConversation,
  mockStreamChat,
  mockAllowlist,
  mockAccessRequests,
  mockImportJobs,
  mockAttention,
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
  test("are real links reachable by accessible name; active one gets aria-current", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [
      { id: 1, title: "CA nursing associate's degrees" },
      { id: 2, title: "CS bachelor's degrees" },
    ]);
    await mockConversation(page, 1, [{ role: "user", content: "CA nursing associate's degrees" }]);

    await page.goto("/");

    // Before the a11y fix these were click-only <div>s with no button role;
    // now they're real react-router <a> links (see frontend/e2e/nav-links.spec.js
    // for the full link-conversion contract). exact:true -- getByRole
    // name-matching is substring by default, and the row's own trash-button
    // aria-label ("Delete chat: <title>", added for the delete-focus a11y
    // fix -- see frontend/e2e/delete-focus.spec.js) now CONTAINS this bare title,
    // so an unscoped substring match would hit both controls (strict-mode
    // violation). This still tests the same intent -- the row is a real
    // link, reachable by its accessible name -- exact matching just
    // disambiguates it from its sibling.
    const convoBtn = page.getByRole("link", { name: "CA nursing associate's degrees", exact: true });
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

  // A11Y-6: the streaming bubble is aria-busy while pending, and a screen reader
  // skips a busy region's content — so a separate status region OUTSIDE any busy
  // region must announce that generation is underway. This region announces the
  // wait and then clears; regressing it (removing the region, or nesting it in
  // the aria-busy bubble) fails here.
  test("a non-visual 'generating' status announces the wait, outside any aria-busy region", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    // Hold the turn in-flight so we can observe the mid-stream announcement.
    await mockStreamChat(page, { conversationId: 42, answer: ANSWER_MD, delayMs: 1500 });

    await page.goto("/");
    await page.getByPlaceholder("Ask about IPEDS data…").fill("nursing degrees");
    await page.getByRole("button", { name: "Send" }).click();

    // While the turn is in-flight this region mirrors the live progress status
    // ("Thinking…") outside the aria-busy bubble; it falls back to a generic
    // "Generating response…" only when no status text is set.
    const status = page.getByTestId("generating-status");
    await expect(status).toHaveText(/Thinking|Generating response/);
    await expect(status).toHaveAttribute("aria-live", "polite");
    // It must NOT live inside the aria-busy assistant bubble (that's the point).
    await expect(page.locator('.msg.assistant [data-testid="generating-status"]')).toHaveCount(0);
    // Once the answer settles the announcement clears (busy → false).
    await expect(page.getByRole("table")).toBeVisible();
    await expect(status).toHaveText("");
  });
});

test.describe("turn timing is accessible text, not title-only (A11Y-7)", () => {
  test("the question timestamp and the answer duration each carry a descriptive accessible name", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    const convos = await mockConversations(page, []);
    await mockStreamChat(page, {
      conversationId: 42, sql: [SQL], answer: ANSWER_MD, messageId: 7, durationMs: 4200,
    });
    await mockConversation(page, 42, [
      { role: "user", content: "nursing degrees" },
      { role: "assistant", id: 7, content: ANSWER_MD, sql_log: [SQL] },
    ]);

    await page.goto("/");
    await page.getByPlaceholder("Ask about IPEDS data…").fill("nursing degrees");
    convos.setList([{ id: 42, title: "nursing degrees" }]);
    await page.getByRole("button", { name: "Send" }).click();
    await expect(page.getByRole("table")).toBeVisible();

    // The visible text is a bare clock time / "Thought for 4 seconds"; the
    // accessible name adds the context that used to live only in `title`.
    await expect(page.getByLabel(/^Asked at /)).toBeVisible();
    await expect(page.getByLabel(/Answer generated — Thought for/)).toBeVisible();
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
    await gotoAdmin(page);

    await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
  });
});

test.describe("tabs selected state", () => {
  test("active Admin subtab link exposes aria-current", async ({ page }) => {
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockConversations(page, []);
    await mockAllowlist(page, []);
    await mockAccessRequests(page, []);
    await mockImportJobs(page, []);

    await page.goto("/admin/users/current");

    const usersSub = page.getByRole("link", { name: "Users" });
    const importsSub = page.getByRole("link", { name: "Imports" });
    await expect(usersSub).toHaveAttribute("aria-current", "page");
    await expect(importsSub).not.toHaveAttribute("aria-current", "page");

    await importsSub.click();
    await expect(importsSub).toHaveAttribute("aria-current", "page");
    await expect(usersSub).not.toHaveAttribute("aria-current", "page");
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
    await gotoAdmin(page);

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
// than being skipped.
//
// Gated on critical AND *serious*. Filtering to `critical` alone (as this did
// originally) is not a strict threshold -- it is a blind spot with a specific
// shape: axe rates colour-contrast, aria-prohibited-attr,
// scrollable-region-focusable and heading-order as `serious`, so the whole
// class of defect this suite exists to catch scored below the gate. A dark-mode
// contrast failure sat on the avatar attention badge -- visible on every page,
// on every admin -- while these tests stayed green.
const GATED_IMPACTS = ["critical", "serious"];

function gatedViolations(results) {
  return results.violations.filter((v) => GATED_IMPACTS.includes(v.impact));
}

test.describe("axe smoke scan", () => {
  test("Login screen has no critical or serious violations", async ({ page }) => {
    await mockMe(page, null);
    await mockAuthConfig(page, "");
    // Scan the RESTING state. The door's figure gallery auto-advances every 5s
    // and each change replays a .34s fade from opacity:0, so an unlucky sample
    // catches the source line mid-fade and axe measures the BLENDED colour
    // (#737f7a instead of --muted #5c6a65) -- reporting 3.56:1 against text
    // that actually renders at 4.85:1. Reduced motion is a real user setting
    // the gallery already honours (it stops rotation and drops the animation),
    // so this measures the pixels a user sees rather than a transient frame.
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "IPEDS Oracle" })).toBeVisible();

    const found = gatedViolations(await new AxeBuilder({ page }).analyze());
    expect(found, JSON.stringify(found, null, 2)).toEqual([]);
  });

  test("Chat screen has no critical or serious violations", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    await page.goto("/");
    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeVisible();

    const found = gatedViolations(await new AxeBuilder({ page }).analyze());
    expect(found, JSON.stringify(found, null, 2)).toEqual([]);
  });

  // The two scans above run in the LIGHT theme as an anonymous/non-admin user,
  // which is exactly where the app's contrast bugs were NOT. The accent-filled
  // badges hardcoded #fff, and #fff over the dark theme's lighter --accent is
  // 2.43:1 -- so the defect needed BOTH the dark theme and an element that only
  // renders for an admin with work waiting. The avatar attention badge is the
  // one such element visible on every page, Chat included, which makes this the
  // cheapest surface that can see it. main.jsx reads the saved theme at boot,
  // so seeding localStorage before load is what puts the page in dark mode.
  test("Chat in dark theme, admin badge showing, has no critical or serious violations",
    async ({ page }) => {
      await mockMe(page, { email: "admin@example.edu", is_admin: true });
      await mockConversations(page, []);
      await mockAttention(page, { users: 2, skills: 1, logs: 4 });
      await page.addInitScript(() => globalThis.localStorage.setItem("theme", "dark"));
      await page.goto("/");
      await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeVisible();
      // Don't scan until the badge the theme bug lived on is actually painted.
      await expect(page.locator(".avatar-badge")).toBeVisible();

      const found = gatedViolations(await new AxeBuilder({ page }).analyze());
      expect(found, JSON.stringify(found, null, 2)).toEqual([]);
    });

  // axe CANNOT catch the badge, which is why this is a separate, explicit
  // assertion rather than a line in the scan above. Its colour-contrast rule
  // files a one-character element as `incomplete`, not a violation --
  // "Element content is too short to determine if it is actual text content"
  // -- and `incomplete` is not gatable in general (it also holds the composer's
  // deliberate 1:1 transparent-textarea-over-mirror overlay). So the count pill
  // that hardcoded #fff over the dark theme's lighter --accent sat at 2.43:1,
  // on every page, on every admin, with the a11y suite green.
  test("the avatar attention badge meets AA contrast in dark theme", async ({ page }) => {
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockConversations(page, []);
    await mockAttention(page, { users: 2, skills: 1, logs: 4 });
    await page.addInitScript(() => globalThis.localStorage.setItem("theme", "dark"));
    await page.goto("/");
    await expect(page.locator(".avatar-badge")).toBeVisible();

    const ratio = await contrastRatio(page, ".avatar-badge");
    expect(ratio, `.avatar-badge contrast was ${ratio?.toFixed(2)}:1`)
      .toBeGreaterThanOrEqual(4.5);
  });
});
