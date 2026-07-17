import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockConversation,
  mockAllowlist,
  mockAccessRequests,
  mockDeniedRequests,
  mockStreamChat,
} from "./mocks.js";

// TDD (written before the implementation exists -- relayed by the architect
// via the project-manager). Pins the conversion of the three nav surfaces
// from <button onClick={navigate}> to real react-router links (<a href>):
//
//   1. Top nav (App.jsx): "Chat" -> <Link to="/">, "Admin" -> <Link to="/admin">.
//      Active one keeps aria-current="page" and the .on class.
//   2. Admin subtabs (Admin.jsx): the 5 ADMIN_TABS -> <NavLink to={`/admin/${t}`} end>.
//   3. Sidebar (Chat.jsx): conversation rows -> <Link to={`/chat/${c.id}`}
//      id={`convo-${c.id}`}>, with the .convo-del trash button staying a
//      SIBLING, never nested inside the link; "+ New chat" (both the
//      expanded and collapsed variants) -> <Link to="/"> with an onClick
//      that (a) on a plain click bumps turnToken and, if already at "/",
//      preventDefault()s and resets thread state directly, and (b) on a
//      modified/middle click early-returns with NO side effects, letting the
//      browser open "/" in a new tab.
//
// Every test below is expected RED against the current button-based UI: no
// <a href> exists yet for any of these controls, so getByRole("link", ...)
// finds nothing and the href/DOM-shape/modifier-click assertions all fail.
//
// See routing-chat.spec.js, routing-admin.spec.js, delete-focus.spec.js and
// midstream-nav.spec.js for the pre-existing routing/focus/mid-stream
// contracts this conversion must not regress -- those specs' button
// selectors for these same four surfaces were flipped to getByRole("link",
// ...) in the same commit as this file.

async function mockAdminUsersTab(page, { isAdmin = true } = {}) {
  await mockMe(page, { email: "admin@example.edu", is_admin: isAdmin });
  await mockConversations(page, []);
  await mockAllowlist(page, [
    { email: "user@example.edu", note: "staff", is_admin: false, last_login: 1700000000 },
  ]);
  await mockAccessRequests(page, []);
  await mockDeniedRequests(page, []);
}

test.describe("top nav -- real links", () => {
  test("Chat and Admin are <a> with the right hrefs; the active one carries aria-current", async ({ page }) => {
    await mockAdminUsersTab(page);
    await page.goto("/");

    const chatLink = page.getByRole("link", { name: "Chat", exact: true });
    const adminLink = page.getByRole("link", { name: "Admin" });
    await expect(chatLink).toHaveAttribute("href", "/");
    await expect(adminLink).toHaveAttribute("href", "/admin");

    await expect(chatLink).toHaveAttribute("aria-current", "page");
    await expect(adminLink).not.toHaveAttribute("aria-current", "page");

    await adminLink.click();
    await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/users");
    await expect(adminLink).toHaveAttribute("aria-current", "page");
    await expect(chatLink).not.toHaveAttribute("aria-current", "page");
  });

  test("keyboard: focusing the Admin link and pressing Enter activates it", async ({ page }) => {
    await mockAdminUsersTab(page);
    await page.goto("/");

    const adminLink = page.getByRole("link", { name: "Admin" });
    await adminLink.focus();
    await expect(adminLink).toBeFocused();
    await page.keyboard.press("Enter");

    await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/users");
  });
});

test.describe("Admin subtabs -- real links", () => {
  test("each of the 5 subtabs is an <a href=\"/admin/<tab>\">, and only the active one carries aria-current", async ({ page }) => {
    await mockAdminUsersTab(page);
    await page.goto("/admin/users");

    const tabs = [
      ["Users", "/admin/users"],
      ["Imports", "/admin/imports"],
      ["Usage", "/admin/usage"],
      ["Skills", "/admin/skills"],
      ["Logs", "/admin/logs"],
    ];
    for (const [name, href] of tabs) {
      const link = page.getByRole("link", { name, exact: true });
      await expect(link).toHaveAttribute("href", href);
    }

    await expect(page.getByRole("link", { name: "Users", exact: true }))
      .toHaveAttribute("aria-current", "page");
    for (const name of ["Imports", "Usage", "Skills", "Logs"]) {
      await expect(page.getByRole("link", { name, exact: true }))
        .not.toHaveAttribute("aria-current", "page");
    }
  });

  test("clicking a different subtab navigates and flips aria-current (NavLink end-matching)", async ({ page }) => {
    await mockAdminUsersTab(page);
    await page.goto("/admin/users");

    const usersLink = page.getByRole("link", { name: "Users", exact: true });
    const logsLink = page.getByRole("link", { name: "Logs", exact: true });
    await logsLink.click();

    await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/logs");
    await expect(logsLink).toHaveAttribute("aria-current", "page");
    await expect(usersLink).not.toHaveAttribute("aria-current", "page");
  });

  test("keyboard: focusing the Imports subtab link and pressing Enter activates it; Space does not", async ({ page }) => {
    await mockAdminUsersTab(page);
    await page.goto("/admin/users");

    const importsLink = page.getByRole("link", { name: "Imports", exact: true });
    await importsLink.focus();
    await expect(importsLink).toBeFocused();

    // Links are not Space-activatable (that's button semantics) -- pressing
    // Space here must NOT navigate.
    await page.keyboard.press(" ");
    await page.waitForTimeout(150);
    expect(new URL(page.url()).pathname).toBe("/admin/users");

    await page.keyboard.press("Enter");
    await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/imports");
  });
});

test.describe("sidebar conversation rows -- real links, trash stays a sibling", () => {
  async function mockTwoConvos(page) {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [
      { id: 3, title: "CA nursing associate's degrees" },
      { id: 5, title: "Other chat" },
    ]);
    await mockConversation(page, 3, [
      { role: "user", content: "CA nursing associate's degrees" },
      { role: "assistant", content: "Here you go." },
    ]);
  }

  test("each row is an <a href=\"/chat/<id>\">, and the open one carries aria-current", async ({ page }) => {
    await mockTwoConvos(page);
    await page.goto("/chat/3");

    const rowA = page.getByRole("link", { name: "CA nursing associate's degrees", exact: true });
    const rowB = page.getByRole("link", { name: "Other chat", exact: true });
    await expect(rowA).toHaveAttribute("href", "/chat/3");
    await expect(rowB).toHaveAttribute("href", "/chat/5");
    await expect(rowA).toHaveAttribute("aria-current", "page");
    await expect(rowB).not.toHaveAttribute("aria-current", "page");
  });

  test("the trash button is a SIBLING of the row link, never nested inside it", async ({ page }) => {
    await mockTwoConvos(page);
    await page.goto("/chat/3");

    // Structural shape: exactly one row-link and one trash-button per row,
    // both direct children of .convo-row -- not one nested inside the other.
    await expect(page.locator(".convo-row > a.convo")).toHaveCount(2);
    await expect(page.locator(".convo-row > button.convo-del")).toHaveCount(2);
    // No interactive descendant inside the link at all.
    await expect(page.locator(".convo-row a.convo button")).toHaveCount(0);
    // The delete control is not itself matched by a ".convo a" query (i.e.
    // it isn't an anchor, and it isn't inside one).
    await expect(page.locator(".convo a button")).toHaveCount(0);
  });

  test("keyboard: focusing a conversation row link and pressing Enter navigates to it", async ({ page }) => {
    await mockTwoConvos(page);
    await mockConversation(page, 5, [
      { role: "user", content: "Other chat" },
      { role: "assistant", content: "Other answer." },
    ]);
    await page.goto("/chat/3");

    const rowB = page.getByRole("link", { name: "Other chat", exact: true });
    await rowB.focus();
    await expect(rowB).toBeFocused();
    await page.keyboard.press("Enter");

    await expect.poll(() => new URL(page.url()).pathname).toBe("/chat/5");
    await expect(page.getByText("Other answer.")).toBeVisible();
  });

  test("a modifier-click on a conversation row does not navigate the current tab", async ({ page }) => {
    await mockTwoConvos(page);
    await page.goto("/chat/3");
    await expect(page.getByText("Here you go.")).toBeVisible();

    // A real <a> with no custom onClick lets the browser's native
    // modified-click handling take over (open in a new tab) -- the CURRENT
    // page must stay exactly where it was, with no client-side navigation.
    const popupPromise = page.context().waitForEvent("page", { timeout: 2000 }).catch(() => null);
    await page.getByRole("link", { name: "Other chat", exact: true })
      .click({ modifiers: ["ControlOrMeta"] });
    const popup = await popupPromise;
    if (popup) await popup.close();

    expect(new URL(page.url()).pathname).toBe("/chat/3");
    await expect(page.getByText("Here you go.")).toBeVisible();
  });
});

test.describe("'+ New chat' -- real link, side effects preserved", () => {
  test("expanded '+ New chat' is an <a href=\"/\">", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [{ id: 3, title: "A" }]);
    await page.goto("/");

    const link = page.getByRole("link", { name: "+ New chat" });
    await expect(link).toHaveAttribute("href", "/");
  });

  test("collapsed '+' variant is also an <a href=\"/\">, reachable by its 'New chat' accessible name", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [{ id: 3, title: "A" }]);
    await page.goto("/");

    await page.getByRole("button", { name: "Collapse sidebar" }).click();
    const collapsedLink = page.getByRole("link", { name: "New chat" });
    await expect(collapsedLink).toHaveAttribute("href", "/");
  });

  // NOTE: the plain-click side-effect invariants for "+ New chat" (already-
  // at-"/" clears the thread with no duplicate history entry; from
  // /chat/:id navigates to / and Back returns; mid-stream abandonment does
  // not teleport once the stream lands) are pinned by the PRE-EXISTING
  // routing-chat.spec.js tests -- deliberately not duplicated here, per the
  // architect's design. Those tests' selectors were flipped to
  // getByRole("link", ...) in the same commit as this file; see
  // routing-chat.spec.js's "'+ New chat' pushes / (empty)...", "'+ New chat'
  // clears the thread when the URL was already /...", and
  // "'+ New chat' mid-stream does not teleport..." tests.

  test("modifier-click on '+ New chat' fires no side effects: mid-stream turn is NOT abandoned, and its answer still lands in the current tab", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    await mockStreamChat(page, {
      conversationId: 42,
      answer: "The answer is 42.",
      messageId: 1,
      userMessageId: 2,
      delayMs: 400,
    });

    await page.goto("/");
    await page.getByPlaceholder("Ask about IPEDS data…").fill("How many?");
    await page.getByRole("button", { name: "Send" }).click();

    // Still streaming -- a modified click on "+ New chat" must early-return
    // with NO side effect (no turnToken bump, no thread reset): the browser's
    // native modified-click handling takes over instead.
    await page.waitForTimeout(100);
    const popupPromise = page.context().waitForEvent("page", { timeout: 2000 }).catch(() => null);
    await page.getByRole("link", { name: "+ New chat" }).click({ modifiers: ["ControlOrMeta"] });
    const popup = await popupPromise;
    if (popup) await popup.close();

    // Let the in-flight turn's `conversation` event (the "/" -> "/chat/42"
    // self-nav) and final answer land. If the modified click had (wrongly)
    // bumped turnToken the way a plain click does, this turn would now be
    // treated as abandoned: the URL would stay "/" and the answer would
    // never render. It must land normally instead.
    await expect.poll(() => new URL(page.url()).pathname).toBe("/chat/42");
    await expect(page.getByText("The answer is 42.")).toBeVisible();
  });

  // Gap note: Playwright's `click({ button: "middle" })` on a plain <a> with
  // no explicit middle-click handler doesn't reliably trigger Chromium's
  // native "open in background tab" behavior the way a real user gesture
  // does in some configurations, and there's no portable way to assert "a
  // new background tab opened with no side effects in THIS tab" beyond what
  // the ControlOrMeta-modifier tests above already cover (same code path:
  // Chat.jsx's handleNewChat only distinguishes "plain click" from "anything
  // else", not modifier-vs-middle specifically). The modifier-click tests
  // above are treated as covering both vectors; a true separate
  // middle-click-only assertion is left as a gap here.
});
