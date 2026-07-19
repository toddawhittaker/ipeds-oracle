import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockConversation,
  mockDeleteConversation,
  mockStreamChat,
} from "./mocks.js";

// TDD (written before the implementation exists — see the a11y-reviewer's
// consult that specced these targets). PR #62 made deleting a conversation
// also navigate; that navigation dropped focus to <body>, stranding
// keyboard/screen-reader users. This spec pins the fix:
//
//   Case 1 (delete the OPEN conversation) -> navigate to "/", focus the
//   composer (#composer-input).
//   Case 2 (delete a DIFFERENT conversation) -> stay put, focus whatever now
//   occupies the deleted row's index: next sibling's .convo button, else the
//   previous sibling (deleted row was last), else "+ New chat" (no rows
//   left), else the composer (defensive last resort). Never <body>.
//
// The deletion outcome is surfaced via the app-wide TOAST (useToast) — a
// visible, self-announcing confirmation. (It used to be an sr-only
// delete-announcer node; that was removed when the toast took over.)
//
// Deletion is now gated by the app-styled confirmation modal (ConfirmModal.jsx),
// NOT window.confirm: clicking a row's trash opens a role="alertdialog" titled
// `Delete "<title>"?` with a "Delete chat" confirm button and a "Cancel" button.
// Each .convo button carries id={`convo-${c.id}`} and each trash button a per-row
// aria-label `Delete chat: ${c.title || "Untitled"}` (WCAG 4.1.2).

// The row's rename/delete buttons are hover-revealed (their .convo-actions
// overlay is pointer-events:none until the row is hovered, so a full-width title
// stays fully clickable). A real click therefore needs the row hovered first;
// hover({force:true}) positions the mouse over the row — activating :hover so the
// button turns interactive — without tripping the hidden button's own
// actionability check. Returns the (now-clickable) button locator.
async function revealRowButton(loc) {
  await loc.hover({ force: true });
  return loc;
}

// Open the delete modal for a given row and click through its confirm button.
// Scoped to the dialog so the "Delete chat" name doesn't collide with the row
// trash buttons (whose labels also contain "Delete chat").
async function confirmDelete(page, rowLabel) {
  await (await revealRowButton(page.getByRole("button", { name: rowLabel }))).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Delete chat", exact: true }).click();
}

test.describe("delete-conversation focus management", () => {
  test("Case 1: deleting the CURRENTLY-OPEN conversation navigates to / and moves focus to the composer", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [
      { id: 1, title: "Chat One" },
      { id: 2, title: "Chat Two" },
      { id: 3, title: "Chat Three" },
    ]);
    await mockConversation(page, 2, [
      { role: "user", content: "Q2" },
      { role: "assistant", content: "A2" },
    ]);
    const del = await mockDeleteConversation(page);

    await page.goto("/chat/2");
    await expect(page.getByText("A2")).toBeVisible();

    // The modal names the specific chat before confirming (specific title, not a
    // vague "Are you sure?").
    await (await revealRowButton(page.getByRole("button", { name: "Delete chat: Chat Two" }))).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText('Delete "Chat Two"?');
    await dialog.getByRole("button", { name: "Delete chat", exact: true }).click();

    await expect.poll(() => del.calls).toEqual(["2"]);

    await expect.poll(() => new URL(page.url()).pathname).toBe("/");
    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeVisible();
    await expect(page.locator("#composer-input")).toBeFocused();

    // The deletion surfaces as a toast (was an sr-only announcer; now visible +
    // announced via the toast host). Wording branches are in src/announce.test.js.
    await expect(page.locator(".toast-msg")).toContainText("Deleted");
  });

  test("Case 2: deleting a DIFFERENT conversation leaves the open one untouched and moves focus to the NEXT sibling row", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    const convos = await mockConversations(page, [
      { id: 1, title: "Chat One" },
      { id: 2, title: "Chat Two" },
      { id: 3, title: "Chat Three" },
    ]);
    await mockConversation(page, 1, [
      { role: "user", content: "Q1" },
      { role: "assistant", content: "A1" },
    ]);
    const del = await mockDeleteConversation(page);

    await page.goto("/chat/1");
    await expect(page.getByText("A1")).toBeVisible();

    // Anticipate refreshConvos() picking up the post-delete list -- the
    // established convention (see routing-chat.spec.js's "convos.setList(...)
    // // reflected by refreshConvos() after the turn").
    convos.setList([
      { id: 1, title: "Chat One" },
      { id: 3, title: "Chat Three" },
    ]);

    await confirmDelete(page, "Delete chat: Chat Two");

    await expect.poll(() => del.calls).toEqual(["2"]);
    expect(new URL(page.url()).pathname).toBe("/chat/1");
    await expect(page.getByText("A1")).toBeVisible();

    // Chat Three was chat Two's next sibling in the DOM at delete time, and
    // now occupies the deleted row's index.
    await expect(page.locator("#convo-3")).toBeFocused();

    await expect(page.locator(".toast-msg")).toContainText("Deleted"); // wording in src/announce.test.js
  });

  test("deleting the LAST row falls back to the PREVIOUS sibling (no next sibling exists)", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    const convos = await mockConversations(page, [
      { id: 1, title: "Chat One" },
      { id: 2, title: "Chat Two" },
      { id: 3, title: "Chat Three" },
    ]);
    await mockConversation(page, 1, [
      { role: "user", content: "Q1" },
      { role: "assistant", content: "A1" },
    ]);
    const del = await mockDeleteConversation(page);

    await page.goto("/chat/1");
    await expect(page.getByText("A1")).toBeVisible();

    convos.setList([
      { id: 1, title: "Chat One" },
      { id: 2, title: "Chat Two" },
    ]);

    await confirmDelete(page, "Delete chat: Chat Three");

    await expect.poll(() => del.calls).toEqual(["3"]);
    await expect(page.locator("#convo-2")).toBeFocused();
  });

  test("deleting the ONLY row falls back to '+ New chat' and announces none remaining", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    const convos = await mockConversations(page, [{ id: 9, title: "Solo Chat" }]);
    const del = await mockDeleteConversation(page);

    await page.goto("/");
    // exact:true -- getByRole name-matching is substring by default, and the
    // sibling trash button's aria-label ("Delete chat: Solo Chat") now
    // CONTAINS this row's bare title, so an unscoped substring match would
    // hit both the row link and the trash button (strict-mode violation).
    // The row itself is a real react-router <a> link.
    await expect(page.getByRole("link", { name: "Solo Chat", exact: true })).toBeVisible();

    convos.setList([]);

    await confirmDelete(page, "Delete chat: Solo Chat");

    await expect.poll(() => del.calls).toEqual(["9"]);
    await expect(page.getByRole("link", { name: "+ New chat" })).toBeFocused();

    await expect(page.locator(".toast-msg")).toContainText("Deleted"); // wording in src/announce.test.js
  });

  // Regression guard: two identically-titled ("Untitled") conversations deleted
  // back to back must each be ANNOUNCED. With the toast host each push is its own
  // keyed live-region child, so a screen reader re-announces even when the two
  // messages are worded identically — the guarantee is now STRUCTURAL (two
  // distinct toasts), not "the single region's text happened to change". (The
  // count still differentiates the wording as UX — pinned in src/announce.test.js.)
  test("REGRESSION: two consecutive deletes of identically-titled conversations each raise their own toast", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    const convos = await mockConversations(page, [
      { id: 5, title: "" },
      { id: 6, title: "" },
    ]);
    const del = await mockDeleteConversation(page);

    await page.goto("/");
    await expect(page.getByRole("link", { name: "+ New chat" })).toBeVisible();

    convos.setList([{ id: 6, title: "" }]);
    // Both rows are identically named "Delete chat: Untitled" at this point --
    // .first() targets the earlier (id 5) row in DOM order.
    await (await revealRowButton(page.getByRole("button", { name: "Delete chat: Untitled" }).first())).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Delete chat", exact: true }).click();
    await expect.poll(() => del.calls).toEqual(["5"]);
    await expect(page.locator(".toast")).toHaveCount(1);

    convos.setList([]);
    await confirmDelete(page, "Delete chat: Untitled");
    await expect.poll(() => del.calls).toEqual(["5", "6"]);
    // A SECOND toast (not a mutation of the first) — each announces on its own.
    await expect(page.locator(".toast")).toHaveCount(2);
  });

  test("cancelling the confirm modal returns focus to that row's trash button, fires no DELETE, and raises no toast", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [
      { id: 1, title: "Chat One" },
      { id: 2, title: "Chat Two" },
    ]);
    await mockConversation(page, 1, [
      { role: "user", content: "Q1" },
      { role: "assistant", content: "A1" },
    ]);
    const del = await mockDeleteConversation(page);

    await page.goto("/chat/1");
    await expect(page.getByText("A1")).toBeVisible();

    const trashBtn = page.getByRole("button", { name: "Delete chat: Chat Two" });
    await (await revealRowButton(trashBtn)).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toHaveCount(0);

    // Let anything an errant implementation might do asynchronously settle
    // before asserting zero -- see the same pattern in routing-chat.spec.js's
    // "must NEVER be called" assertions.
    await page.waitForTimeout(200);
    expect(del.calls).toEqual([]);
    // Cancel/dismiss returns focus to the opener (still mounted -- nothing
    // mutated) and, per the locked spec decision, raises NO toast.
    await expect(trashBtn).toBeFocused();
    await expect(page.locator(".toast")).toHaveCount(0);
  });

  test("pressing Escape on the confirm modal cancels it: no DELETE, focus back on the trash button", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [
      { id: 1, title: "Chat One" },
      { id: 2, title: "Chat Two" },
    ]);
    await mockConversation(page, 1, [
      { role: "user", content: "Q1" },
      { role: "assistant", content: "A1" },
    ]);
    const del = await mockDeleteConversation(page);

    await page.goto("/chat/1");
    await expect(page.getByText("A1")).toBeVisible();

    const trashBtn = page.getByRole("button", { name: "Delete chat: Chat Two" });
    await (await revealRowButton(trashBtn)).click();
    await expect(page.getByRole("alertdialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("alertdialog")).toHaveCount(0);

    await page.waitForTimeout(200);
    expect(del.calls).toEqual([]);
    await expect(trashBtn).toBeFocused();
  });

  test("a failed delete (500) keeps the modal open with an in-modal error, keeps the row, and never claims 'Deleted'", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [
      { id: 1, title: "Chat One" },
      { id: 2, title: "Chat Two" },
    ]);
    await mockConversation(page, 1, [
      { role: "user", content: "Q1" },
      { role: "assistant", content: "A1" },
    ]);
    const del = await mockDeleteConversation(page, { httpStatus: 500 });

    await page.goto("/chat/1");
    await expect(page.getByText("A1")).toBeVisible();

    await (await revealRowButton(page.getByRole("button", { name: "Delete chat: Chat Two" }))).click();
    const dialog = page.getByRole("alertdialog");
    await dialog.getByRole("button", { name: "Delete chat", exact: true }).click();

    await expect.poll(() => del.calls).toEqual(["2"]);
    // The modal STAYS OPEN on failure, showing a contextual in-modal error, and
    // the row must still be there (a failed DELETE must not optimistically
    // vanish it). exact:true -- see the "ONLY row" note above.
    await expect(dialog).toBeVisible();
    await expect(dialog.locator(".notice.error")).toBeVisible();
    // The row survives a failed delete. A CSS locator (not getByRole) because the
    // background is aria-hidden while the modal is open -- so it's absent from the
    // accessibility tree by design, but still present + visible in the DOM.
    await expect(page.locator("#convo-2")).toBeVisible();

    // A failed delete must surface an ERROR toast and must NEVER falsely claim
    // success. Exact failure copy is a constant in src/announce.js; the
    // load-bearing guarantee here is the "never Deleted" negative.
    const toast = page.locator(".toast");
    await expect(toast).toHaveClass(/\berror\b/);
    await expect(toast).not.toContainText("Deleted");

    // Focus stays trapped in the still-open modal, never dropped to <body>.
    // globalThis.document (not `document`) -- this callback runs in the browser,
    // but the e2e eslint config only has Node globals.
    const activeTag = await page.evaluate(() => globalThis.document.activeElement?.tagName);
    expect(activeTag).not.toBe("BODY");
  });

  // Code-review-style regression: `convos` changes for lots of reasons that
  // have nothing to do with a delete -- mount, after every submit,
  // optimistic title patch. A naive `useEffect(() => focusRow(), [convos])`
  // would re-fire on every one of those and yank focus back into the
  // sidebar. The implementer's one-shot ref guard must only act on the
  // convos refresh that immediately follows an actual delete.
  test("a later refreshConvos() (triggered by an unrelated submit) does not steal focus back into the sidebar", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    const convos = await mockConversations(page, [
      { id: 1, title: "Chat One" },
      { id: 2, title: "Chat Two" },
    ]);
    await mockConversation(page, 1, [
      { role: "user", content: "Q1" },
      { role: "assistant", content: "A1" },
    ]);
    const del = await mockDeleteConversation(page);
    await mockStreamChat(page, {
      conversationId: 1,
      answer: "Follow-up answer.",
      messageId: 9,
      userMessageId: 10,
    });

    await page.goto("/chat/1");
    await expect(page.getByText("A1")).toBeVisible();

    convos.setList([{ id: 1, title: "Chat One" }]);
    await confirmDelete(page, "Delete chat: Chat Two");
    await expect.poll(() => del.calls).toEqual(["2"]);
    // Deleted row (Chat Two) was the last/only other row -> falls back to
    // the previous sibling, Chat One's own row button.
    await expect(page.locator("#convo-1")).toBeFocused();

    // The user moves on and continues the conversation -- deliberately move
    // focus into the composer first so a later refetch stealing it back is
    // actually observable.
    await page.locator("#composer-input").fill("Follow-up question");
    await expect(page.locator("#composer-input")).toBeFocused();

    await page.getByRole("button", { name: "Send" }).click();
    await expect(page.getByText("Follow-up answer.")).toBeVisible();

    // submit() calls refreshConvos() again once the stream completes --
    // convos changes for a reason that has nothing to do with the earlier
    // delete. Focus must not have been yanked back to #convo-1.
    await expect(page.locator("#convo-1")).not.toBeFocused();
  });

  // Several pre-existing specs (routing-chat.spec.js) use an UNSCOPED
  // page.locator('[role="status"].sr-only') and rely on it resolving to exactly
  // one node (Chat's bad-conversation notice). The toast host's live regions are
  // bare aria-live, never role="status", so they don't collide -- this pins that
  // the count stays exactly one.
  test("[role=status].sr-only still resolves to exactly ONE node on the Chat page", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);

    await page.goto("/");

    await expect(page.locator('[role="status"].sr-only')).toHaveCount(1);
  });
});
