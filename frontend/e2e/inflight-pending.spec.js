import { expect, test } from "@playwright/test";

import {
  gotoAdmin, mockAccessRequests, mockAllowlist, mockAttention, mockConversation,
  mockConversations, mockDeniedRequests, mockMe, mockStreamChat,
  mockStreamChatDripped, mockVersion,
} from "./mocks.js";

// Browser truth for "show me SOMETHING while my answer is still coming".
//
// A turn lives only in the browser until it finishes — the server writes both
// message rows in one transaction at the very END (chat.py `_persist`), so
// mid-flight there is nothing to fetch. Meanwhile navigating clears `messages`
// and bumps `turnToken` so every later view write is dropped (deliberate; see
// midstream-nav.spec.js). The result was that leaving a running question and
// coming back showed the thread as it was BEFORE you asked — and stayed that way
// even after the answer landed, because nothing refetched the open thread.
//
// inflight.js keeps the question alive across that navigation. These tests are
// the browser half; the state machine is unit-tested in src/inflight.test.js.

const USER = { email: "user@example.edu", is_admin: false };

const CONVOS = [
  { id: 3, title: "First chat", updated_at: 2 },
  { id: 5, title: "Second chat", updated_at: 1 },
];

async function base(page) {
  await mockMe(page, USER);
  await mockVersion(page);
  await mockAttention(page);
  await mockConversations(page, CONVOS);
  await mockConversation(page, 3, [
    { id: 1, role: "user", content: "an older question" },
    { id: 2, role: "assistant", content: "an older answer" },
  ]);
  await mockConversation(page, 5, [
    { id: 9, role: "user", content: "over here" },
    { id: 10, role: "assistant", content: "other answer" },
  ]);
}

const ask = async (page, q) => {
  await page.getByPlaceholder("Ask about IPEDS data…").fill(q);
  await page.getByRole("button", { name: "Send" }).click();
};

const pending = (page) => page.getByText("Still working on your question…");

test.describe("a running turn stays visible across navigation", () => {
  test("leaving and returning mid-stream shows the question and a spinner",
    async ({ page }) => {
      // THE HEADLINE. Before this, coming back showed the conversation exactly
      // as it was before you asked, with nothing to say a turn was running.
      await base(page);
      await mockStreamChat(page, {
        conversationId: 3, delayMs: 2500, answer: "The eventual answer.",
        messageId: 42, userMessageId: 41,
      });
      await page.goto("/chat/3");
      await ask(page, "my slow question");

      await page.getByRole("link", { name: /Second chat/ }).click();
      await expect(page.getByText("over here")).toBeVisible();
      await expect(pending(page)).toHaveCount(0);   // never in the wrong chat

      await page.getByRole("link", { name: /First chat/ }).click();
      await expect(page.getByText("an older answer")).toBeVisible();
      await expect(page.getByText("my slow question")).toHaveCount(1);
      await expect(pending(page)).toBeVisible();
    });

  test("the answer replaces the placeholder when it lands, without a reload",
    async ({ page }) => {
      // The other half: before this, nothing refetched the open thread, so a
      // viewer who returned stayed on the stale view INDEFINITELY — even once
      // the answer was on disk.
      await base(page);
      await mockStreamChat(page, {
        conversationId: 3, delayMs: 1200, answer: "The eventual answer.",
        messageId: 42, userMessageId: 41,
      });
      await page.goto("/chat/3");
      await ask(page, "my slow question");
      await page.getByRole("link", { name: /Second chat/ }).click();
      await expect(page.getByText("over here")).toBeVisible();

      // Re-mock conversation 3 as the server will have it once the turn lands.
      await mockConversation(page, 3, [
        { id: 1, role: "user", content: "an older question" },
        { id: 2, role: "assistant", content: "an older answer" },
        { id: 41, role: "user", content: "my slow question" },
        { id: 42, role: "assistant", content: "The eventual answer." },
      ]);
      await page.getByRole("link", { name: /First chat/ }).click();
      await expect(pending(page)).toBeVisible();

      await expect(page.getByText("The eventual answer.")).toBeVisible({ timeout: 5000 });
      await expect(pending(page)).toHaveCount(0);
      // Exactly one copy — the placeholder must be superseded, not joined.
      await expect(page.getByText("my slow question")).toHaveCount(1);
      // Not busy — Send is back in place of Stop. (Send itself is disabled
      // whenever the composer is empty, so its enabled-ness says nothing here.)
      await expect(page.getByRole("button", { name: "Stop generating" })).toHaveCount(0);
      await expect(page.getByRole("button", { name: "Send" })).toBeVisible();
    });

  test("the owner watching its own turn sees no duplicate and triggers no reload",
    async ({ page }) => {
      // The anti-double-render. The owning view already draws its own pending
      // pair, so the placeholder must stay suppressed while it does — otherwise
      // the question appears twice for the entire duration of the turn.
      //
      // Asserted DURING the stream, which is the only window where it can be
      // wrong: once the turn settles the entry is deleted, so a late assertion
      // passes with the filter removed. (Verified — the first version of this
      // test did exactly that.)
      await base(page);
      const conv3 = await mockConversation(page, 3, [
        { id: 1, role: "user", content: "an older question" },
        { id: 2, role: "assistant", content: "an older answer" },
      ]);
      await mockStreamChat(page, {
        conversationId: 3, delayMs: 1500, answer: "The eventual answer.",
        messageId: 42, userMessageId: 41,
      });
      await page.goto("/chat/3");
      await expect(page.getByText("an older answer")).toBeVisible();
      const loadsBefore = conv3.calls;

      await ask(page, "my slow question");
      await expect(page.getByRole("button", { name: "Stop generating" })).toBeVisible();
      // Settle before counting. toHaveCount(1) passes on its FIRST poll, so
      // asserting immediately races the store emit and goes green even while a
      // duplicate is about to appear — verified: without this wait the test
      // passed with the filter removed.
      await page.waitForTimeout(400);
      // Counted SYNCHRONOUSLY, not with toHaveCount. Playwright's matchers
      // auto-retry, so `toHaveCount(1)` against a 1.5s stream simply waits the
      // turn out: the entry settles, the duplicate disappears, and the
      // assertion goes green having never seen the bug. Verified — that is
      // exactly how the first two versions of this test passed with the
      // anti-double-render filter deleted. A retrying matcher cannot assert
      // "this is not true RIGHT NOW".
      expect(await page.getByText("my slow question").count()).toBe(1);
      expect(await pending(page).count()).toBe(0);

      await expect(page.getByText("The eventual answer.")).toBeVisible({ timeout: 5000 });
      await expect(page.getByText("my slow question")).toHaveCount(1);
      // A turn the owner rendered must not refetch its own conversation — the
      // same contract midstream-nav pins as conv7.calls === 0.
      expect(conv3.calls).toBe(loadsBefore);
    });

  test("a STOPPED turn keeps its note; the answer is not yanked in under you",
    async ({ page }) => {
      // Stop is abandon-and-drain: the server still saves the answer, but the
      // user deliberately stopped watching. Pulling the finished answer in under
      // them is the same yank the scroll containment exists to prevent.
      //
      // The viewer STAYS PUT — that is the whole scenario. (Navigating away and
      // back is a normal load and legitimately shows whatever the server now
      // has, including the answer; asserting otherwise there tests nothing.)
      await base(page);
      await mockStreamChat(page, {
        conversationId: 3, delayMs: 800, answer: "The eventual answer.",
        messageId: 42, userMessageId: 41,
      });
      await page.goto("/chat/3");
      await ask(page, "my slow question");
      await page.getByRole("button", { name: "Stop generating" }).click();
      await expect(page.getByText(/^Stopped\./)).toBeVisible();

      // The server DOES persist it, so a reload WOULD pull it in. Without this
      // the fixture cannot reveal the yank: a refetch would return the same old
      // thread and this test stays green with the bug fully present — verified
      // by deleting hideTurn.
      await mockConversation(page, 3, [
        { id: 1, role: "user", content: "an older question" },
        { id: 2, role: "assistant", content: "an older answer" },
        { id: 41, role: "user", content: "my slow question" },
        { id: 42, role: "assistant", content: "The eventual answer." },
      ]);
      await page.waitForTimeout(1400);   // let the abandoned stream drain + settle

      await expect(page.getByText(/^Stopped\./)).toBeVisible();
      await expect(page.getByText("The eventual answer.")).toHaveCount(0);
      await expect(pending(page)).toHaveCount(0);
    });

  test("survives Chat unmounting entirely (Admin and back)", async ({ page }) => {
    // An ADMIN, because gotoAdmin goes through the account menu's Admin item —
    // which only exists for one.
    // THE reason the registry is module-level rather than React state. Chat
    // unmounts when Admin takes over the main content, so component state cannot
    // carry the turn across exactly the navigation this feature exists for.
    // SPA navigation only — a page.goto() would be a real load and wipe it.
    await base(page);
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockAllowlist(page, []);
    await mockAccessRequests(page, []);
    await mockDeniedRequests(page, []);
    await mockStreamChat(page, {
      conversationId: 3, delayMs: 3000, answer: "The eventual answer.",
      messageId: 42, userMessageId: 41,
    });
    await page.goto("/chat/3");
    await ask(page, "my slow question");

    await gotoAdmin(page);
    await expect(page.getByRole("heading", { name: /Users/i }).first()).toBeVisible();

    await page.getByRole("link", { name: /IPEDS Oracle, go to chat/i }).click();
    await page.getByRole("link", { name: /First chat/ }).click();
    await expect(page.getByText("my slow question")).toHaveCount(1);
    await expect(pending(page)).toBeVisible();
  });

  test("an empty conversation shows the pending question, not the greeting",
    async ({ page }) => {
      // A conversation whose only turn is still running loads as [] from the
      // server, so the empty state would otherwise render BESIDE the question.
      await base(page);
      await mockConversation(page, 5, []);
      await mockStreamChat(page, {
        conversationId: 5, delayMs: 2500, answer: "Later.", messageId: 2, userMessageId: 1,
      });
      await page.goto("/chat/5");
      await ask(page, "first question here");
      await page.getByRole("link", { name: /First chat/ }).click();
      await expect(page.getByText("an older answer")).toBeVisible();
      await page.getByRole("link", { name: /Second chat/ }).click();

      await expect(pending(page)).toBeVisible();
      await expect(page.getByText("What would you like to know")).toHaveCount(0);
    });
});

test.describe("a brand-new chat's first turn", () => {
  // Needs the DRIPPING mock: a new chat learns its conversation id only from the
  // server's `conversation` event, and the one-shot mock delivers nothing until
  // the turn is already over — so with it, this case is untestable by
  // construction rather than merely untested.
  test("appears in the sidebar while it runs, and shows its question on return",
    async ({ page }) => {
      await mockMe(page, USER);
      await mockVersion(page);
      await mockAttention(page);
      await mockConversations(page, []);
      await mockStreamChatDripped(page, [
        { atMs: 150, event: { type: "conversation", id: 77 } },
        { atMs: 2500, event: { type: "answer", text: "The eventual answer." } },
        { atMs: 2550, event: { type: "done", message_id: 2, user_message_id: 1 } },
      ]);
      await page.goto("/");
      await ask(page, "my brand new question");

      // The conversation event lands -> the row is already on the server (it is
      // created at the top of the generator, titled from the question), so a
      // re-list puts it in the sidebar while the turn is still running. Without
      // that there is nothing to navigate back TO.
      await mockConversations(page, [{ id: 77, title: "my brand new question", updated_at: 9 }]);
      await mockConversation(page, 77, []);
      await expect(page.getByRole("link", { name: /my brand new question/ }))
        .toBeVisible({ timeout: 4000 });

      // Leave and come back while it is still streaming.
      await page.getByRole("link", { name: "New chat" }).click({ force: true });
      await page.getByRole("link", { name: /my brand new question/ }).click();
      await expect(pending(page)).toBeVisible();
      await expect(page.getByText("my brand new question")).toHaveCount(2); // sidebar + bubble
    });
});

test.describe("refreshing mid-turn is guarded", () => {
  test("beforeunload is armed while a turn runs and disarmed when none is",
    async ({ page }) => {
      // A refresh is not navigation: it tears the request down, the server
      // generator is cancelled, _persist never runs, and for a new chat the row
      // is deleted outright. The question and answer are simply gone.
      //
      // Dispatching the event tests what we wrote — a listener that calls
      // preventDefault — deterministically. Driving Chromium's real dialog would
      // mostly be testing the browser.
      const armed = () => page.evaluate(() =>
        !globalThis.dispatchEvent(new Event("beforeunload", { cancelable: true })));

      await base(page);
      await mockStreamChat(page, {
        conversationId: 3, delayMs: 2000, answer: "The eventual answer.",
        messageId: 42, userMessageId: 41,
      });
      await page.goto("/chat/3");
      await expect(page.getByText("an older answer")).toBeVisible();
      // The NEGATIVE case first, and it is the one that matters day to day: an
      // always-armed guard nags on every reload and gets muscle-memoried away.
      expect(await armed()).toBe(false);

      await ask(page, "my slow question");
      expect(await armed()).toBe(true);

      await expect(page.getByText("The eventual answer.")).toBeVisible({ timeout: 6000 });
      expect(await armed()).toBe(false);
    });
});
