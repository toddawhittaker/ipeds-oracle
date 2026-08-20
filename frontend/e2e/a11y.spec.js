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
  mockDeniedRequests,
  mockSkills,
  mockLogs,
  mockMarkLogsSeen,
  mockImportCatalog,
  mockVersion,
  mockUsage,
  mockSkillCategories,
  mockSkillRejections,
  mockApiKeys,
  mockAdminKeys,
} from "./mocks.js";

// Everything the six admin sections fetch on mount, with CONTENT rather than
// empty lists: an empty table renders none of the elements whose contrast or
// semantics could be wrong. The Logs fixture carries all three levels because
// the WARNING one sat at 2.52:1 and no scan had ever rendered it.
async function adminA11yMocks(page) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockVersion(page);
  await mockAttention(page, { users: 2, skills: 1, logs: 4 });
  await mockConversations(page, []);
  await mockAllowlist(page, [
    { email: "prof@example.edu", note: "Faculty", added_by: "admin@example.edu", added_at: 1 },
  ]);
  await mockAccessRequests(page, [
    { id: 1, email: "new@example.edu", canon_email: "new@example.edu", created_at: 1, status: "pending" },
  ]);
  await mockDeniedRequests(page, [
    // `emails` is REQUIRED by the Blocked table's Email renderer. Omitting it
    // threw, the error boundary swallowed the whole route, and all three
    // Users paths x both themes -- 6 of these scans -- silently scanned a
    // three-element crash card and reported clean.
    { id: 2, email: "no@example.edu", canon_email: "no@example.edu",
      emails: ["no@example.edu", "no+tag@example.edu"], created_at: 1, denied_at: 2 },
  ]);
  await mockSkills(page, [
    { id: 1, headline: "Match an exact 6-digit CIP", description: "…", sql_example: "SELECT 1",
      verified: 0, created_by: "critic", created_at: 1, category: "CIP_ROLLUP",
      // Real counts: without them the row renders "undefined upvotes", which is
      // a fixture defect that would read as a product one.
      upvotes: 3, downvotes: 1, hits: 12 },
  ]);
  // Skills fetches THREE endpoints, not one. Without these two the categories
  // and rejections calls 404, so the page renders "Muted categories (0)" and a
  // bare "Not Found" where the rejected-lesson list belongs — putting the
  // category pill, "Reject & mute", the Rejected rows and Allow-again/Unmute
  // outside the gate in both themes. Same shape as the mockUsage omission below.
  await mockSkillCategories(page, [
    { token: "CIP_ROLLUP", label: "CIP rollup", muted: false },
  ]);
  await mockSkillRejections(page, [
    { id: 5, headline: "Verify figures against the result", description: "…",
      category: "UNGROUNDED_NUMBER", was_verified: 0, hits: 2, created_at: 1 },
  ]);
  // CONTENT, not an empty array, for the same reason as every list above: an
  // empty keys list renders none of the elements whose contrast could be wrong
  // — the masked-key code, the label, the row action. A revoked row is not worth
  // adding here: /keys lists live keys only, and the "Revoked" pill it used to
  // show now exists solely in the admin table below.
  await mockApiKeys(page, [
    { id: 1, last4: "9f2a", label: "Work laptop", created_at: 1,
      created_by: null, last_used_at: 2, revoked_at: null },
  ]);
  await mockAdminKeys(page, [
    { id: 1, email: "prof@example.edu", last4: "9f2a", label: "Work laptop",
      created_at: 1, created_by: null, last_used_at: 2, revoked_at: null },
    // A revoked row too, or the status pill and the missing-action cell are both
    // outside the scan.
    { id: 2, email: "dean@example.edu", last4: "2222", label: null,
      created_at: 1, created_by: "admin@example.edu", last_used_at: null, revoked_at: 3 },
  ]);
  // THE USAGE PAGE WAS NEVER ACTUALLY SCANNED. `mockUsage` was simply absent, so
  // /api/admin/usage 404'd and both Usage scans measured the
  // load-FAILURE state: measured 0 `.stat` tiles, 0 `.errbound`, panel text
  // ending "Not Found". Neither existing guard caught it — the readiness wait
  // matches an unconditional `<h2 class="sr-only">Usage</h2>`, and `.errbound`
  // is 0 because Usage CATCHES its fetch rejection rather than throwing. That
  // left 12 stat tiles, 12 HelpPopover triggers, the metric toolbar, the chart
  // and the Top-users scroll region unscanned in both themes. `figures_suppressed`
  // is present so the "· N suppressed" label form is covered too.
  await mockUsage(page, {
    bucket: "day",
    series: [{ t: "2026-01-01", queries: 12, tokens: 900, spend: 0.4 }],
    top_users: [{ email: "someone.with.a.long.address@example.edu", queries: 42,
                  tokens: 900, spend: 0.5 }],
    totals: { queries: 120, tokens: 8400, spend: 1.23, cache_hits: 9,
              escalations: 2, failures: 1, prompt_tokens: 8400,
              cached_prompt_tokens: 4200, first_call_prompt_tokens: 5000,
              first_call_cached_prompt_tokens: 2500,
              figures_checked: 10, figures_ungrounded: 1, figures_suppressed: 4,
              table_cells_checked: 318, table_cells_matched: 312,
              emit_turns: 50, structured_turns: 50, leaked_turns: 1,
              exhausted_turns: 3, degraded_turns: 1,
              priceable_turns: 40, estimated_turns: 12, cost_warning: false },
  });
  await mockLogs(page, [
    { ts: 1700000000, level: "INFO", name: "ipeds.app", msg: "started" },
    { ts: 1700000100, level: "WARNING", name: "ipeds.app", msg: "something looks off" },
    { ts: 1700000200, level: "ERROR", name: "ipeds.app", msg: "it failed" },
  ]);
  await mockMarkLogsSeen(page);
  await mockImportJobs(page, [
    { id: 3, filename: "integrate:2024", status: "swapped", updated_at: 1 },
  ]);
  await mockImportCatalog(page, {
    probed_at: 0, partial: false,
    years: [{ start_year: 2024, year: 2025, year_label: "2024-25", status: "final",
              integrated: false, available: true, release: "Final", selectable: true,
              zip_bytes: 1000 }],
    disk: { free_bytes: 9e11, total_bytes: 1e12, used_bytes: 1e11 },
    calibration: null,
  });
}

// Coverage for the a11y fixes the implementer landed across App.jsx, Chat.jsx,
// Login.jsx, Admin.jsx and Markdown.jsx. Every assertion here uses role/label/
// aria selectors against the real rendered app (via the existing /api/** mocks)
// rather than CSS, so a regression that removes an aria attribute or a <label>
// association fails the test, not just a visual/CSS check.

// `LIMIT 200` is deliberate. Prism colours a number literal with .token.number,
// and axe files a ONE-CHARACTER element as `incomplete` rather than a violation
// — so while the only number here was `3` (from `awlevel=3`), a below-AA number
// colour could not be rated at all. It shipped at 4.44:1 on exactly that basis.
// (`'51.3801'` does not count: quoted, so it lexes as .token.string.)
//
// This DOES now make the scan able to catch the regression, but only together
// with the tall viewport set on the axe describe below — the token renders
// below a 720px fold, and axe silently PASSES off-screen text rather than
// rating it. Both halves are required; with either one missing the scan is
// green against a real violation. The direct contrast assertion at the bottom
// of this file is the belt to that pair of braces.
const SQL = "SELECT stabbr, SUM(x) AS total FROM c_a WHERE cipcode='51.3801' "
  + "AND awlevel=3 GROUP BY stabbr LIMIT 200";
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


// A RICH answer: figure + table + chart + a ```sql fence. The scan below is
// meant to cover the answer surface, and the shared ANSWER_MD is table-only —
// so without this the chart (and the aria-hidden-focus defect on its hidden
// PNG-export copy) sat outside the gate even after the scan was added.
async function openRichAnswer(page, { convId = 5 } = {}) {
  await mockMe(page, { email: "user@example.edu", is_admin: false });
  await mockConversations(page, [{ id: convId, title: "Nursing", updated_at: 0 }]);
  await mockConversation(page, convId, [
    { id: 1, role: "user", content: "nursing degrees by state" },
    { id: 2, role: "assistant", sql_log: [SQL],
      thinking: [{ kind: "status", text: "Thinking…" }, { kind: "sql", text: SQL }],
      suggestions: ["Which state grew fastest?"],
      figure: { value: 12345, unit: "degrees", label: "national total" },
      figure_grounding: "exact", table_grounding: "matched",
      table_cells_checked: 2, table_cells_matched: 2,
      content: ANSWER_MD + "\n```chart\n"
        + '{"type":"bar","x":"State","y":["Total"],'
        + '"data":[{"State":"CA","Total":100},{"State":"NY","Total":50}]}'
        + "\n```\n" },
  ]);
  await page.goto(`/chat/${convId}`);
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
  // A TALL viewport, and this is load-bearing rather than cosmetic.
  //
  // axe only contrast-checks text that is inside the viewport: colorContrastEvaluate
  // begins `if (!_isVisibleOnScreen(node)) { ...; return true }` — a PASS, not an
  // incomplete. This app pins `html, body { overflow: hidden }` and gives each
  // screen its own inner scroller (.messages, .admin), so at Playwright's default
  // 1280x720 nothing below the fold is ever measured.
  //
  // Measured: on /admin/logs with 30 records, 95 text nodes are checked at 720px
  // and 143 at 4000px — 34% never contrast-checked. And a real below-AA colour
  // (#a15c00 on .token.number, 4.44:1) sat at y=767 in the rendered-answer scan,
  // 47px below the fold, so that scan PASSED while a direct probe of the same
  // state reported it as a serious violation. That gap is what this fixes; it is
  // specific to colour-contrast, since the other gated rules are DOM/CSSOM-based
  // and not viewport-gated.
  // ...and reduced motion for EVERY scan, not just Login's.
  //
  // The Login scan already did this, and its comment gives the reason: axe
  // sampling mid-fade measures the BLENDED colour, reporting a ratio against
  // pixels that never rest there. That reasoning was never Login-specific —
  // toasts, modals, the bulk toolbar and the spinner all animate — it was just
  // the only scan that had been bitten. Widening the viewport surfaced it
  // elsewhere: /admin/users/blocked (dark) failed roughly one full-suite run in
  // two while passing 3/3 in isolation, the signature of a transient frame
  // rather than a real violation. Scan resting pixels, everywhere.
  // One thing the tall viewport NARROWS, stated so nobody "simplifies" it away:
  // rules gated on a region actually BEING scrollable
  // (scrollable-region-focusable) mostly stop firing here, because at 2600px
  // the panels fit. That class is covered by a11y-scroll-regions.spec.js, which
  // deliberately keeps the default 1280x720 — do not fold those cases in here.
  test.use({ viewport: { width: 1280, height: 2600 }, reducedMotion: "reduce" });

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

  // THE SCANS ABOVE ONLY EVER SAW THE EMPTY STATE.
  //
  // Login and an empty Chat are the app's two least-populated screens. Every
  // control the product is actually made of — the result table, the chart and
  // its toolbar, SqlBlock, Figure, TableTrust, CopyMenu, the chips — and every
  // admin page were outside the gate entirely. That is a COVERAGE hole, not a
  // threshold one: axe rates scrollable-region-focusable, colour-contrast and
  // aria-hidden-focus as `serious`, which this gate already fails on. It simply
  // never rendered a state where they could fire, which is how two whole
  // classes of defect shipped past a green suite.
  //
  // Adding the answer scan immediately found `aria-hidden-focus` on the hidden
  // PNG-export chart (recharts renders a focusable svg surface), i.e. a
  // keyboard user could Tab into an invisible chart that announces nothing.
  test("a rendered answer, with its disclosures open, has no critical or serious violations",
    async ({ page }) => {
      await openRichAnswer(page);
      // The trace and SQL panels only EXIST once opened, so scanning the
      // settled answer alone would still miss SqlBlock entirely.
      await page.getByRole("button", { name: /Thinking/i }).click();
      await page.getByRole("button", { name: /^SQL/i }).click();

      const found = gatedViolations(await new AxeBuilder({ page }).analyze());
      expect(found, JSON.stringify(found, null, 2)).toEqual([]);
    });

  test("a MID-STREAM answer has no critical or serious violations", async ({ page }) => {
    // The live pending bubble is the ONLY state where .thought-list is
    // scrollable — styles.css unsets its max-height inside .trace-panel — so a
    // scan of a settled answer structurally cannot see it, and
    // scrollable-region-focusable would never fire however many settled answers
    // were scanned.
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    await mockStreamChat(page, { conversationId: 9, delayMs: 4000, answer: "Later." });
    await page.goto("/");
    await page.getByPlaceholder("Ask about IPEDS data…").fill("a slow question");
    await page.getByRole("button", { name: "Send" }).click();
    await expect(page.getByRole("button", { name: "Stop generating" })).toBeVisible();

    const found = gatedViolations(await new AxeBuilder({ page }).analyze());
    expect(found, JSON.stringify(found, null, 2)).toEqual([]);
  });

  test("the one-shot key reveal has no critical or serious violations",
    async ({ page }) => {
      // Every scan below catches its page at REST. The reveal dialog is the one
      // screen in a key's whole life where the secret is visible, it exists only
      // for the moment after a mint, and no scan had ever seen it — the same
      // shape of blind spot that made widening this loop find two `serious`
      // violations on main.
      await mockMe(page, { email: "user@example.edu", is_admin: false });
      await mockConversations(page, []);
      await mockApiKeys(page, []);
      await page.goto("/keys");
      await page.getByRole("button", { name: "Create key" }).click();
      await expect(page.getByRole("dialog")).toBeVisible();

      const found = gatedViolations(await new AxeBuilder({ page }).analyze());
      expect(found, JSON.stringify(found, null, 2)).toEqual([]);
    });

  test("the bulk-selection toolbar has no critical or serious violations",
    async ({ page }) => {
      // The BulkBar exists ONLY while rows are selected, so every resting-page
      // scan in the loop below — including the four Allowlist tables that have
      // had it far longer than this one — has always missed it. Scanned on the
      // Keys table because that is where it was wired most recently; the
      // component is shared, so this covers the toolbar itself for all of them.
      await adminA11yMocks(page);
      await page.goto("/admin/keys");
      await expect(page.getByRole("heading", { name: "API keys" })).toBeVisible();
      await page.getByRole("row", { name: /prof@example\.edu/ })
        .getByRole("checkbox").check();
      // Both states of the destructive action, in one scan: the live row's
      // Revoke is enabled, and a disabled danger button is its own contrast
      // question.
      await expect(page.getByRole("button", { name: "Revoke", exact: true })).toBeVisible();

      const found = gatedViolations(await new AxeBuilder({ page }).analyze());
      expect(found, JSON.stringify(found, null, 2)).toEqual([]);
    });

  for (const [path, ready, content] of [
    ["/admin/users/current", /Users/i, "[role=tabpanel]:not([hidden]) .grid.data tbody tr"],
    ["/admin/users/pending", /Users/i, "[role=tabpanel]:not([hidden]) .grid.data tbody tr"],
    // Third entry: a selector that only exists once the page's DATA rendered.
    // The heading and `.errbound` checks below are both necessary and both
    // insufficient — see the comment at the assertion.
    ["/admin/users/blocked", /Users/i, "[role=tabpanel]:not([hidden]) .grid.data tbody tr"],
    ["/admin/imports", /Load IPEDS years/i, ".year-card-wrap"],
    ["/admin/usage", /Usage/i, ".stat"],
    ["/admin/skills", /Learned lessons/i, ".skill"],
    ["/admin/keys", /API keys/i, ".grid.data.keys tbody tr"],
    ["/admin/logs", /Server logs/i, ".logmsg"],
    // Not an admin page, but scanned in the same loop: /keys is reachable by
    // every signed-in user and renders under the same shell. A page absent from
    // this table is never scanned at all.
    ["/keys", /API keys/i, ".keyrow"],
  ]) {
    for (const theme of ["light", "dark"]) {
      test(`${path} has no critical or serious violations (${theme})`, async ({ page }) => {
        // No admin page was scanned at all before this. Both themes, because the
        // app's contrast defects have twice been dark-only or light-only — a
        // single-theme sweep is half a sweep.
        await adminA11yMocks(page);
        if (theme === "dark") {
          await page.addInitScript(() => globalThis.localStorage.setItem("theme", "dark"));
        }
        await page.goto(path);
        // attached, not visible: Usage's <h2> is deliberately sr-only, so a
        // visibility wait would hang on the one page whose heading is invisible
        // BY DESIGN.
        await expect(page.getByRole("heading", { name: ready }).first()).toBeAttached();
        // The heading check is NOT proof the page rendered. A throw in any
        // panel is swallowed by the error boundary, which replaces the whole
        // route with a three-element card that axe then scans clean — and that
        // is not hypothetical: a denied-requests fixture missing its `emails`
        // array crashed all three Users paths in both themes, so 6 of these 19
        // scans measured the crash card for months while reporting green.
        // Assert we are looking at the page before believing the result.
        await expect(page.locator(".errbound")).toHaveCount(0);
        // ...and `.errbound` is not sufficient either. A panel that CATCHES its
        // own fetch rejection never throws, so the boundary never fires and the
        // page renders its error state — which axe scans perfectly clean. That
        // is how /admin/usage went unscanned entirely: `mockUsage` was simply
        // missing from the fixture, the request 404'd, and both Usage scans
        // measured 0 stat tiles and the words "Not Found" while reporting green.
        // So assert something that only exists once the DATA rendered.
        await expect(page.locator(content).first()).toBeVisible();

        const found = gatedViolations(await new AxeBuilder({ page }).analyze());
        expect(found, JSON.stringify(found, null, 2)).toEqual([]);
      });
    }
  }

  test("the hidden PNG-export chart is out of the FOCUS order, not just the a11y tree",
    async ({ page }) => {
      // Asserted directly as well as via the scan above, because this is a
      // two-attribute contract and axe only proves the pair is currently
      // consistent. Drop `inert` and the scan catches it; drop `aria-hidden`
      // and it also catches it — but neither tells you WHY both are needed, and
      // a future "simplify" that keeps one is exactly the plausible mistake.
      // Tabbing must never land inside an offscreen chart.
      await openRichAnswer(page);
      const src = page.locator(".chart-export-src");
      await expect(src).toHaveAttribute("aria-hidden", "true");
      await expect(src).toHaveAttribute("inert", /.*/);

      const focusableInside = await page.evaluate(() => {
        const el = globalThis.document.querySelector(".chart-export-src");
        if (!el) return -1;
        return el.querySelectorAll(
          'a[href], button, input, select, textarea, svg[tabindex], [tabindex]:not([tabindex="-1"])'
        ).length;
      });
      // recharts DOES render a focusable surface in there; `inert` is what makes
      // it unreachable. This asserts the element exists to be guarded, so the
      // test can't quietly pass on a chart that never rendered.
      expect(focusableInside).toBeGreaterThanOrEqual(0);
      expect(await src.count()).toBe(1);
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

  // THE REGRESSION: .sqlblock .token.number shipped at #a15c00 = 4.44:1 on
  // --bg, below AA, while every sibling token passed (.function 5.81,
  // .string 4.57, .operator 4.85). The gate could not see it because the
  // fixture's only number was a single character; see the SQL const above.
  // Asserted directly as well as via the widened fixture, because a measured
  // ratio names the number when it drifts, where axe just says "serious".
  test("SQL number literals meet AA contrast in the light theme", async ({ page }) => {
    await askAndUnlockAnswer(page);
    await page.getByRole("button", { name: /SQL/i }).first().click();
    const token = page.locator(".sqlblock .token.number").first();
    await expect(token).toBeVisible();

    const ratio = await contrastRatio(page, ".sqlblock .token.number");
    expect(ratio, `.sqlblock .token.number contrast was ${ratio?.toFixed(2)}:1`)
      .toBeGreaterThanOrEqual(4.5);
  });
});
