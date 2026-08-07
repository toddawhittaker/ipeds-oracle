import { test, expect } from "@playwright/test";

import {
  mockMe, mockConversations, mockConversation, mockStreamChat, mockAttention,
  mockMarkLogsSeen, mockAllowlist, mockAccessRequests, mockDeniedRequests,
  mockSkills, mockLogs, mockImportCatalog, mockImportJobs, mockIntegrate,
  mockImportJobPoll,
} from "./mocks.js";

// WCAG 2.4.3: a control that disables or unmounts ITSELF on activation drops
// focus to <body>, stranding a keyboard or screen-reader user at the top of the
// document — usually at the exact moment something is happening that they now
// cannot follow.
//
// The repo already fixed this four times deliberately (DataTable.goPage,
// ConfirmModal.confirmAction, BulkBar.onFocusFallback, Chat.stopGenerating).
// These are the ones that were left.
//
// Every case asserts the SPECIFIC destination, not merely "not BODY" — landing
// somewhere arbitrary is its own bug, and a not-BODY assertion would pass for
// any of them.

const USER = { email: "user@example.edu", is_admin: false };
const ADMIN = { email: "admin@example.edu", is_admin: true };

const activeTag = (page) =>
  page.evaluate(() => globalThis.document.activeElement?.tagName);

test.describe("controls that disable themselves keep focus somewhere useful", () => {
  test("a suggestion chip hands focus to the composer", async ({ page }) => {
    // The chips are disabled={busy} and submit() sets busy in the same tick, so
    // the chip the user just activated disables WHILE FOCUSED.
    await mockMe(page, USER);
    await mockConversations(page, [{ id: 5, title: "T", updated_at: 0 }]);
    await mockConversation(page, 5, [
      { id: 1, role: "user", content: "q" },
      { id: 2, role: "assistant", content: "An answer.",
        suggestions: ["Ask something else?"] },
    ]);
    await mockStreamChat(page, { conversationId: 5, delayMs: 400, answer: "Next." });
    await page.goto("/chat/5");

    const chip = page.getByRole("button", { name: "Ask something else?" });
    await chip.focus();
    await expect(chip).toBeFocused();
    await chip.click();

    // Straight to the composer — the documented free-text escape hatch, and
    // where the user's attention has to go next anyway.
    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeFocused();
    expect(await activeTag(page)).toBe("TEXTAREA");
  });

  test("a clarify chip hands focus to the composer", async ({ page }) => {
    // The worst instance: a clarify's chips are the ONLY UI for its answer
    // phrases, so the user answering a blocking disambiguation is precisely the
    // one navigating them by keyboard.
    await mockMe(page, USER);
    await mockConversations(page, [{ id: 6, title: "T", updated_at: 0 }]);
    await mockConversation(page, 6, [
      { id: 1, role: "user", content: "which major?" },
      { id: 2, role: "assistant", content: "Did you mean:",
        clarify: { question: "Which award level?", options: ["Bachelor's only"] } },
    ]);
    await mockStreamChat(page, { conversationId: 6, delayMs: 400, answer: "Next." });
    await page.goto("/chat/6");

    const chip = page.getByRole("button", { name: "Bachelor's only" });
    await chip.focus();
    await chip.click();

    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeFocused();
  });

  test("Rerun hands focus to the composer instead of its own disabled button",
    async ({ page }) => {
      // Rerun is disabled={busy}. On the modal-free last-turn path it disables
      // itself directly; on the confirm path ConfirmModal correctly returns
      // focus to its opener — which is disabled by then, so even correct modal
      // a11y ends at <body>.
      await mockMe(page, USER);
      await mockConversations(page, [{ id: 7, title: "T", updated_at: 0 }]);
      await mockConversation(page, 7, [
        { id: 1, role: "user", content: "a question" },
        { id: 2, role: "assistant", content: "An answer." },
      ]);
      await mockStreamChat(page, { conversationId: 7, delayMs: 400, answer: "Redone." });
      await page.goto("/chat/7");

      await page.getByRole("button", { name: "Rerun" }).click();
      await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeFocused();
    });

  test("the CSV button stays focusable while it prepares", async ({ page }) => {
    // aria-disabled rather than :disabled, so the button the user activated is
    // still there to hold focus while the export builds and any error toast
    // lands. The handler's own early return is what prevents a second download.
    await mockMe(page, USER);
    await mockConversations(page, [{ id: 8, title: "T", updated_at: 0 }]);
    await mockConversation(page, 8, [
      { id: 1, role: "user", content: "q" },
      // sql_log is REQUIRED for the server-side export path — without a query
      // the button correctly falls back to the client-side CSV, which never
      // enters the `downloading` state this test is about.
      { id: 2, role: "assistant", sql_log: ["SELECT instnm, awards FROM c_a"],
        content: "Here.\n\n| Institution | Awards |\n| --- | --- |\n| Alpha | 12 |\n| Beta | 9 |" },
    ]);
    await page.goto("/chat/8");

    // A SLOW export, so the assertion lands while `downloading` is true. An
    // idle-state check would be worthless: `disabled={downloading}` renders no
    // attribute at all when false, so "has no disabled attribute" passes
    // against the unfixed code too. The bug only exists mid-download.
    await page.route("**/download.csv*", async (route) => {
      await new Promise((r) => setTimeout(r, 1200));
      await route.fulfill({ status: 200, contentType: "text/csv", body: "a,b\n1,2\n" });
    });

    // Located structurally, NOT by accessible name: the label deliberately
    // becomes "Preparing…" while downloading, so a name-based locator stops
    // matching at exactly the moment under test and the assertion retries
    // against nothing.
    const csv = page.locator(".table-tools button.link").first();
    await expect(csv).toHaveText(/CSV/);
    await csv.focus();
    await expect(csv).toBeFocused();
    await csv.click();

    // Mid-export: the button reports itself unavailable but is STILL the
    // focused element. Natively disabled, it would have vanished from the
    // focus order and dumped the user on <body> — with a toast possibly
    // arriving that they are no longer positioned to hear.
    await expect(csv).toHaveAttribute("aria-disabled", "true");
    await expect(csv).toBeFocused();
    expect(await activeTag(page)).toBe("BUTTON");
  });

  test("starting an import moves focus to the notice explaining the lock",
    async ({ page }) => {
      // submitIntegrate calls notify("") on the happy path, so the existing
      // focus-to-notice effect never fires, while setIntegrating(true) disables
      // the button just pressed. Focus has to go somewhere that explains why the
      // controls vanished.
      await mockMe(page, ADMIN);
      await mockConversations(page, []);
      await mockAllowlist(page, []);
      await mockAccessRequests(page, []);
      await mockDeniedRequests(page, []);
      await mockSkills(page, []);
      await mockLogs(page, []);
      await mockAttention(page, { users: 0, skills: 0, logs: 0 });
      await mockMarkLogsSeen(page);
      await mockImportJobs(page, []);
      await mockImportCatalog(page, {
        probed_at: 0, partial: false,
        years: [{
          start_year: 2024, year: 2025, year_label: "2024-25", status: "final",
          integrated: false, available: true, release: "Final", selectable: true,
          zip_bytes: 1000,
        }],
        disk: { free_bytes: 9e11, total_bytes: 1e12, used_bytes: 1e11 },
        // calibration null on purpose: with it present the disk estimator runs,
        // and an empty object makes it report "insufficient", which disables
        // the very button under test. Null skips the estimate entirely.
        calibration: null,
      });
      await mockIntegrate(page, { jobId: 9, status: "running" });
      // watch() polls the job detail immediately; without this the fetch
      // escapes the mocks and the panel never settles.
      await mockImportJobPoll(page, 9, [
        { id: 9, filename: "integrate:2024", status: "running", log: "working\n" },
      ]);
      await page.goto("/admin/imports");

      // The whole year card is the toggle; its accessible name is
      // `Integrate {year_label} ({release})`.
      await page.getByRole("checkbox", { name: "Integrate 2024-25 (Final)" }).click();
      await page.getByRole("button", { name: /Integrate selected/ }).click();
      // Adding years is confirmed now (same as removing one) — clear the modal.
      await page.getByRole("alertdialog").getByRole("button", { name: "Start rebuild" }).click();

      const lock = page.getByText(/controls are locked until it finishes/);
      await expect(lock).toBeVisible();
      await expect(lock).toBeFocused();
    });
});
