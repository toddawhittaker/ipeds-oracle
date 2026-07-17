import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockConversation,
  mockDeleteConversation,
  mockStreamChat,
  mockStreamChatNoConversation,
  mockStreamChatError,
} from "./mocks.js";

// Field name streamChat() actually sends -- see web/src/api.js:
// `conversation_id` (snake_case), never the camelCase `conversationId` used
// on the JS call site. Assertions below read the raw POST body, so this
// constant keeps the field name from drifting silently out of sync.
const CONVERSATION_ID_FIELD = "conversation_id";

// Client-side routing (react-router-dom v6) for Chat. URLs mirror the open
// conversation so a chat is shareable/bookmarkable and the browser's Back/
// Forward buttons behave like a normal multi-page app:
//   /            -> Chat, empty new-chat screen
//   /chat/:id    -> Chat with that conversation loaded
// See CLAUDE.md / the SPA-routing spec this suite pins. Written before the
// feature exists (TDD) -- these specs are expected to be RED against the
// current (routerless) App.jsx.

test.describe("chat routing", () => {
  test("/ renders the empty new-chat screen and the URL stays /", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);

    await page.goto("/");

    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeVisible();
    expect(new URL(page.url()).pathname).toBe("/");
  });

  test("clicking a sidebar conversation pushes /chat/:id, loads its messages, and marks the row aria-current", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [{ id: 3, title: "CA nursing associate's degrees" }]);
    await mockConversation(page, 3, [
      { role: "user", content: "CA nursing associate's degrees" },
      { role: "assistant", content: "Here you go." },
    ]);

    await page.goto("/");
    const row = page.getByRole("button", { name: "CA nursing associate's degrees" });
    await row.click();

    await expect.poll(() => new URL(page.url()).pathname).toBe("/chat/3");
    await expect(page.getByText("Here you go.")).toBeVisible();
    await expect(row).toHaveAttribute("aria-current", "page");
  });

  test("deep-linking /chat/:id loads its messages with the sidebar intact", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [
      { id: 3, title: "CA nursing associate's degrees" },
      { id: 5, title: "Other chat" },
    ]);
    await mockConversation(page, 3, [
      { role: "user", content: "CA nursing associate's degrees" },
      { role: "assistant", content: "Here you go." },
    ]);

    await page.goto("/chat/3");

    await expect(page.getByText("Here you go.")).toBeVisible();
    await expect(page.getByRole("button", { name: "CA nursing associate's degrees" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Other chat" })).toBeVisible();
    await expect(page.getByRole("button", { name: "+ New chat" })).toBeVisible();
  });

  // Regression guard: a naive "fetch the conversation whenever :id changes"
  // loader effect would fire the instant the SSE `conversation` event flips
  // the URL to /chat/7, and (since the real fetch races the in-memory stream
  // state) could wipe the just-streamed answer off the screen. The URL flip
  // must be purely cosmetic for a conversation the client already has fully
  // in memory.
  test("a brand-new chat's stream survives the URL flip to /chat/:id (no refetch, answer stays visible)", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    await mockStreamChat(page, { conversationId: 7, answer: "The answer is 42.", messageId: 1, userMessageId: 2 });
    const conv7 = await mockConversation(page, 7, []); // must NEVER be called -- asserted below

    await page.goto("/");
    await page.getByPlaceholder("Ask about IPEDS data…").fill("How many?");
    await page.getByRole("button", { name: "Send" }).click();

    await expect.poll(() => new URL(page.url()).pathname).toBe("/chat/7");
    await expect(page.getByText("The answer is 42.")).toBeVisible();

    await page.waitForTimeout(200); // let any errant fetch land before asserting zero
    expect(conv7.calls).toBe(0);
  });

  test("the post-stream URL is a history REPLACE, not a push (Back does not return to /)", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    await mockStreamChat(page, { conversationId: 7, answer: "The answer is 42.", messageId: 1, userMessageId: 2 });
    await mockConversation(page, 7, []);

    await page.goto("/");
    await page.getByPlaceholder("Ask about IPEDS data…").fill("How many?");
    await page.getByRole("button", { name: "Send" }).click();
    await expect.poll(() => new URL(page.url()).pathname).toBe("/chat/7");

    await page.goBack();
    // If the flip had PUSHed a new history entry, Back would land squarely
    // back on "/". A REPLACE leaves no "/" entry to return to.
    expect(new URL(page.url()).pathname).not.toBe("/");
  });

  test("'+ New chat' pushes / (empty), and Back reloads the previous /chat/:id", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [{ id: 3, title: "CA nursing associate's degrees" }]);
    await mockConversation(page, 3, [
      { role: "user", content: "CA nursing associate's degrees" },
      { role: "assistant", content: "Here you go." },
    ]);

    await page.goto("/chat/3");
    await expect(page.getByText("Here you go.")).toBeVisible();

    await page.getByRole("button", { name: "+ New chat" }).click();
    await expect.poll(() => new URL(page.url()).pathname).toBe("/");
    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeVisible();
    await expect(page.getByText("Here you go.")).toHaveCount(0);

    await page.goBack();
    await expect.poll(() => new URL(page.url()).pathname).toBe("/chat/3");
    await expect(page.getByText("Here you go.")).toBeVisible();
  });

  // Code-review HIGH: newChat() is just navigate("/"). When the URL is
  // ALREADY "/", routeId stays null, the render-time reset (openId !==
  // routeId) never fires, and whatever's on screen stays there. Reachable
  // whenever a submit's stream never carries a `conversation` event: the
  // backend's has_data:false guard (app/routers/chat.py) streams only
  // answer+done, so the URL never leaves "/" in the first place.
  test("'+ New chat' clears the thread when the URL was already / (stream had no conversation event)", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false, has_data: false });
    await mockConversations(page, []);
    await mockStreamChatNoConversation(page, { answer: "Here's what I can tell you without data." });

    await page.goto("/");
    // has_data:false does not disable the composer -- the question can still
    // be submitted, it just never gets a conversation id back.
    await page.getByPlaceholder("Ask about IPEDS data…").fill("How many?");
    await page.getByRole("button", { name: "Send" }).click();

    await expect(page.getByText("Here's what I can tell you without data.")).toBeVisible();
    expect(new URL(page.url()).pathname).toBe("/");

    const historyLenBefore = await page.evaluate(() => globalThis.history.length);
    await page.getByRole("button", { name: "+ New chat" }).click();

    await expect(page.getByText("Here's what I can tell you without data.")).toHaveCount(0);
    expect(new URL(page.url()).pathname).toBe("/");
    // The URL didn't change (was already /), so this must not push a
    // duplicate "/" history entry either -- that would leave Back looking
    // dead (pressing it once lands on another indistinguishable "/").
    const historyLenAfter = await page.evaluate(() => globalThis.history.length);
    expect(historyLenAfter).toBe(historyLenBefore);
  });

  // Same gap, reached via the OTHER path that leaves the URL on / with no
  // conversation event: streamChat() throws (network drop / 500) before any
  // SSE event is ever read.
  test("'+ New chat' clears the thread after a stream error before any conversation event", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    await mockStreamChatError(page, { httpStatus: 500, detail: "boom" });

    await page.goto("/");
    await page.getByPlaceholder("Ask about IPEDS data…").fill("How many?");
    await page.getByRole("button", { name: "Send" }).click();

    await expect(page.getByText(/boom/)).toBeVisible();
    expect(new URL(page.url()).pathname).toBe("/");

    await page.getByRole("button", { name: "+ New chat" }).click();

    await expect(page.getByText(/boom/)).toHaveCount(0);
    expect(new URL(page.url()).pathname).toBe("/");
  });

  test("deleting the currently-open conversation navigates to /", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [{ id: 3, title: "CA nursing associate's degrees" }]);
    await mockConversation(page, 3, [
      { role: "user", content: "CA nursing associate's degrees" },
      { role: "assistant", content: "Here you go." },
    ]);
    const del = await mockDeleteConversation(page);

    await page.goto("/chat/3");
    await expect(page.getByText("Here you go.")).toBeVisible();

    page.once("dialog", (d) => d.accept());
    await page.getByRole("button", { name: "Delete chat" }).click();

    await expect.poll(() => del.calls).toEqual(["3"]);
    await expect.poll(() => new URL(page.url()).pathname).toBe("/");
    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeVisible();
  });

  test("deleting a DIFFERENT conversation leaves the currently-open one's URL and thread untouched", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [
      { id: 3, title: "CA nursing associate's degrees" },
      { id: 5, title: "Other chat" },
    ]);
    await mockConversation(page, 3, [
      { role: "user", content: "CA nursing associate's degrees" },
      { role: "assistant", content: "Here you go." },
    ]);
    const del = await mockDeleteConversation(page);

    await page.goto("/chat/3");
    await expect(page.getByText("Here you go.")).toBeVisible();

    const otherRow = page.locator(".convo-row", { has: page.getByRole("button", { name: "Other chat" }) });
    page.once("dialog", (d) => d.accept());
    await otherRow.getByRole("button", { name: "Delete chat" }).click();

    await expect.poll(() => del.calls).toEqual(["5"]);
    expect(new URL(page.url()).pathname).toBe("/chat/3");
    await expect(page.getByText("Here you go.")).toBeVisible();
  });

  test("/chat/999 (doesn't exist) shows the exact unavailable notice, an empty new-chat screen, and the sidebar -- with no server detail leaked", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [{ id: 3, title: "Some other chat" }]);
    await mockConversation(page, 999, [], { httpStatus: 404, detail: "conversation 999 not found for user 1" });

    await page.goto("/chat/999");

    // Scoped to the visible .notice box specifically: the a11y fix below adds
    // an always-mounted sr-only role="status" region carrying this SAME text,
    // so an unscoped getByText(...) would match two elements and hit
    // Playwright's strict-mode violation.
    await expect(page.locator(".notice")).toHaveText("That conversation isn't available.");
    await expect(page.getByText("conversation 999 not found for user 1")).toHaveCount(0);
    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeVisible();
    await expect(page.getByRole("button", { name: "Some other chat" })).toBeVisible();
    // The bad-id URL itself isn't wiped out from under the user.
    expect(new URL(page.url()).pathname).toBe("/chat/999");
  });

  // No enumeration oracle: a conversation that exists but belongs to someone
  // else must read IDENTICALLY to one that doesn't exist at all.
  test("/chat/55 (not yours, 403) shows the IDENTICAL notice text as a 404 -- no enumeration oracle", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    await mockConversation(page, 55, [], { httpStatus: 403, detail: "forbidden: not your conversation" });

    await page.goto("/chat/55");

    await expect(page.locator(".notice")).toHaveText("That conversation isn't available.");
    await expect(page.getByText(/forbidden/i)).toHaveCount(0);
  });

  test("/chat/abc (non-numeric) shows the IDENTICAL notice with NO network request at all", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    const convAbc = await mockConversation(page, "abc", []);

    await page.goto("/chat/abc");

    await expect(page.locator(".notice")).toHaveText("That conversation isn't available.");
    await page.waitForTimeout(200);
    expect(convAbc.calls).toBe(0);
  });

  // A11y (WCAG 4.1.3, code review HIGH): the bad-conversation notice must
  // announce to screen readers. Two paths were broken: /chat/abc computes
  // showNotice DURING RENDER, so a role="status" node existed at FIRST PAINT
  // (an already-populated live region doesn't announce); /chat/999 set it in
  // an async .catch, so the role="status" node MOUNTED FRESH (a brand-new
  // node isn't a reliably-announced shape either -- the same class of bug
  // already fixed for Admin.jsx's flash box, see Admin.jsx:249-256). The fix
  // is that same house pattern: an ALWAYS-MOUNTED `.sr-only[role=status]`
  // region, populated via an effect, with role="status" removed from the
  // visible `.notice`.
  test("the bad-conversation notice's live region is always mounted and starts empty on a normal chat load", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);

    await page.goto("/");

    const live = page.locator('[role="status"].sr-only');
    await expect(live).toBeAttached();
    await expect(live).toHaveText("");
  });

  test("/chat/999 (404) populates the always-mounted live region, and the visible .notice no longer carries role=status", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    await mockConversation(page, 999, [], { httpStatus: 404, detail: "conversation 999 not found for user 1" });

    await page.goto("/chat/999");

    const live = page.locator('[role="status"].sr-only');
    await expect(live).toHaveText("That conversation isn't available.");
    await expect(live).toHaveAttribute("aria-live", "polite");
    // Exactly one status region belonging to this notice (visible .notice +
    // its sr-only counterpart) -- scoped to <main> so a page-wide route
    // announcer elsewhere in the app (a separate a11y fix) can't make this
    // assertion collide with unrelated status regions.
    await expect(page.locator("main").getByRole("status")).toHaveCount(1);
    await expect(page.locator(".notice")).not.toHaveAttribute("role", "status");
  });

  test("/chat/abc (non-numeric) populates the always-mounted live region the same way (the synchronous, render-time path)", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);

    await page.goto("/chat/abc");

    const live = page.locator('[role="status"].sr-only');
    await expect(live).toHaveText("That conversation isn't available.");
    await expect(page.locator("main").getByRole("status")).toHaveCount(1);
    await expect(page.locator(".notice")).not.toHaveAttribute("role", "status");
  });

  // Code-review BLOCKER: `convId` (Chat.jsx) is gated on `!notice`:
  //   const convId = routeId !== null && !badFormat && !notice ? Number(routeId) : null;
  // `notice` is normally cleared only by the render-time reset
  // (openId !== routeId), but the `conversation` SSE event handler
  // deliberately SETS openId itself (to keep the just-streamed answer from
  // being wiped -- see the "brand-new chat's stream survives the URL flip"
  // test above), so that reset never fires. Starting from a bad /chat/:id
  // (a 404), `notice` therefore survives forever: the stale "isn't
  // available" banner never clears, convId is pinned null on every future
  // turn, and every submission -- including follow-ups on what LOOKS like an
  // established conversation -- mints a brand-new conversation row
  // server-side, silently orphaning the previous one. The sidebar highlight
  // and delete-the-open-chat affordance (both keyed on `c.id === convId`)
  // are dead the same way.
  test("a chat started from a bad /chat/:id URL does not orphan a new conversation on every turn", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    const convos = await mockConversations(page, []);
    await mockConversation(page, 999, [], { httpStatus: 404, detail: "conversation 999 not found for user 1" });

    // Hand-rolled (rather than mockStreamChat) so it can mirror the real
    // backend's actual semantics: a null conversation_id always mints a
    // brand-new conversation id; a non-null one continues that same
    // conversation. This is exactly the distinction the bug erases.
    const calls = [];
    let nextId = 42;
    await page.route("**/api/chat/stream", async (route) => {
      const body = route.request().postDataJSON();
      calls.push(body);
      const convId = body[CONVERSATION_ID_FIELD] ?? nextId++;
      const events = [
        { type: "conversation", id: convId },
        { type: "status", text: "Thinking…" },
        { type: "answer", text: `Answer #${calls.length}.` },
        { type: "done", message_id: calls.length, user_message_id: calls.length + 100,
          model: "test", tokens: 0 },
      ];
      const sseBody = events.map((e) => `data: ${JSON.stringify(e)}`).join("\n\n") + "\n\n";
      await route.fulfill({ status: 200, contentType: "text/event-stream", body: sseBody });
    });

    await page.goto("/chat/999");
    await expect(page.locator(".notice")).toHaveText("That conversation isn't available.");

    await page.getByPlaceholder("Ask about IPEDS data…").fill("How many?");
    convos.setList([{ id: 42, title: "How many?" }]); // reflected by refreshConvos() after the turn
    await page.getByRole("button", { name: "Send" }).click();

    await expect.poll(() => new URL(page.url()).pathname).toBe("/chat/42");
    await expect(page.getByText("Answer #1.")).toBeVisible();

    // The stale 404 notice must disappear once a real, live conversation has
    // landed -- it must not float above the thread forever.
    await expect(page.locator(".notice")).toHaveCount(0);

    // Follow-up turn on the now-open conversation.
    await page.getByPlaceholder("Ask about IPEDS data…").fill("And more?");
    await page.getByRole("button", { name: "Send" }).click();
    await expect(page.getByText("Answer #2.")).toBeVisible();

    // The follow-up's request must carry the FIRST turn's conversation id --
    // not null (which would mint yet another brand-new conversation and
    // silently orphan #42).
    expect(calls).toHaveLength(2);
    expect(calls[1][CONVERSATION_ID_FIELD]).toBe(42);

    // ...and the URL must therefore stay put on /chat/42, not walk to /chat/43.
    expect(new URL(page.url()).pathname).toBe("/chat/42");

    // Sidebar highlight (c.id === convId) must recognize the open conversation.
    await expect(page.getByRole("button", { name: "How many?", exact: true }))
      .toHaveAttribute("aria-current", "page");
  });

  // Code-review MEDIUM: newChat() has no busy guard. Clicking "+ New chat"
  // while a turn is still streaming clears the local thread immediately (the
  // routeId===null branch), but the in-flight stream's `conversation` event
  // still lands later and unconditionally does `setOpenId` + `navigate`,
  // yanking the user onto /chat/:id showing NOTHING -- and it's STICKY,
  // because that same handler sets `loadedFor.current` to the new id first,
  // so neither the loader effect nor the render-time reset ever populates it.
  // Only a hard reload recovers the (server-side intact) conversation. The
  // fix is left to the implementer; this only pins the observable: the user
  // must not be moved off "/" (or shown an empty thread) by a stream they
  // already walked away from.
  test("'+ New chat' mid-stream does not teleport the user to an empty /chat/:id once the stream's conversation event lands", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    await mockStreamChat(page, {
      conversationId: 42,
      answer: "The answer is 42.",
      messageId: 1,
      userMessageId: 2,
      delayMs: 600,
    });

    await page.goto("/");
    await page.getByPlaceholder("Ask about IPEDS data…").fill("How many?");
    await page.getByRole("button", { name: "Send" }).click();

    // The stream is still in flight (mocked with a 600ms delay) -- navigate
    // away before it resolves, same as a user who changes their mind.
    await page.waitForTimeout(150);
    expect(new URL(page.url()).pathname).toBe("/");
    await page.getByRole("button", { name: "+ New chat" }).click({ force: true });

    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeVisible();
    expect(new URL(page.url()).pathname).toBe("/");
    await expect(page.locator(".msg")).toHaveCount(0);

    // Let the in-flight stream's `conversation` event actually land.
    await page.waitForTimeout(700);

    // Must NOT have been yanked to /chat/42 showing an empty thread.
    expect(new URL(page.url()).pathname).toBe("/");
    await expect(page.locator(".msg")).toHaveCount(0);
    await expect(page.getByText("The answer is 42.")).toHaveCount(0);
  });
});
