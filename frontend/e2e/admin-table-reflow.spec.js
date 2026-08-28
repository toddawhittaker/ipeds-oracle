import { test, expect } from "@playwright/test";
import {
  mockMe, mockConversations, mockAllowlist, mockAccessRequests, mockDeniedRequests,
  mockAttention, mockMarkLogsSeen, mockUsage, mockAdminKeys,
  mockImportJobs, mockImportCatalog,
} from "./mocks.js";

// WCAG 1.4.10 Reflow, for the admin tables.
//
// `html, body { overflow: hidden }` means the page itself cannot scroll
// sideways; every screen owns an inner scroller, and for admin that is the
// whole `.admin` column. So a table wider than the viewport used to be reachable
// only by scrolling the entire page in two directions at once — heading, nav and
// all — which is exactly what 1.4.10 exists to prevent. The fix gives the table
// a scroll region of its own.
//
// Two viewports, and BOTH are load-bearing. At 320 the region has to scroll; at
// 1280 it must NOT, or an `overflow-x: auto` wrapper just puts a permanent
// horizontal scrollbar under every admin table. That second half is only
// measurable because #314 stopped the Actions tooltip hanging 18px outside the
// table — before that the wrapper reported 18px of scroll on a table that fits.
//
// The pure sub-tab/table logic lives in vitest; this file owns geometry, which
// only a browser has.

const ADMIN = { email: "admin@example.edu", is_admin: true };
const USERS = [
  { email: "alice@example.edu", note: "staff", is_admin: false,
    last_login: 1_700_000_000, last_active: 1_700_000_000 },
  { email: "bob@example.edu", note: "faculty", is_admin: false,
    last_login: 1_700_000_000, last_active: 1_700_000_000 },
];
const PENDING = [{ id: 1, email: "p1@example.edu", status: "pending", created_at: 1_700_000_000 }];
const DENIED = [{ id: 9, canon_email: "blocked@example.edu", emails: ["blocked@example.edu"],
  created_at: 1_700_000_000, denied_at: 1_700_000_500 }];

// A filename the product actually produces. The short fixtures every other
// Imports spec uses are exactly why this table's overflow went unmeasured.
const JOBS = [{ id: 1, filename: "IPEDS_2023-24_Provisional_All_Data.zip",
  status: "swapped", updated_at: 1_700_000_000 }];

async function openImports(page, jobs) {
  await mockMe(page, ADMIN);
  await mockConversations(page, []);
  await mockAttention(page, { users: 0, skills: 0, logs: 0 });
  await mockMarkLogsSeen(page);
  await mockImportJobs(page, jobs);
  await mockImportCatalog(page, { probed_at: 1_700_000_000, partial: false, years: [] });
  await page.goto("/admin/imports");
  await expect(page.getByRole("table", { name: "Recent jobs" })).toBeVisible();
}

async function openUsers(page, path = "/admin/users/current") {
  await mockMe(page, ADMIN);
  await mockConversations(page, []);
  await mockAllowlist(page, USERS);
  await mockAccessRequests(page, PENDING);
  await mockDeniedRequests(page, DENIED);
  await page.goto(path);
  await expect(page.getByRole("heading", { name: "Users" })).toBeVisible();
}

// The admin column on its own, for a case that has to fail on GEOMETRY when the
// wrapper is gone rather than on the wrapper's own locator not resolving.
const adminGeometry = (page) => page.locator(".admin").evaluate((el) => (
  { client: el.clientWidth, scroll: el.scrollWidth }));

// clientWidth/scrollWidth of a scroll region plus the page scroller behind it.
// Read together in one evaluate so they describe the same layout pass.
const geometry = (locator) => locator.evaluate((el) => {
  const admin = globalThis.document.querySelector(".admin");
  return {
    client: el.clientWidth, scroll: el.scrollWidth,
    adminClient: admin.clientWidth, adminScroll: admin.scrollWidth,
  };
});

test.describe("admin tables reflow into their own scroll region", () => {
  test("at 320px the users table scrolls itself, and the page does not", async ({ page }) => {
    await openUsers(page);
    await page.setViewportSize({ width: 320, height: 900 });

    const wrap = page.locator("#userpanel-current .table-scroll");
    const geo = await geometry(wrap);
    // The table is wider than 320 and has somewhere of its own to go...
    expect(geo.scroll).toBeGreaterThan(geo.client);
    // ...and the admin column, which is what used to absorb it, does not.
    expect(geo.adminScroll).toBe(geo.adminClient);
  });

  test("scrolling to the Actions button leaves the heading where it was", async ({ page }) => {
    // The defect in user terms. Content was always REACHABLE; reaching it took
    // the heading and the section nav off-screen with it.
    await openUsers(page);
    await page.setViewportSize({ width: 320, height: 900 });

    const heading = page.getByRole("heading", { name: "Users" });
    const before = await heading.boundingBox();

    const wrap = page.locator("#userpanel-current .table-scroll");
    await wrap.evaluate((el) => { el.scrollLeft = el.scrollWidth; });

    await expect(page.getByRole("button", { name: /Promote admin/ }).first()).toBeInViewport();
    expect((await heading.boundingBox()).x).toBe(before.x);
  });

  test("at 1280px no admin table gets a scrollbar it does not need", async ({ page }) => {
    // The over-broad-fix guard, and the reason #314 had to land first: an
    // unconditional overflow-x:auto wrapper is only acceptable if the tables
    // genuinely fit at desktop. All three sub-tabs, because each renders a
    // different set of columns.
    for (const sub of ["current", "pending", "blocked"]) {
      await openUsers(page, `/admin/users/${sub}`);
      const geo = await geometry(page.locator(`#userpanel-${sub} .table-scroll`));
      expect(geo.scroll, `${sub} must not scroll at 1280`).toBe(geo.client);
    }
  });

  test("Top users scrolls itself too, rather than widening the page", async ({ page }) => {
    // Not a DataTable and it sets no column widths — but an email address is a
    // single unbreakable token, so one ordinary long address made the whole
    // admin column scroll sideways.
    await mockMe(page, ADMIN);
    await mockConversations(page, []);
    await mockAttention(page, { users: 0, skills: 0, logs: 0 });
    await mockMarkLogsSeen(page);
    await mockUsage(page, {
      bucket: "day", series: [],
      top_users: [{ email: "someone.with.a.long.address@example.edu", queries: 42,
        tokens: 900, spend: 0.5 }],
      totals: { queries: 120, tokens: 8400, spend: 1.23, cache_hits: 9,
        escalations: 2, failures: 1, prompt_tokens: 8400, cached_prompt_tokens: 4200 },
    });
    await page.goto("/admin/usage");
    await expect(page.getByRole("table", { name: "Top users" })).toBeVisible();
    await page.setViewportSize({ width: 320, height: 900 });

    const geo = await geometry(page.locator(".table-scroll").filter({
      has: page.getByRole("table", { name: "Top users" }),
    }));
    expect(geo.scroll).toBeGreaterThan(geo.client);
    expect(geo.adminScroll).toBe(geo.adminClient);
  });

  test("a scroll region with nothing focusable inside is focusable itself",
    async ({ page }) => {
      // WCAG 2.1.1. Top users is not a DataTable: every cell is plain text, so
      // there is nothing inside to tab to and nothing to scroll the region into
      // view. The wrapper was copied from DataTable.jsx, whose exemption
      // depends on rows having focusable controls — a precondition that
      // silently did not transfer, which is why DataTable now DERIVES it.
      await mockMe(page, ADMIN);
      await mockConversations(page, []);
      await mockAttention(page, { users: 0, skills: 0, logs: 0 });
      await mockMarkLogsSeen(page);
      await mockUsage(page, {
        bucket: "day", series: [],
        top_users: [{ email: "someone.with.a.long.address@example.edu",
          queries: 42, tokens: 900, spend: 0.5 }],
        totals: { queries: 120, tokens: 8400, spend: 1.23, cache_hits: 9,
          escalations: 2, failures: 1, prompt_tokens: 8400,
          cached_prompt_tokens: 4200 },
      });
      await page.goto("/admin/usage");

      const region = page.getByRole("region", { name: /Top users/ });
      await expect(region).toBeVisible();
      await region.focus();
      await expect(region).toBeFocused();
      // ...and its name is NOT the table's own, which would announce twice.
      expect(await region.getAttribute("aria-label"))
        .not.toBe(await page.getByRole("table", { name: "Top users" })
          .getAttribute("aria-label"));
    });

  test("the Keys table DOES get a region, because one of its columns is unsortable",
    async ({ page }) => {
      // The other side of the derivation, and the first consumer ever to ship
      // it. `needsScrollRegion` (datatable-region.js) returns true when any
      // column is unsortable, because that column's header has no button — so a
      // table with an unsortable column is NOT fully keyboard-reachable through
      // its own contents, and the wrapper has to be a tab stop. Admin -> Keys
      // declares `last4` unsortable (there is no sensible order for four
      // characters of a hash), so it takes the true branch that the three
      // Allowlist tables never do. Until this test the branch had no browser
      // coverage at all, and `docs/ARCHITECTURE.md` still described it as a
      // decision NOT to render a region.
      await mockMe(page, ADMIN);
      await mockConversations(page, []);
      await mockAttention(page, { users: 0, skills: 0, logs: 0 });
      await mockMarkLogsSeen(page);
      await mockAdminKeys(page, [{
        id: 1, email: "someone@example.edu", label: "laptop", last4: "9f2a",
        created_at: 1_700_000_000, last_used_at: null, revoked_at: null,
      }]);
      await page.goto("/admin/keys");

      const region = page.getByRole("region", { name: /API keys/ });
      await expect(region).toBeVisible();
      await region.focus();
      await expect(region).toBeFocused();
      // The region's name must differ from the table's, or a screen reader
      // announces "API keys" twice on the way in.
      expect(await region.getAttribute("aria-label"))
        .not.toBe(await page.getByRole("table", { name: "API keys" })
          .getAttribute("aria-label"));
      // And at 1280 it must not actually scroll — a region that always shows a
      // scrollbar is the failure mode this file's other half exists to catch.
      const geo = await region.evaluate((el) => ({ scroll: el.scrollWidth, client: el.clientWidth }));
      expect(geo.scroll).toBe(geo.client);
    });

  test("Recent jobs scrolls itself rather than dragging the admin column sideways",
    async ({ page }) => {
      // The regression: Admin -> Imports' "Recent jobs" was the one admin table
      // #315 measured as FITTING and so left without a wrapper. A real upload
      // does not fit — `IPEDS_2023-24_Provisional_All_Data.zip` is a single
      // unbreakable token and the localised timestamp beside it will not wrap
      // either — so the table came to 440px at a 320px viewport and the
      // `.admin` column scrolled sideways instead, heading and section nav
      // with it.
      //
      // Both assertions are load-bearing, in opposite directions: `.admin` must
      // not scroll, which IS the defect (320 against 503 with the wrapper
      // removed, on this fixture, mutation-verified); and
      // the region must genuinely overflow, or this passes against a table with
      // no rows. `.admin` is measured FIRST and on its own locator, so removing
      // the wrapper fails this on geometry rather than on a missing element —
      // a test that only proves "a region exists" would still pass if the
      // region stopped scrolling.
      await openImports(page, JOBS);
      await page.setViewportSize({ width: 320, height: 900 });

      const admin = await adminGeometry(page);
      expect(admin.scroll, "the admin column must not scroll sideways").toBe(admin.client);

      const geo = await geometry(page.getByRole("region", { name: /Recent jobs/ }));
      expect(geo.scroll).toBeGreaterThan(geo.client);

      // ...and it was never the filename length that decided it. Even a
      // seven-character name overflows (measured 336 into 238), so what #315
      // measured as fitting was the EMPTY jobs array almost every Imports spec
      // passes — the same "mock admin lists with CONTENT, not empty arrays"
      // trap docs/TESTING.md records for the axe scans. Re-measured here so a
      // future reader does not repeat the measurement and get the old answer.
      await openImports(page, [{ id: 1, filename: "a.accdb", status: "swapped",
        updated_at: 1_700_000_000 }]);
      await page.setViewportSize({ width: 320, height: 900 });
      const shortAdmin = await adminGeometry(page);
      expect(shortAdmin.scroll).toBe(shortAdmin.client);
      const short = await geometry(page.getByRole("region", { name: /Recent jobs/ }));
      expect(short.scroll).toBeGreaterThan(short.client);
    });

  test("Recent jobs gets no scrollbar at 1280 that it does not need",
    async ({ page }) => {
      // The over-broad-fix guard. `overflow-x: auto` with no `min-width` has to
      // leave a fluid table alone at desktop, or the wrapper just parks a
      // permanent horizontal scrollbar under the table — which is why this one
      // gets no blanket `min-width` the way `.grid.data` does.
      await openImports(page, JOBS);
      const wide = await geometry(page.getByRole("region", { name: /Recent jobs/ }));
      expect(wide.scroll, "must not scroll at 1280").toBe(wide.client);
    });

  test("the Recent jobs region is focusable, because tabbing lands at its far edge",
    async ({ page }) => {
      // WCAG 2.1.1, and the reason `needsScrollRegion` returns true for this
      // shape: no header here is a sort button, so the only thing a keyboard
      // can land on is the "view" button in the LAST column. Tabbing to it
      // scrolls the region hard right, and without a tab stop of its own there
      // is then nothing to bring File and Status back into view.
      await openImports(page, JOBS);
      await page.setViewportSize({ width: 320, height: 900 });

      const region = page.getByRole("region", { name: /Recent jobs/ });
      await region.focus();
      await expect(region).toBeFocused();
      // Tabbing to the row action really does strand the left-hand columns —
      // the condition the tab stop exists for, asserted rather than assumed.
      await page.getByRole("button", { name: "view" }).first().focus();
      expect(await region.evaluate((el) => el.scrollLeft)).toBeGreaterThan(0);
      // And the region's name is not the table's own, or it announces twice.
      expect(await region.getAttribute("aria-label"))
        .not.toBe(await page.getByRole("table", { name: "Recent jobs" })
          .getAttribute("aria-label"));
    });

  test("a table whose rows are already keyboard-reachable gets NO extra tab stop",
    async ({ page }) => {
      // The other half of the derivation, and why it is a predicate rather than
      // "add a region everywhere": all three Users tables have a sort button in
      // every header and action buttons in every row, so a region would be a
      // tab stop before each of three mounted tables for no gain.
      await openUsers(page, "/admin/users/current");
      await expect(page.locator("#userpanel-current .table-scroll")).toBeVisible();
      await expect(page.locator("#userpanel-current .table-scroll[role=region]"))
        .toHaveCount(0);
    });
});
