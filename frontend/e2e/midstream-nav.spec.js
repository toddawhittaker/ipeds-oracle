import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockConversation,
  mockStreamChat,
  mockAllowlist,
  mockAccessRequests,
  mockDeniedRequests,
} from "./mocks.js";

// Pins the mid-stream navigation fix (architect's design, relayed via the
// project-manager):
//
//   The SSE stream must keep DRAINING to completion when the user navigates
//   away mid-stream (the client never aborts the fetch -- an abort would
//   cancel the server generator and LOSE the answer). Only the CLIENT's
//   rendering is gated behind a per-turn token (Chat.jsx's `turnToken`) so a
//   stale turn's view-state writes can't bleed into whatever conversation the
//   user is now looking at. `refreshConvos`/sidebar-title updates stay live
//   regardless. The CURRENT turn's own success (including a brand-new
//   conversation's self-navigating `/` -> `/chat/:id`) must still render its
//   own answer + SQL/Thoughts trace -- a naive "gate everything" fix would
//   break that happy path, which is why tests 4/5 below are regression
//   guards, not just the new behavior.
//
// Written BEFORE the fix exists (TDD) -- every test here is expected RED
// against the current Chat.jsx, for the reasons noted at each assertion.
//
// mockStreamChat (mocks.js) fulfills the WHOLE SSE body as one chunk after
// `delayMs`, so "mid-stream" here means: the request is in-flight during
// `delayMs`, and the whole burst (conversation event + answer) lands AFTER
// the simulated navigation -- exactly the ordering that exercises the bleed.
// Tests 2 and 5 need per-conversation-id control that the shared
// mockStreamChat helper doesn't offer (it always answers with one fixed
// conversationId/answer/delay), so they hand-roll page.route(...) keyed on
// the request's conversation_id, mirroring routing-chat.spec.js's "orphaned
// conversation" regression test (~line 361).

test.describe("mid-stream navigation", () => {
  test("navigating away mid-stream then back: the answer is persisted server-side and does not bleed into the conversation navigated to", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [{ id: 3, title: "A" }, { id: 5, title: "B" }]);
    await mockConversation(page, 3, [
      { role: "user", content: "first A question" },
      { role: "assistant", content: "first A answer" },
    ]);
    await mockConversation(page, 5, [
      { role: "user", content: "old B question" },
      { role: "assistant", content: "old B answer" },
    ]);
    await mockStreamChat(page, { conversationId: 3, answer: "ANSWER-A", delayMs: 600 });

    await page.goto("/chat/3");
    await expect(page.getByText("first A answer")).toBeVisible();

    await page.getByPlaceholder("Ask about IPEDS data…").fill("Q-A");
    await page.getByRole("button", { name: "Send" }).click();

    // Still streaming (mocked with a 600ms delay) -- navigate away, same as a
    // user who changes their mind mid-answer.
    await page.waitForTimeout(150);
    await page.getByRole("link", { name: "B", exact: true }).click();

    await expect.poll(() => new URL(page.url()).pathname).toBe("/chat/5");
    await expect(page.getByText("old B answer")).toBeVisible();

    // Let A's delayed stream land while the user is looking at B.
    await page.waitForTimeout(700);

    // No bleed: A's answer must not appear on B's screen, and the URL must
    // not have been yanked back to /chat/3 by the stale `conversation` event.
    // FAILS TODAY: submit()'s final setMessages (which writes ANSWER-A into
    // whatever `messages` array is on screen right now) is completely
    // ungated by turnToken -- only the `conversation` event's
    // navigate/setOpenId side effects are gated.
    await expect(page.getByText("ANSWER-A")).toHaveCount(0);
    expect(new URL(page.url()).pathname).toBe("/chat/5");

    // ...but the answer WAS persisted server-side (the stream kept draining
    // rather than aborting) -- reopening A shows it.
    await mockConversation(page, 3, [
      { role: "user", content: "first A question" },
      { role: "assistant", content: "first A answer" },
      { role: "user", content: "Q-A" },
      { role: "assistant", content: "ANSWER-A" },
    ]);
    await page.getByRole("link", { name: "A", exact: true }).click();
    await expect.poll(() => new URL(page.url()).pathname).toBe("/chat/3");
    await expect(page.getByText("ANSWER-A")).toBeVisible();
  });

  test("a second question submitted from a DIFFERENT conversation mid-stream does not cross-bleed either direction", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [{ id: 3, title: "A" }, { id: 5, title: "B" }]);
    await mockConversation(page, 3, [
      { role: "user", content: "first A question" },
      { role: "assistant", content: "first A answer" },
    ]);
    await mockConversation(page, 5, [
      { role: "user", content: "first B question" },
      { role: "assistant", content: "first B answer" },
    ]);

    // Hand-rolled: conversation 3's turn answers slowly ("ANSWER-A", 800ms);
    // conversation 5's turn answers immediately ("ANSWER-B"). Mirrors the
    // real backend's per-request semantics, which a single fixed
    // mockStreamChat() call can't express.
    await page.route("**/api/chat/stream", async (route) => {
      const body = route.request().postDataJSON();
      const convId = body.conversation_id;
      const delayMs = convId === 3 ? 800 : 0;
      const answer = convId === 3 ? "ANSWER-A" : "ANSWER-B";
      if (delayMs > 0) await new Promise((r) => setTimeout(r, delayMs));
      const events = [
        { type: "conversation", id: convId },
        { type: "status", text: "Thinking…" },
        { type: "answer", text: answer },
        { type: "done", message_id: null, user_message_id: null, model: "test", tokens: 0 },
      ];
      const sseBody = events.map((e) => `data: ${JSON.stringify(e)}`).join("\n\n") + "\n\n";
      await route.fulfill({ status: 200, contentType: "text/event-stream", body: sseBody });
    });

    await page.goto("/chat/3");
    await page.getByPlaceholder("Ask about IPEDS data…").fill("Q-A");
    await page.getByRole("button", { name: "Send" }).click();

    await page.waitForTimeout(150); // A is still in flight (800ms delay)
    await page.getByRole("link", { name: "B", exact: true }).click();
    await expect.poll(() => new URL(page.url()).pathname).toBe("/chat/5");

    await page.getByPlaceholder("Ask about IPEDS data…").fill("Q-B");
    await page.getByRole("button", { name: "Send" }).click();

    // B's own (undelayed) turn renders promptly.
    await expect(page.getByText("ANSWER-B")).toBeVisible();

    // Let A's delayed stream land while B's turn is already showing.
    await page.waitForTimeout(800);

    // FAILS TODAY: A's stale final setMessages patches "the last message" of
    // whatever `messages` array is in memory at that moment -- which by now
    // is B's thread with B's own pending/finished assistant message in that
    // slot -- clobbering ANSWER-B with ANSWER-A instead of leaving it alone.
    await expect(page.getByText("ANSWER-A")).toHaveCount(0);
    await expect(page.getByText("ANSWER-B")).toBeVisible();
    expect(new URL(page.url()).pathname).toBe("/chat/5");
  });

  test("navigating Chat -> Admin -> Chat mid-stream: the abandoned stream keeps draining and its answer survives", async ({ page }) => {
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockConversations(page, [{ id: 3, title: "A" }]);
    await mockConversation(page, 3, [
      { role: "user", content: "first A question" },
      { role: "assistant", content: "first A answer" },
    ]);
    await mockStreamChat(page, { conversationId: 3, answer: "ANSWER-A", delayMs: 600 });
    // Admin's default tab (Users) needs these to render without erroring.
    await mockAllowlist(page, []);
    await mockAccessRequests(page, []);
    await mockDeniedRequests(page, []);

    await page.goto("/chat/3");
    await page.getByPlaceholder("Ask about IPEDS data…").fill("Q-A");
    await page.getByRole("button", { name: "Send" }).click();

    // Still streaming -- navigate to Admin. Chat unmounts entirely; the
    // in-flight stream (per the design) must keep draining in the
    // background rather than being aborted.
    await page.waitForTimeout(150);
    await page.getByRole("link", { name: "Admin", exact: true }).click();
    await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/users");

    // Let the abandoned stream finish landing while Chat is unmounted.
    await page.waitForTimeout(700);

    // The answer is now persisted server-side -- a fresh Chat mount shows it.
    await mockConversation(page, 3, [
      { role: "user", content: "first A question" },
      { role: "assistant", content: "first A answer" },
      { role: "user", content: "Q-A" },
      { role: "assistant", content: "ANSWER-A" },
    ]);
    await page.goto("/chat/3");

    // FAILS TODAY (mildly): this mostly proves the unmounted stale stream
    // doesn't wedge the app -- the real risk this guards is a future
    // implementation that GATES BY ABORTING the fetch/stream reader on
    // unmount, which would lose the answer entirely (it would never get
    // persisted, so this reopen would show nothing new).
    await expect(page.getByText("ANSWER-A")).toBeVisible();
    await expect(page.getByText(/⚠️/)).toHaveCount(0);
    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeVisible();
    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeEnabled();
  });

  // REGRESSION guard: a naive "suppress every view-state write once stale"
  // gate could over-fire and also suppress the CURRENT turn's own writes
  // (status/sql/thinking), since a brand-new conversation's `conversation`
  // event self-navigates "/" -> "/chat/:id" -- which bumps routeId and,
  // under a careless implementation, could look indistinguishable from the
  // user having navigated away. This complements (does not duplicate)
  // routing-chat.spec.js's "brand-new chat's stream survives the URL flip"
  // test (~line 86), which only asserts the bare answer text and conv7.calls
  // === 0 -- this additionally pins that the SQL/Thoughts trace, which is
  // written via the SAME gated code path, still renders for the turn that
  // owns it.
  test("REGRESSION: a brand-new conversation's own stream still renders its own answer + SQL/Thoughts trace", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);
    await mockStreamChat(page, { conversationId: 7, sql: ["SELECT 1"], answer: "The answer is 42." });
    const conv7 = await mockConversation(page, 7, []); // must never be called -- asserted below

    await page.goto("/");
    await page.getByPlaceholder("Ask about IPEDS data…").fill("How many?");
    await page.getByRole("button", { name: "Send" }).click();

    await expect.poll(() => new URL(page.url()).pathname).toBe("/chat/7");
    await expect(page.getByText("The answer is 42.")).toBeVisible();

    await page.waitForTimeout(200);
    expect(conv7.calls).toBe(0);

    // The trace for THIS turn must still be there -- proves the gate doesn't
    // blanket-suppress the turn that actually owns the current view.
    // Chat.jsx gives BOTH the "Thinking" trace and the "SQL" log the SAME
    // `details.sql` class -- scope by the summary text each carries so the
    // two don't collide under strict mode.
    const sqlDetails = page.locator("details.sql").filter({ hasText: "SQL" });
    const thinkingDetails = page.locator("details.sql").filter({ hasText: "Thinking" });
    await expect(sqlDetails.getByText("SQL", { exact: true })).toBeVisible();
    await sqlDetails.locator("summary").click();
    await expect(sqlDetails.getByText("SELECT 1")).toBeVisible();
    await expect(thinkingDetails.getByText("Thinking", { exact: true })).toBeVisible();
  });

  // REGRESSION guard: `busy` is a single shared piece of Chat state, not
  // per-turn. A gate that only suppresses submit()'s FINAL setMessages/
  // setBusy(false) call (rather than resetting busy the moment the route
  // actually changes) leaves the composer disabled on the conversation the
  // user navigated TO, until the abandoned turn's stream finally resolves in
  // the background -- stranding the user who was told they could walk away.
  test("REGRESSION: the composer is not left disabled ('busy stranded') after navigating away mid-stream", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, [{ id: 3, title: "A" }, { id: 5, title: "B" }]);
    await mockConversation(page, 3, [
      { role: "user", content: "first A question" },
      { role: "assistant", content: "first A answer" },
    ]);
    await mockConversation(page, 5, [
      { role: "user", content: "first B question" },
      { role: "assistant", content: "first B answer" },
    ]);

    // Hand-rolled so a follow-up submit from B (optional final assertion
    // below) gets its own quick, distinct answer rather than colliding with
    // A's long-delayed one.
    await page.route("**/api/chat/stream", async (route) => {
      const body = route.request().postDataJSON();
      const convId = body.conversation_id;
      const delayMs = convId === 3 ? 1500 : 0;
      const answer = convId === 3 ? "ANSWER-A" : "ANSWER-B2";
      if (delayMs > 0) await new Promise((r) => setTimeout(r, delayMs));
      const events = [
        { type: "conversation", id: convId },
        { type: "status", text: "Thinking…" },
        { type: "answer", text: answer },
        { type: "done", message_id: null, user_message_id: null, model: "test", tokens: 0 },
      ];
      const sseBody = events.map((e) => `data: ${JSON.stringify(e)}`).join("\n\n") + "\n\n";
      await route.fulfill({ status: 200, contentType: "text/event-stream", body: sseBody });
    });

    await page.goto("/chat/3");
    await page.getByPlaceholder("Ask about IPEDS data…").fill("Q-A");
    await page.getByRole("button", { name: "Send" }).click();

    await page.waitForTimeout(150);
    await page.getByRole("link", { name: "B", exact: true }).click();
    await expect.poll(() => new URL(page.url()).pathname).toBe("/chat/5");

    await page.getByPlaceholder("Ask about IPEDS data…").fill("Q-B");
    // FAILS TODAY: `busy` was set true by A's submit() and is only reset by
    // that same submit()'s (ungated) tail -- which doesn't run until A's
    // 1500ms stream resolves -- so Send stays disabled here well before that.
    await expect(page.getByRole("button", { name: "Send" })).toBeEnabled({ timeout: 1000 });

    // And a submission from B actually goes through once busy is genuinely free.
    await page.getByRole("button", { name: "Send" }).click();
    await expect(page.getByText("ANSWER-B2")).toBeVisible();
  });
});
