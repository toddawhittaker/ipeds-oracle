import { test, expect } from "@playwright/test";

import {
  mockMe, mockConversations, mockConversation, mockAttention, mockMarkLogsSeen,
  mockAllowlist, mockAccessRequests, mockDeniedRequests, mockSkills, mockLogs,
  mockImportJobs, mockImportJobPoll, mockImportCatalog,
} from "./mocks.js";

// WCAG 2.1.1 (Level A): a region that scrolls but contains nothing focusable is
// unreachable by keyboard — you can see the first screenful and no more.
//
// Six such regions shipped: the Thinking trace and the SQL inside it, the
// standalone SQL block, a non-SQL code fence, the admin Logs viewer, the import
// job log, and the CSV-format example. `.md .table-wrap` was already done right
// and is the pattern they now copy (tabIndex + role=region + aria-label).
//
// Why this is a spec and not left to the axe gate: axe's own
// `scrollable-region-focusable` rule is rated `serious` and the gate DOES fail
// on serious — it simply never renders any of these states. That is a coverage
// hole, not a threshold one, which is exactly the shape that hides this class of
// bug. The role assertions here are deliberately direct so they hold even before
// the scans widen.

const USER = { email: "user@example.edu", is_admin: false };
const ADMIN = { email: "admin@example.edu", is_admin: true };

const ANSWER = [
  "Here you go.",
  "",
  "```sql",
  "SELECT instnm FROM hd LIMIT 5",
  "```",
  "",
  "```",
  "not sql, just a fence",
  "```",
].join("\n");

async function adminMocks(page) {
  await mockMe(page, ADMIN);
  await mockConversations(page, []);
  await mockAllowlist(page, []);
  await mockAccessRequests(page, []);
  await mockDeniedRequests(page, []);
  await mockSkills(page, []);
  await mockAttention(page, { users: 0, skills: 0, logs: 0 });
  await mockMarkLogsSeen(page);
}

test.describe("scrollable regions are keyboard reachable", () => {
  test("an answer's SQL block and code fence are focusable, named regions",
    async ({ page }) => {
      // The SQL block is the load-bearing case: its attributes have to survive
      // react-syntax-highlighter's PreTag indirection. That pass-through is
      // asserted HERE, in a browser, rather than taken on faith from the
      // library's docs — if a future version stops forwarding them, the region
      // silently stops being reachable and nothing else would notice.
      await mockMe(page, USER);
      await mockConversations(page, [{ id: 5, title: "T", updated_at: 0 }]);
      await mockConversation(page, 5, [
        { id: 1, role: "user", content: "q" },
        { id: 2, role: "assistant", content: ANSWER, sql_log: ["SELECT 1"] },
      ]);
      await page.goto("/chat/5");

      const sqlRegion = page.getByRole("region", { name: "SQL query" }).first();
      await expect(sqlRegion).toBeVisible();
      await expect(sqlRegion).toHaveAttribute("tabindex", "0");

      const code = page.getByRole("region", { name: "Code block" }).first();
      await expect(code).toBeVisible();
      await expect(code).toHaveAttribute("tabindex", "0");

      // Reachable in fact, not just in markup.
      await sqlRegion.focus();
      await expect(sqlRegion).toBeFocused();
    });

  test("the admin log viewer is a focusable, named region", async ({ page }) => {
    await adminMocks(page);
    await mockLogs(page, [
      { ts: 1700000000, level: "INFO", name: "ipeds.app", msg: "started" },
    ]);
    await page.goto("/admin/logs");

    const log = page.getByRole("region", { name: "Server log" });
    await expect(log).toBeVisible();
    await expect(log).toHaveAttribute("tabindex", "0");
    await log.focus();
    await expect(log).toBeFocused();
  });

  test("the CSV-format example is a focusable, named region", async ({ page }) => {
    await adminMocks(page);
    await mockLogs(page, []);
    await page.goto("/admin/users/current");

    // The example lives inside the CSV-import help popover, which is itself
    // inside a collapsed <details> — expand that first or the trigger isn't in
    // the tree at all.
    await page.getByText("Import from CSV").click();
    // focus(), NOT click(). HelpPopover opens on hover AND focus while its
    // onClick TOGGLES, so a bare click() races React: click() dispatches
    // mouseenter -> focus -> click, and whether `openedByFocus` is armed depends
    // on whether the mouseenter's setOpen(true) has COMMITTED by the time
    // onFocus reads `open`. Batched into one task it passes; under load the
    // commit lands first, the click toggles the popover shut, and this test
    // fails. Focus alone calls openNow() unconditionally — no toggle, no race —
    // and it is also the keyboard route, which is what this spec is about.
    // Every other popover spec already does this (csv-import.spec.js,
    // admin-usage-info.spec.js's focusPopover); this was the lone outlier.
    await page.getByRole("button", { name: "CSV format help" }).focus();
    const example = page.getByRole("region", { name: "CSV format example" });
    await expect(example).toBeVisible();
    await expect(example).toHaveAttribute("tabindex", "0");
  });

  test("the CSV-format example can actually be REACHED and KEPT by keyboard", async ({ page }) => {
    // THE REGRESSION: existing coverage asserted the region was visible and
    // carried tabindex=0 — but never focused it, so it passed while the region
    // was unreachable. HelpPopover tracked focus on the TRIGGER only, so Tabbing
    // into the popover blurred the trigger, fired closeSoon(), and unmounted the
    // region out from under the focused element 140ms later. Measured before the
    // fix: focus was the region immediately after Tab, and <body> 400ms later,
    // with the popover gone. That makes the tabindex=0 added for WCAG 2.1.1
    // decorative, and strands the user at the top of the document.
    await adminMocks(page);
    await mockLogs(page, []);
    await page.goto("/admin/users/current");
    await page.getByText("Import from CSV").click();
    await page.getByRole("button", { name: "CSV format help" }).focus();

    await page.keyboard.press("Tab");
    const example = page.getByRole("region", { name: "CSV format example" });
    await expect(example).toBeFocused();

    // Past closeSoon()'s 140ms timer: the popover must NOT have closed under it.
    // Asserted synchronously after the wait, because an auto-retrying matcher
    // would happily wait out a transient and report the wrong thing either way.
    await page.waitForTimeout(400);
    expect(await example.evaluate((el) => el === globalThis.document.activeElement)).toBe(true);
    await expect(example).toBeVisible();
  });

  test("the import job log is a focusable, named region", async ({ page }) => {
    await adminMocks(page);
    await mockLogs(page, []);
    // The job panel is only populated by watch(), which runs when "view" is
    // clicked on a row — the jobs LIST carries no log text of its own.
    await mockImportJobs(page, [
      { id: 3, filename: "integrate:2024", status: "swapped", updated_at: 1700000000 },
    ]);
    await mockImportJobPoll(page, 3, [{
      id: 3, filename: "integrate:2024", status: "swapped",
      log: "step one\nstep two\n", report: null,
    }]);
    await page.goto("/admin/imports");

    await page.getByRole("button", { name: /view/i }).first().click();
    const jobLog = page.getByRole("region", { name: "Import job log" });
    await expect(jobLog).toBeVisible();
    await expect(jobLog).toHaveAttribute("tabindex", "0");
  });
});

test.describe("a locked year card keeps its semantics", () => {
  // THE REGRESSION: `interactive = selectable && !locked` gated role,
  // aria-checked, aria-label AND tabIndex together, so a locked card was a
  // roleless <div> carrying aria-disabled — an attribute ARIA does not permit
  // on a generic element, so it is ignored outright. The whole year grid
  // degraded to unannounced static text with NO disabled semantics.
  //
  // Newly reachable because Imports adopts a running job on mount: a second
  // admin, or the same one after a reload, lands in exactly this state on the
  // app's highest-stakes screen. axe cannot see it — the a11y fixture's only
  // year is selectable, so the branch never renders under the gate.
  test("stays a checkbox, focusable, and announces as disabled while locked", async ({ page }) => {
    await mockMe(page, ADMIN);
    await mockConversations(page, []);
    await mockAttention(page, { users: 0, skills: 0, logs: 0 });
    await mockMarkLogsSeen(page);
    await mockImportCatalog(page, {
      probed_at: 0, partial: false,
      years: [{ start_year: 2024, year: 2025, year_label: "2024-25", status: "final",
                integrated: false, available: true, release: "Final", selectable: true }],
      disk: null, calibration: null,
    });
    // A job already running -> the tab adopts it and locks every control.
    await mockImportJobs(page, [
      { id: 88, filename: "integrate:2024", status: "running", updated_at: 1 },
    ]);
    await mockImportJobPoll(page, 88, [
      { id: 88, filename: "integrate:2024", status: "running", log: "…", report: null },
    ]);
    await page.goto("/admin/imports");

    const card = page.getByRole("checkbox", { name: "Integrate 2024-25 (Final)" });
    await expect(card).toBeVisible();                       // still IN the a11y tree
    await expect(card).toHaveAttribute("aria-disabled", "true");
    await expect(card).toHaveAttribute("tabindex", "0");    // discoverable, not skipped
    // aria-disabled is advisory to the DOM, so the HANDLER has to be what
    // actually refuses. force:true because Playwright itself honours
    // aria-disabled and would otherwise wait for the element to become
    // "enabled" — which is a good sign in its own right, and also means an
    // ordinary .click() cannot express this assertion.
    await card.click({ force: true });
    await expect(card).toHaveAttribute("aria-checked", "false");
  });

  test("a NON-selectable card is named by its state, not by an action it cannot do",
    async ({ page }) => {
      // THE REGRESSION: making role="checkbox" unconditional was right, but the
      // label was not. role=checkbox is in ARIA's presentational-children list,
      // so the tile's own .year-label and StatusBadge are PRUNED and aria-label
      // becomes the ENTIRE accessible name. An already-loaded year therefore
      // announced "Integrate 2022-23 (Final)" — its "Integrated" badge gone, and
      // an affordance stated that does not exist — and `release` is null for an
      // unprobed/unknown year, which rendered the literal string "(null)".
      // axe cannot rate a wrong-but-present name, and the fixture above only
      // covers a SELECTABLE card, which is how this got through.
      await mockMe(page, ADMIN);
      await mockConversations(page, []);
      await mockAttention(page, { users: 0, skills: 0, logs: 0 });
      await mockMarkLogsSeen(page);
      await mockImportJobs(page, []);
      await mockImportCatalog(page, {
        probed_at: 0, partial: false,
        years: [
          { start_year: 2022, year: 2023, year_label: "2022-23", status: "integrated",
            integrated: true, available: true, release: "Final", selectable: false },
          { start_year: 2019, year: 2020, year_label: "2019-20", status: "unknown",
            integrated: false, available: false, release: null, selectable: false },
        ],
        disk: null, calibration: null,
      });
      await page.goto("/admin/imports");

      // Named by STATE, so the badge's meaning survives being pruned.
      await expect(page.getByRole("checkbox", { name: "2022-23 — Integrated" })).toBeVisible();
      // A null release must never reach the name as the string "null".
      const unknown = page.getByRole("checkbox", { name: /^2019-20 —/ });
      await expect(unknown).toBeVisible();
      await expect(page.getByRole("checkbox", { name: /null/ })).toHaveCount(0);
      await expect(page.getByRole("checkbox", { name: /^Integrate 2022-23/ })).toHaveCount(0);
    });
});
