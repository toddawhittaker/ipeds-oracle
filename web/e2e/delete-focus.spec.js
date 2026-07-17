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
// A separate, always-mounted, BARE aria-live="polite" node (data-testid
// "delete-announcer" — deliberately NOT role="status", see the last test in
// this file) carries a human-readable announcement of what happened.
//
// The implementer is expected to add id={`convo-${c.id}`} to each .convo
// button (used throughout this file to target a specific row), a per-row
// aria-label of `Delete chat: ${c.title || "Untitled"}` on the trash button
// (replacing today's identical "Delete chat" on every row -- WCAG 4.1.2),
// and a window.confirm message of `Delete "<title>"? This can't be undone.`
//
// Renaming the trash button's accessible name is a BREAKING change to
// existing selectors -- routing-chat.spec.js's two `getByRole("button",
// { name: "Delete chat" })` locators were updated in the same commit as this
// new file to the per-row name.

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

    let confirmMessage = null;
    page.once("dialog", (d) => { confirmMessage = d.message(); d.accept(); });
    await page.getByRole("button", { name: "Delete chat: Chat Two" }).click();

    await expect.poll(() => del.calls).toEqual(["2"]);
    // Assert the exact confirm wording outside the dialog handler -- an
    // expect() thrown inside a Playwright dialog callback doesn't reliably
    // fail the test.
    expect(confirmMessage).toBe("Delete \"Chat Two\"? This can't be undone.");

    await expect.poll(() => new URL(page.url()).pathname).toBe("/");
    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeVisible();
    await expect(page.locator("#composer-input")).toBeFocused();

    const announcer = page.getByTestId("delete-announcer");
    await expect(announcer).toHaveText('Deleted "Chat Two". Started a new chat.');
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

    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: "Delete chat: Chat Two" }).click();

    await expect.poll(() => del.calls).toEqual(["2"]);
    expect(new URL(page.url()).pathname).toBe("/chat/1");
    await expect(page.getByText("A1")).toBeVisible();

    // Chat Three was chat Two's next sibling in the DOM at delete time, and
    // now occupies the deleted row's index.
    await expect(page.locator("#convo-3")).toBeFocused();

    const announcer = page.getByTestId("delete-announcer");
    await expect(announcer).toHaveText('Deleted "Chat Two". 2 chats remaining.');
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

    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: "Delete chat: Chat Three" }).click();

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

    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: "Delete chat: Solo Chat" }).click();

    await expect.poll(() => del.calls).toEqual(["9"]);
    await expect(page.getByRole("link", { name: "+ New chat" })).toBeFocused();

    const announcer = page.getByTestId("delete-announcer");
    await expect(announcer).toHaveText('Deleted "Solo Chat". No chats remaining.');
  });

  // Regression guard: a live region only announces to a screen reader on a
  // TEXT MUTATION. Two identically-titled ("Untitled") conversations deleted
  // back to back would produce an IDENTICAL announcement string if the
  // wording were just `Deleted "Untitled".` -- the second delete would be
  // silently swallowed. The remaining-chat count is what forces the two
  // messages to differ, so this pins that the count is actually load-bearing.
  test("REGRESSION: two consecutive deletes of identically-titled conversations produce DIFFERENT announcer text", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    const convos = await mockConversations(page, [
      { id: 5, title: "" },
      { id: 6, title: "" },
    ]);
    const del = await mockDeleteConversation(page);

    await page.goto("/");
    await expect(page.getByRole("link", { name: "+ New chat" })).toBeVisible();

    const announcer = page.getByTestId("delete-announcer");

    convos.setList([{ id: 6, title: "" }]);
    page.once("dialog", (d) => d.accept());
    // Both rows are identically named "Delete chat: Untitled" at this point --
    // .first() targets the earlier (id 5) row in DOM order.
    await page.getByRole("button", { name: "Delete chat: Untitled" }).first().click();
    await expect.poll(() => del.calls).toEqual(["5"]);
    await expect(announcer).toHaveText('Deleted "Untitled". 1 chat remaining.');
    const firstText = await announcer.textContent();

    convos.setList([]);
    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: "Delete chat: Untitled" }).click();
    await expect.poll(() => del.calls).toEqual(["5", "6"]);
    await expect(announcer).toHaveText('Deleted "Untitled". No chats remaining.');
    const secondText = await announcer.textContent();

    expect(secondText).not.toBe(firstText);
  });

  test("dismissing the confirm dialog returns focus to that row's trash button, fires no DELETE, and leaves the announcer empty", async ({ page }) => {
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

    const announcer = page.getByTestId("delete-announcer");
    await expect(announcer).toHaveText("");

    const trashBtn = page.getByRole("button", { name: "Delete chat: Chat Two" });
    page.once("dialog", (d) => d.dismiss());
    await trashBtn.click();

    // Let anything an errant implementation might do asynchronously settle
    // before asserting zero -- see the same pattern in routing-chat.spec.js's
    // "must NEVER be called" assertions.
    await page.waitForTimeout(200);
    expect(del.calls).toEqual([]);
    await expect(trashBtn).toBeFocused();
    await expect(announcer).toHaveText("");
  });

  test("a failed delete (500) keeps the row, announces failure -- never 'Deleted' -- and never leaves focus on <body>", async ({ page }) => {
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

    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: "Delete chat: Chat Two" }).click();

    await expect.poll(() => del.calls).toEqual(["2"]);
    // The row must still be there -- a failed DELETE must not optimistically
    // vanish it. exact:true -- see the same note in the "ONLY row" test above.
    await expect(page.getByRole("link", { name: "Chat Two", exact: true })).toBeVisible();

    const announcer = page.getByTestId("delete-announcer");
    await expect(announcer).toHaveText("Couldn't delete that chat.");
    await expect(announcer).not.toContainText("Deleted");

    // globalThis.document (not `document` directly) -- this callback runs in
    // the browser, but the e2e eslint config only has Node globals, and
    // globalThis dodges the no-undef complaint the same way routing-chat.spec.js's
    // globalThis.history reference does.
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
    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: "Delete chat: Chat Two" }).click();
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
  // page.locator('[role="status"].sr-only') and rely on it resolving to
  // exactly one node. The new delete announcer must therefore be a BARE
  // aria-live region, never role="status" -- otherwise it collides and those
  // specs break with a Playwright strict-mode violation.
  test("[role=status].sr-only still resolves to exactly ONE node on the Chat page", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);

    await page.goto("/");

    await expect(page.locator('[role="status"].sr-only')).toHaveCount(1);
  });
});
