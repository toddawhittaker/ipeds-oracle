import { test, expect } from "@playwright/test";
import { mockMe, mockConversations } from "./mocks.js";

// Browser-truth for the reusable confirmation modal (ConfirmModal.jsx). The
// per-feature wording/flows live in each feature's spec (delete-focus,
// year-remove, deny-access-request, users-table, admin-lessons); THIS spec pins
// the component contract itself, driven through the delete-chat danger flow as a
// representative vehicle: overlay + inert background, focus trap, the destructive
// action NOT auto-focused, the async processing/loading state, in-modal error +
// retry on failure, and role=alertdialog semantics.

// A DELETE mock we fully control: an optional delay, and a fail-first-then-succeed
// mode for the retry test. Registered AFTER mockConversation so its GET falls
// through (see mocks.js mockDeleteConversation's ordering note).
async function controllableDelete(page, { delayMs = 0, failTimes = 0 } = {}) {
  const state = { calls: 0 };
  await page.route("**/api/chat/conversations/*", async (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    state.calls += 1;
    if (delayMs) await new Promise((r) => setTimeout(r, delayMs));
    const fail = state.calls <= failTimes;
    await route.fulfill({
      status: fail ? 500 : 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: !fail }),
    });
  });
  return state;
}

async function openDeleteModal(page, { delayMs = 0, failTimes = 0 } = {}) {
  await mockMe(page, { email: "user@example.edu", is_admin: false });
  await mockConversations(page, [
    { id: 1, title: "Chat One" },
    { id: 2, title: "Chat Two" },
  ]);
  const del = await controllableDelete(page, { delayMs, failTimes });
  await page.goto("/");
  await page.getByRole("link", { name: "+ New chat" }).waitFor();
  const opener = page.getByRole("button", { name: "Delete chat: Chat One" });
  // Hover-revealed control (pointer-events:none until the row is hovered):
  // force-hover positions the mouse over the row so the trash turns clickable.
  await opener.hover({ force: true });
  await opener.click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  return { del, dialog, opener };
}

test.describe("confirmation modal — component contract", () => {
  test("uses role=alertdialog with an accessible name (title) and description", async ({ page }) => {
    const { dialog } = await openDeleteModal(page);
    await expect(dialog).toHaveAttribute("aria-modal", "true");
    // Accessible name comes from the title via aria-labelledby.
    await expect(dialog).toHaveAccessibleName('Delete "Chat One"?');
    // aria-describedby points at the body copy.
    await expect(dialog).toHaveAttribute("aria-describedby", /confirm-body-/);
  });

  test("the destructive action is NOT auto-focused — focus lands on Cancel", async ({ page }) => {
    const { dialog } = await openDeleteModal(page);
    await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();
    await expect(dialog.getByRole("button", { name: "Delete chat", exact: true })).not.toBeFocused();
  });

  test("dims + inerts the background while open, and restores it on close", async ({ page }) => {
    const { dialog } = await openDeleteModal(page);
    // The scrim exists and the app shell is inert + hidden from AT.
    await expect(page.locator(".modal-overlay")).toBeVisible();
    await expect(page.locator(".app[inert]")).toHaveCount(1);
    await expect(page.locator('.app[aria-hidden="true"]')).toHaveCount(1);
    // Closing removes both.
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toHaveCount(0);
    await expect(page.locator(".app[inert]")).toHaveCount(0);
    await expect(page.locator('.app[aria-hidden="true"]')).toHaveCount(0);
  });

  test("traps focus: Tab cycles between Cancel and the confirm button, never escaping", async ({ page }) => {
    const { dialog } = await openDeleteModal(page);
    const cancel = dialog.getByRole("button", { name: "Cancel" });
    const confirm = dialog.getByRole("button", { name: "Delete chat", exact: true });
    await expect(cancel).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(confirm).toBeFocused();
    // Tab off the last control wraps back to the first.
    await page.keyboard.press("Tab");
    await expect(cancel).toBeFocused();
    // Shift+Tab off the first wraps to the last.
    await page.keyboard.press("Shift+Tab");
    await expect(confirm).toBeFocused();
  });

  test("clicking the dimmed overlay dismisses (before processing), firing no action", async ({ page }) => {
    const { del, dialog } = await openDeleteModal(page);
    // Click the scrim itself, at the top-left corner well clear of the panel.
    await page.locator(".modal-overlay").click({ position: { x: 5, y: 5 } });
    await expect(dialog).toHaveCount(0);
    await page.waitForTimeout(150);
    expect(del.calls).toBe(0);
  });

  test("while processing: buttons disable, a spinner shows, and Escape can't dismiss", async ({ page }) => {
    const { dialog } = await openDeleteModal(page, { delayMs: 1500 });
    const confirm = dialog.getByRole("button", { name: "Delete chat", exact: true });
    await confirm.click();
    // The confirm button enters a busy/loading state; both controls disable.
    await expect(confirm).toHaveAttribute("aria-busy", "true");
    await expect(dialog.locator(".modal-confirm .spinner")).toBeVisible();
    // The in-flight state is announced politely to AT (WCAG 4.1.3).
    await expect(dialog.locator('[aria-live="polite"]')).toHaveText("Working…");
    await expect(confirm).toBeDisabled();
    await expect(dialog.getByRole("button", { name: "Cancel" })).toBeDisabled();
    // Escape must NOT dismiss mid-flight.
    await page.keyboard.press("Escape");
    await expect(dialog).toBeVisible();
    // It resolves and closes on its own.
    await expect(dialog).toHaveCount(0);
  });

  test("on failure the modal stays open with an in-modal error + error toast, then a retry succeeds", async ({ page }) => {
    const { del, dialog } = await openDeleteModal(page, { failTimes: 1 });
    const confirm = dialog.getByRole("button", { name: "Delete chat", exact: true });
    await confirm.click();

    // Stays open, shows a contextual in-modal error, and an error toast appears
    // (supplementing, not replacing, the in-modal error).
    await expect(dialog).toBeVisible();
    await expect(dialog.locator(".notice.error")).toBeVisible();
    await expect(page.locator(".toast.error")).toBeVisible();
    // The in-modal error is wired into the dialog's accessible description, so it's
    // re-exposed if the dialog is re-queried (WCAG — a11y review finding 5).
    await expect(dialog).toHaveAttribute("aria-describedby", /confirm-error-/);
    // Focus is back on the confirm button for a one-keystroke retry.
    await expect(confirm).toBeFocused();

    // Retry — the 2nd call succeeds, the modal closes.
    await confirm.click();
    await expect(dialog).toHaveCount(0);
    expect(del.calls).toBe(2);
  });
});
