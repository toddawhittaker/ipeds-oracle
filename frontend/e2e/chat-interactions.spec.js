import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockConversation,
  mockRenameConversation,
  mockStreamChat,
} from "./mocks.js";

// Browser truth for the chat interaction pass: stop-generating, the
// scroll-containment (no yank while reading + the Jump-to-latest pill), the
// conversation-loading skeleton, composer focus behaviors, inline error
// retry, and sidebar rename. The pure type-anywhere predicate is
// vitest-pinned (src/typeahead.test.js) — this file owns only what jsdom
// fakes: real focus, real scrolling, real in-flight requests.

const USER = { email: "user@example.edu", is_admin: false };

// A conversation long enough that the thread genuinely scrolls at 700px.
function longConversation(n = 8) {
  const msgs = [];
  for (let i = 0; i < n; i++) {
    msgs.push({ id: i * 2 + 1, role: "user", content: `Question ${i}?` });
    msgs.push({
      id: i * 2 + 2, role: "assistant",
      content: `Answer ${i}.\n\nSome longer prose so each exchange takes real vertical space.`,
      sql_log: null,
    });
  }
  return msgs;
}

test.describe("stop generating", () => {
  test("Send morphs to Stop while streaming; Stop frees the composer, shows the "
    + "stopped note, and the drained stream never bleeds back into the view", async ({ page }) => {
    await mockMe(page, USER);
    await mockConversations(page, []);
    await mockStreamChat(page, {
      conversationId: 7, delayMs: 1500, answer: "The eventual answer.", title: "T",
    });
    await page.goto("/");

    await page.getByPlaceholder("Ask about IPEDS data…").fill("slow question");
    await page.getByRole("button", { name: "Send" }).click();

    // While in flight: Stop replaces Send.
    const stop = page.getByRole("button", { name: "Stop generating" });
    await expect(stop).toBeVisible();
    await expect(page.getByRole("button", { name: "Send" })).toHaveCount(0);

    await stop.click();
    // The pending bubble becomes the stopped note; the composer is usable
    // again immediately (Send back, focus landed in the box).
    await expect(page.getByText(/^Stopped\./)).toBeVisible();
    await expect(page.getByRole("button", { name: "Send" })).toBeVisible();
    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeFocused();

    // The abandoned stream still drains (deliberately no abort — the server
    // must be allowed to persist). Its answer must NOT replace the stopped
    // note or navigate the viewer to /chat/7.
    await page.waitForTimeout(1800);
    await expect(page.getByText(/^Stopped\./)).toBeVisible();
    await expect(page.getByText("The eventual answer.")).toHaveCount(0);
    await expect(page).toHaveURL("/");
  });

  // THE REGRESSION, in full: stopGenerating() bumps turnToken, so isMine() is
  // false at the finalize -- and that block was the ONLY place msgId /
  // userMsgId were applied. The stopped turn's user message therefore kept no
  // `id`, so Rerun sent editMessageId: undefined -> chat.py set
  // edit_from = None -> _persist skipped its DELETE and APPENDED. The client
  // had already done slice(0, i), so the DB silently grew a second copy of the
  // question with a different answer, visible only after a reload. Silent
  // because a stopped turn is the LAST turn: laterTurnsLost() is 0, so the
  // destructive-edit confirmation never fires.
  test("a stopped turn's Rerun REPLACES it instead of appending a duplicate",
    async ({ page }) => {
      await mockMe(page, USER);
      await mockConversations(page, []);
      const stream = await mockStreamChat(page, {
        conversationId: 7, delayMs: 900, answer: "The eventual answer.",
        messageId: 42, userMessageId: 41,
      });
      await page.goto("/");

      await page.getByPlaceholder("Ask about IPEDS data…").fill("slow question");
      await page.getByRole("button", { name: "Send" }).click();
      await page.getByRole("button", { name: "Stop generating" }).click();
      await expect(page.getByText(/^Stopped\./)).toBeVisible();

      // Let the abandoned stream drain — that is when the done event (and so
      // the ids) actually arrives.
      await expect.poll(() => stream.calls.length).toBe(1);
      await page.waitForTimeout(1200);

      // Rerun the stopped question. It is the last turn, so no modal.
      await page.getByRole("button", { name: "Rerun" }).click();

      await expect.poll(() => stream.calls.length).toBe(2);
      expect(stream.calls[1].edit_message_id).toBe(41);
    });

  test("ids from a turn stopped in one chat never land in another chat",
    async ({ page }) => {
      // The finalize writes were POSITIONAL (c.length-1 / c.length-2), i.e.
      // "the last two messages" — whoever those happen to be by the time a
      // drained stream returns. Applying the ids ungated would therefore stamp
      // them onto a conversation the user has since opened. The fix targets by
      // per-turn lookup instead, so this is a no-op rather than corruption.
      await mockMe(page, USER);
      await mockConversations(page, [{ id: 3, title: "Other chat", updated_at: 0 }]);
      await mockConversation(page, 3, [
        { id: 900, role: "user", content: "a question in the other chat" },
        { id: 901, role: "assistant", content: "its answer" },
      ]);
      const stream = await mockStreamChat(page, {
        conversationId: 7, delayMs: 900, answer: "The eventual answer.",
        messageId: 42, userMessageId: 41,
      });
      await page.goto("/");

      await page.getByPlaceholder("Ask about IPEDS data…").fill("slow question");
      await page.getByRole("button", { name: "Send" }).click();
      await page.getByRole("button", { name: "Stop generating" }).click();
      await expect(page.getByText(/^Stopped\./)).toBeVisible();

      // Navigate away while the abandoned stream is still draining.
      await page.getByRole("link", { name: /Other chat/ }).click();
      await expect(page.getByText("a question in the other chat")).toBeVisible();
      await page.waitForTimeout(1200);

      // Content is intact...
      await expect(page.getByText("slow question")).toHaveCount(0);
      await expect(page.getByText("The eventual answer.")).toHaveCount(0);
      await expect(page.getByText("a question in the other chat")).toBeVisible();

      // ...and so are the IDS, which is the half that actually matters and is
      // invisible on screen. Rerunning this conversation's own question must
      // carry its own message id (900). Under a positional write it would have
      // been overwritten with the stopped turn's 41, and every later edit or
      // rerun in THIS conversation would then target the wrong row server-side.
      await page.getByRole("button", { name: "Rerun" }).click();
      await expect.poll(() => stream.calls.length).toBe(2);
      expect(stream.calls[1].edit_message_id).toBe(900);
    });

  test("a NEW question asked after a stop does not inherit the stopped turn's ids",
    async ({ page }) => {
      // THIS is the case a turnToken-equality gate cannot catch, and the reason
      // the fix keys on per-message identity. submit() never bumps turnToken,
      // so the turn started after the stop captures the SAME token value the
      // stopped turn compares against — a "was I the stopped turn?" check is
      // true for both, and the stale turn's ids would land on the new turn's
      // messages (which are now the last two in the array).
      await mockMe(page, USER);
      await mockConversations(page, []);
      // The two turns MUST return different ids, or this test cannot detect the
      // leak it exists for: with one shared id, a stale write and a correct
      // write are indistinguishable and the assertion passes either way.
      const calls = [];
      let nth = 0;
      await page.route("**/api/chat/stream", async (route) => {
        calls.push(route.request().postDataJSON());
        const turn = ++nth;
        // Turn 1 is the one that gets stopped, and it drains slowly so it
        // settles AFTER turn 2 has appended its own messages.
        const [uid, mid, delay, answer] = turn === 1
          ? [41, 42, 900, "The eventual answer."]
          : [51, 52, 0, "The second answer."];
        if (delay) await new Promise((r) => setTimeout(r, delay));
        const body = [
          { type: "conversation", id: 7 },
          { type: "answer", text: answer },
          { type: "done", message_id: mid, user_message_id: uid, model: "t", tokens: 0 },
        ].map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");
        await route.fulfill({ status: 200, contentType: "text/event-stream", body });
      });
      await page.goto("/");

      await page.getByPlaceholder("Ask about IPEDS data…").fill("slow question");
      await page.getByRole("button", { name: "Send" }).click();
      await page.getByRole("button", { name: "Stop generating" }).click();
      await expect(page.getByText(/^Stopped\./)).toBeVisible();

      // Ask again immediately, while the first stream is still draining, then
      // let the abandoned one land.
      await page.getByPlaceholder("Ask about IPEDS data…").fill("second question");
      await page.getByRole("button", { name: "Send" }).click();
      await expect(page.getByText("The second answer.")).toBeVisible();
      await page.waitForTimeout(1200);

      // Rerunning the SECOND question must carry the second turn's own id (51).
      // If the stopped turn's ids had been applied positionally they would have
      // landed on these messages and this would read 41.
      await page.getByRole("button", { name: "Rerun" }).last().click();
      await expect.poll(() => calls.length).toBe(3);
      expect(calls[2].question).toBe("second question");
      expect(calls[2].edit_message_id).toBe(51);
    });

  test("the stopped note waits for the answer, then offers a check that works",
    async ({ page }) => {
      // The note used to say "reopen it in a moment to check", and there was no
      // way to do that. settleTurn deliberately schedules no reload for a
      // stopped turn (the no-yank), and re-clicking the conversation you are
      // already in is not a route change — so nothing refetched, and the only
      // thing that actually worked was a page reload, which kills the turn the
      // note promises will be saved.
      //
      // Three contracts in one flow, because they only make sense together: the
      // answer must not arrive on its own, the check must not be offered before
      // it can succeed, and the click must fetch exactly once.
      await mockMe(page, USER);
      await mockConversations(page, [{ id: 1, title: "Chat" }]);

      const base = [
        { id: 900, role: "user", content: "an earlier question" },
        { id: 901, role: "assistant", content: "its answer" },
      ];
      const withAnswer = [...base,
        { id: 902, role: "user", content: "slow question" },
        { id: 903, role: "assistant", content: "The eventual answer." }];
      // The thread the server would return, before and after the drained turn
      // commits. A static fixture cannot express this test at all: the whole
      // question is whether a fetch issued at the wrong moment brings the
      // answer back or the thread as it stood before the question.
      let persisted = false;
      let convCalls = 0;
      await page.route("**/api/chat/conversations/1", async (route) => {
        if (route.request().method() !== "GET") return route.continue();
        convCalls += 1;
        await route.fulfill({
          status: 200, contentType: "application/json",
          body: JSON.stringify(persisted ? withAnswer : base),
        });
      });
      let streamCalls = 0;
      await page.route("**/api/chat/stream", async (route) => {
        streamCalls += 1;
        await new Promise((r) => setTimeout(r, 1200));
        // _persist commits BEFORE the done event is yielded, so the rows are on
        // disk by the time the client settles the turn.
        persisted = true;
        const body = [
          { type: "conversation", id: 1 },
          { type: "answer", text: "The eventual answer." },
          { type: "done", message_id: 903, user_message_id: 902, model: "t", tokens: 0 },
        ].map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");
        await route.fulfill({ status: 200, contentType: "text/event-stream", body });
      });

      await page.goto("/chat/1");
      await expect(page.getByText("an earlier question")).toBeVisible();
      expect(convCalls).toBe(1);

      await page.getByPlaceholder("Ask about IPEDS data…").fill("slow question");
      await page.getByRole("button", { name: "Send" }).click();
      await page.getByRole("button", { name: "Stop generating" }).click();

      // Mid-drain: the answer is NOT on disk yet, so there is nothing to check
      // and the button must not be there. Asserted synchronously — an
      // auto-retrying matcher would simply wait the 1.2s stream out and pass
      // having never looked at the state this assertion is about.
      await expect(page.getByText(/^Stopped\. The answer is still being written/)).toBeVisible();
      expect(await page.getByRole("button", { name: "Check now" }).count()).toBe(0);

      // The drained stream lands. The note flips to the settled wording and
      // offers the check...
      await expect.poll(() => streamCalls).toBe(1);
      const check = page.getByRole("button", { name: "Check now" });
      await expect(check).toBeVisible();
      await expect(page.getByText(/^Stopped\. The answer has been saved/)).toBeVisible();

      // ...and the answer still has NOT been pulled in under the reader who
      // chose to stop watching. No refetch happened on its own: convCalls is
      // the direct pin on the no-yank, and it fails if a future change makes
      // settleTurn bump the counter for a hidden turn.
      await expect(page.getByText("The eventual answer.")).toHaveCount(0);
      expect(convCalls).toBe(1);

      await check.click();
      await expect(page.getByText("The eventual answer.")).toBeVisible();
      await expect(page.getByText(/^Stopped\./)).toHaveCount(0);
      // The click destroys its own button, so focus has to be put somewhere
      // deliberate or it falls to <body> and the next Tab restarts at the top
      // of the page.
      await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeFocused();
      // Exactly one fetch — the counter is a useEffect dep, and a value that
      // could change again on the same click would refetch in a loop.
      expect(convCalls).toBe(2);
    });
});

test.describe("scroll containment", () => {
  // The app animates its follow-scroll (scrollIntoView behavior:"smooth")
  // unless the user prefers reduced motion. Under an animation, a test's
  // scrollTop writes race the easing frames — emulate reduced motion so
  // every scroll in here is instantaneous and deterministic (the containment
  // LOGIC, not the easing, is what's under test).
  test.use({ contextOptions: { reducedMotion: "reduce" } });

  test("scrolling up shows the Jump-to-latest pill; jumping returns to the "
    + "bottom and hides it", async ({ page }) => {
    await mockMe(page, USER);
    await mockConversations(page, [{ id: 1, title: "Long chat" }]);
    await mockConversation(page, 1, longConversation());
    await page.goto("/chat/1");
    await expect(page.getByText("Answer 7.")).toBeVisible();

    // Opening a conversation lands at the latest message — no pill.
    await expect(page.getByRole("button", { name: "Jump to latest message" })).toHaveCount(0);

    await page.locator(".messages").evaluate((el) => { el.scrollTop = 0; });
    const pill = page.getByRole("button", { name: "Jump to latest message" });
    await expect(pill).toBeVisible();

    await pill.click();
    await expect(pill).toHaveCount(0);
    // Genuinely at the bottom: the last exchange is in view.
    await expect(page.getByText("Answer 7.")).toBeInViewport();
  });

  // Asking a question scrolls YOUR OWN question into view. send() has always
  // done this, but the three other paths that call submit() -- a suggestion
  // chip, Rerun, and Save-edit -- did not, so from a scrolled-up thread the new
  // pending bubble was appended off-screen and only the "Latest" pill changed.
  // The click looked like it had done nothing at all.
  //
  // Only the CHIP path is pinned here, deliberately. Rerun and Save-edit share
  // the same pin site (submit()), but they slice the thread SHORTER first, and
  // that shrink already restores the near-bottom state on its own — a rerun
  // test passed with the bug present at 8 turns and still at 30, so it would
  // have been a test that cannot fail. They get the fix by construction; this
  // is the path that could actually demonstrate the defect.
  test("a suggestion chip scrolls its new turn into view from a scrolled-up "
    + "thread", async ({ page }) => {
    await mockMe(page, USER);
    await mockConversations(page, [{ id: 1, title: "Long chat" }]);
    const msgs = longConversation();
    msgs[msgs.length - 1].suggestions = ["How about bachelor's only?"];
    await mockConversation(page, 1, msgs);
    await mockStreamChat(page, { conversationId: 1, answer: "Chip answer." });
    await page.goto("/chat/1");
    await expect(page.getByText("Answer 7.")).toBeVisible();

    await page.locator(".messages").evaluate((el) => { el.scrollTop = 0; });
    await expect(page.getByRole("button", { name: "Jump to latest message" })).toBeVisible();

    await page.getByRole("button", { name: "How about bachelor's only?" }).click();
    // The question the chip just asked must be on screen, and the pill gone --
    // the pill still showing IS the "nothing happened" symptom.
    await expect(page.getByText("How about bachelor's only?").last()).toBeInViewport();
    await expect(page.getByRole("button", { name: "Jump to latest message" })).toHaveCount(0);
  });

  test("REGRESSION: a finalizing answer does not yank a viewer who has "
    + "scrolled up to read", async ({ page }) => {
    await mockMe(page, USER);
    await mockConversations(page, [{ id: 1, title: "Long chat" }]);
    await mockConversation(page, 1, longConversation());
    await mockStreamChat(page, {
      conversationId: 1, delayMs: 1200, answer: "Fresh streamed answer.",
    });
    await page.goto("/chat/1");
    await expect(page.getByText("Answer 7.")).toBeVisible();

    await page.getByPlaceholder("Ask about IPEDS data…").fill("another question");
    await page.getByRole("button", { name: "Send" }).click();
    // While the model "thinks", scroll up to re-read an earlier answer.
    await page.locator(".messages").evaluate((el) => { el.scrollTop = 0; });
    await expect(page.getByRole("button", { name: "Jump to latest message" })).toBeVisible();

    // Let the stream finalize, then assert the view stayed put (the old
    // behavior scrolled to the bottom on EVERY message/status change).
    await page.waitForTimeout(1600);
    await expect(page.getByText("Fresh streamed answer.")).toHaveCount(1); // it DID land
    const scrollTop = await page.locator(".messages").evaluate((el) => el.scrollTop);
    expect(scrollTop).toBeLessThan(200); // still reading at the top
    await expect(page.getByRole("button", { name: "Jump to latest message" })).toBeVisible();
  });
});

// THE REGRESSION: the empty state used to STATE the loaded range as fact
// ("collection years 2019-20 through 2024-25") while every deployment picks its
// own years via Admin → Imports. It was wrong everywhere but the machine it was
// typed on, and nothing could catch it, because a hardcoded string renders just
// as confidently as a correct one. These drive the range from /me, so a
// re-hardcoding fails: the wording must FOLLOW the mocked bounds.
test.describe("chat empty state names the loaded collection years", () => {
  test("reads the range from /me rather than a literal", async ({ page }) => {
    await mockMe(page, { ...USER, years: { min: 2016, max: 2021 } });
    await mockConversations(page, []);
    await page.goto("/");

    // Deliberately NOT the 2020–2025 the shared mock defaults to: a literal left
    // in the source would still print the old span and pass a laxer assertion.
    await expect(page.locator(".empty p.muted"))
      .toContainText("collection years 2015-16 through 2020-21");
  });

  test("a single loaded year is not written as a range", async ({ page }) => {
    await mockMe(page, { ...USER, years: { min: 2025, max: 2025 } });
    await mockConversations(page, []);
    await page.goto("/");

    const blurb = page.locator(".empty p.muted");
    await expect(blurb).toContainText("collection year 2024-25");
    await expect(blurb).not.toContainText("through");
  });

  test("omits the clause entirely when /me carries no years", async ({ page }) => {
    // An older server (or a deployment mid-import) sends no bounds. The sentence
    // has to stay grammatical rather than trailing a half-written clause.
    await mockMe(page, { ...USER, years: null });
    await mockConversations(page, []);
    await page.goto("/");

    const blurb = page.locator(".empty p.muted");
    await expect(blurb).toContainText("staffing and finance.");
    await expect(blurb).not.toContainText("across");
  });
});

test.describe("the empty state belongs to the no-conversation route", () => {
  test("a conversation route with no messages never shows the index greeting",
    async ({ page }) => {
      // FOUND LIVE: the in-flight placeholder was replaced by the new-chat
      // greeting — heading, blurb and six example chips — while the URL was
      // still /chat/:id and the answer was already saved. `messages` being
      // momentarily empty on a conversation route is a transient to ride out
      // (the loader is mid-flight, or a turn hasn't persisted yet), never a cue
      // to offer a fresh start. The index page must not impersonate a chat.
      await mockMe(page, USER);
      await mockConversations(page, [{ id: 4, title: "A chat", updated_at: 0 }]);
      await mockConversation(page, 4, []);   // genuinely empty, as a mid-flight turn reads
      await page.goto("/chat/4");

      await expect(page.getByText("What would you like to know")).toHaveCount(0);
      await expect(page.locator(".examples-grid")).toHaveCount(0);
      // ...and the route itself is unchanged; this is about what renders, not
      // where we are.
      expect(new URL(page.url()).pathname).toBe("/chat/4");
    });

  test("a failed conversation load says so INSTEAD of offering a fresh start",
    async ({ page }) => {
      // The two used to render together: "That conversation isn't available."
      // above "What would you like to know about U.S. colleges?" — an error and
      // an invitation, at once. The skeleton already guarded this with
      // !showNotice; the empty states never did.
      await mockMe(page, USER);
      await mockConversations(page, [{ id: 4, title: "A chat", updated_at: 0 }]);
      await mockConversation(page, 4, [], { httpStatus: 500 });
      await page.goto("/chat/4");

      // .notice specifically — the same text is deliberately duplicated into an
      // sr-only live region, so a bare getByText is a strict-mode violation.
      await expect(page.locator(".notice").filter({ hasText: /isn't available/ }))
        .toBeVisible();
      await expect(page.getByText("What would you like to know")).toHaveCount(0);
    });

  test("the greeting still shows on the no-conversation route", async ({ page }) => {
    // The other half of the bound: scoping it must not delete the empty state
    // from the one place it belongs, which is where every example chip lives.
    await mockMe(page, USER);
    await mockConversations(page, []);
    await page.goto("/");

    await expect(page.getByText("What would you like to know")).toBeVisible();
    await expect(page.locator(".examples-grid")).toBeVisible();
  });
});

test.describe("conversation-loading skeleton", () => {
  test("switching to a conversation shows the skeleton — never the "
    + "empty-state prompt — until messages land", async ({ page }) => {
    await mockMe(page, USER);
    await mockConversations(page, [{ id: 1, title: "Slow chat" }]);
    await mockConversation(page, 1, longConversation(2), { delayMs: 800 });
    await page.goto("/");
    // The empty-state prompt is correct on "/" (new chat)...
    await expect(page.getByRole("heading", { name: /What would you like to know/ })).toBeVisible();

    await page.getByRole("link", { name: "Slow chat" }).click();
    // ...but during the fetch the skeleton shows and the prompt does NOT
    // (the old behavior flashed the prompt over every conversation switch).
    await expect(page.getByTestId("convo-skeleton")).toBeVisible();
    await expect(page.getByRole("heading", { name: /What would you like to know/ })).toHaveCount(0);

    await expect(page.getByText("Answer 1.")).toBeVisible();
    await expect(page.getByTestId("convo-skeleton")).toHaveCount(0);
  });
});

test.describe("composer focus", () => {
  test("the composer autofocuses on load, and typing anywhere lands in it", async ({ page }) => {
    await mockMe(page, USER);
    await mockConversations(page, [{ id: 1, title: "A chat" }]);
    await mockConversation(page, 1, longConversation(1));
    await page.goto("/");
    const composer = page.getByPlaceholder("Ask about IPEDS data…");
    await expect(composer).toBeFocused();

    // Click a sidebar chat (focus moves to the link), then just type —
    // the keystrokes are redirected into the composer.
    await page.getByRole("link", { name: "A chat" }).click();
    await expect(page.getByText("Answer 0.")).toBeVisible();
    await page.keyboard.type("follow-up");
    await expect(composer).toHaveValue("follow-up");
    await expect(composer).toBeFocused();
  });
});

test.describe("inline error retry", () => {
  test("a failed turn shows Try again on the answer itself; clicking it "
    + "re-sends the same question", async ({ page }) => {
    await mockMe(page, USER);
    await mockConversations(page, []);
    let calls = 0;
    await page.route("**/api/chat/stream", async (route) => {
      calls += 1;
      await route.fulfill({ status: 500, contentType: "application/json",
        body: JSON.stringify({ detail: "boom" }) });
    });
    await page.goto("/");
    await page.getByPlaceholder("Ask about IPEDS data…").fill("doomed question");
    await page.getByRole("button", { name: "Send" }).click();

    const retry = page.getByRole("button", { name: "Try again" });
    await expect(retry).toBeVisible();
    await retry.click();
    await expect(retry).toBeVisible(); // fails again — still recoverable
    expect(calls).toBe(2);
  });
});

test.describe("thinking / SQL trace toggles", () => {
  test("Thinking and SQL are mutually-exclusive toggles whose panel opens "
    + "full-width below the actions row (never reflowing the copy buttons)", async ({ page }) => {
    await mockMe(page, USER);
    await mockConversations(page, []);
    await mockStreamChat(page, {
      conversationId: 5, sql: ["SELECT stabbr FROM c_a"], answer: "Here you go.",
    });
    await page.goto("/");
    await page.getByPlaceholder("Ask about IPEDS data…").fill("give me states");
    await page.getByRole("button", { name: "Send" }).click();
    await expect(page.getByText("Here you go.")).toBeVisible();

    const thinking = page.getByRole("button", { name: "Thinking", exact: true });
    const sql = page.getByRole("button", { name: "SQL", exact: true });
    await expect(thinking).toHaveAttribute("aria-expanded", "false");
    await expect(sql).toHaveAttribute("aria-expanded", "false");
    // Nothing expanded yet.
    await expect(page.locator(".trace-panel")).toHaveCount(0);

    // Open SQL: its panel appears full-width — a SIBLING of .msg-actions, never
    // nested inside it (the old inline <details> widened the flex row).
    await sql.click();
    await expect(sql).toHaveAttribute("aria-expanded", "true");
    await expect(page.locator(".trace-panel .sqlblock")).toContainText("c_a");
    await expect(page.locator(".msg-actions .trace-panel")).toHaveCount(0);

    // Open Thinking: SQL closes (mutual exclusivity) — exactly one panel at a time.
    await page.getByRole("button", { name: "Thinking", exact: true }).click();
    await expect(page.getByRole("button", { name: "Thinking", exact: true }))
      .toHaveAttribute("aria-expanded", "true");
    await expect(page.getByRole("button", { name: "SQL", exact: true }))
      .toHaveAttribute("aria-expanded", "false");
    await expect(page.locator(".trace-panel")).toHaveCount(1);

    // Toggling the open panel closes it.
    await page.getByRole("button", { name: "Thinking", exact: true }).click();
    await expect(page.locator(".trace-panel")).toHaveCount(0);
  });

  test("REGRESSION: a reopened conversation still shows the Thinking trace "
    + "(it is persisted server-side, not only live)", async ({ page }) => {
    await mockMe(page, USER);
    await mockConversations(page, [{ id: 9, title: "Past chat" }]);
    // The server returns the persisted trace (JSON) alongside sql_log.
    await mockConversation(page, 9, [
      { id: 1, role: "user", content: "an earlier question" },
      {
        id: 2, role: "assistant", content: "an earlier answer.",
        sql_log: ["SELECT 1"],
        thinking: [
          { kind: "reason", text: "recalling how this was reasoned" },
          { kind: "status", text: "Running query…" },
          { kind: "sql", text: "SELECT 1" },
        ],
      },
    ]);
    // Deep-link straight into the chat — the reopen/refresh path, no live stream.
    await page.goto("/chat/9");
    await expect(page.getByText("an earlier answer.")).toBeVisible();

    const thinking = page.getByRole("button", { name: "Thinking", exact: true });
    await expect(thinking).toBeVisible();
    await thinking.click();
    await expect(page.locator(".trace-panel")).toContainText("recalling how this was reasoned");
  });

  test("REGRESSION: a large SQL query in the Thinking trace is not flex-squished "
    + "to one line — it's a capped, scrollable window", async ({ page }) => {
    // A many-line query. The Thinking trace is a flex column that, without
    // flex:none on the SQL block, shrinks a tall child to ~16px (the reported
    // 'single line' bug). It must instead keep a readable, capped height.
    const bigSql = "SELECT " + Array.from({ length: 30 }, (_, i) => `col_${i}`).join(", ")
      + " FROM c_a WHERE year = (SELECT MAX(year) FROM _years) GROUP BY 1 ORDER BY 1";
    await mockMe(page, USER);
    await mockConversations(page, []);
    await mockStreamChat(page, { conversationId: 3, sql: [bigSql], answer: "Done." });
    await page.goto("/");
    await page.getByPlaceholder("Ask about IPEDS data…").fill("big query");
    await page.getByRole("button", { name: "Send" }).click();
    await expect(page.getByText("Done.")).toBeVisible();

    await page.getByRole("button", { name: "Thinking", exact: true }).click();
    const sql = page.locator(".trace-panel .thought-sql");
    const box = await sql.evaluate((el) => ({ clientH: el.clientHeight, scrollH: el.scrollHeight }));
    // Not squished to a sliver (the bug produced ~16px), and capped below the
    // full content height with a scroll region (~9-10 line window).
    expect(box.clientH).toBeGreaterThan(80);
    expect(box.clientH).toBeLessThan(260);
    expect(box.scrollH).toBeGreaterThan(box.clientH + 2);
  });
});

test.describe("sidebar rename", () => {
  async function openWithChats(page) {
    await mockMe(page, USER);
    await mockConversations(page, [
      { id: 1, title: "Nursing trend" }, { id: 2, title: "Tuition data" },
    ]);
    await page.goto("/");
    await expect(page.getByRole("link", { name: "Tuition data" })).toBeVisible();
  }

  test("pencil → inline input → Enter commits: PATCHes the new title and the "
    + "sidebar updates optimistically", async ({ page }) => {
    const rename = await mockRenameConversation(page, 2);
    await openWithChats(page);

    const row = page.locator(".convo-row", { hasText: "Tuition data" });
    await row.hover();
    await row.getByRole("button", { name: "Rename chat: Tuition data" }).click();

    const input = page.getByRole("textbox", { name: "Rename chat: Tuition data" });
    await expect(input).toBeFocused();
    await input.fill("My tuition study");
    await input.press("Enter");

    await expect(page.getByRole("link", { name: "My tuition study" })).toBeVisible();
    await expect.poll(() => rename.calls).toEqual([{ title: "My tuition study" }]);
    // Focus lands back on the renamed row's link (WCAG 2.4.3 — the input
    // unmounted; focus must not drop to <body>).
    await expect(page.getByRole("link", { name: "My tuition study" })).toBeFocused();
  });

  test("Escape cancels without a PATCH; a failed PATCH reverts the title and "
    + "toasts", async ({ page }) => {
    const rename = await mockRenameConversation(page, 2, { httpStatus: 500 });
    await openWithChats(page);
    const row = page.locator(".convo-row", { hasText: "Tuition data" });

    // Escape: no request, original title intact.
    await row.hover();
    await row.getByRole("button", { name: "Rename chat: Tuition data" }).click();
    await page.getByRole("textbox", { name: "Rename chat: Tuition data" }).press("Escape");
    await expect(page.getByRole("link", { name: "Tuition data" })).toBeVisible();
    expect(rename.calls).toEqual([]);

    // Failed PATCH: optimistic title reverts, error toast explains.
    await row.hover();
    await row.getByRole("button", { name: "Rename chat: Tuition data" }).click();
    const input = page.getByRole("textbox", { name: "Rename chat: Tuition data" });
    await input.fill("Won't stick");
    await input.press("Enter");
    await expect(page.locator(".toast-msg")).toHaveText("Couldn't rename the chat. Try again.");
    await expect(page.getByRole("link", { name: "Tuition data" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Won't stick" })).toHaveCount(0);
  });
});

test.describe("copy menu (UX-H3)", () => {
  // The two "Copy Markdown"/"Copy HTML" text buttons collapse into ONE menu
  // button. jsdom fakes focus + clipboard, so the menu's focus/keyboard/clipboard
  // truth lives here, not in vitest.
  test.use({ permissions: ["clipboard-read", "clipboard-write"] });

  async function openAnswer(page) {
    await mockMe(page, USER);
    await mockConversations(page, [{ id: 9, title: "Past chat" }]);
    await mockConversation(page, 9, [
      { id: 1, role: "user", content: "an earlier question" },
      { id: 2, role: "assistant", content: "The copyable answer." },
    ]);
    await page.goto("/chat/9");
    await expect(page.getByText("The copyable answer.")).toBeVisible();
  }

  test("one Copy menu replaces the two copy buttons; a menuitem copies and closes", async ({ page }) => {
    await openAnswer(page);
    // The old two separate text buttons are gone.
    await expect(page.getByRole("button", { name: "Copy Markdown" })).toHaveCount(0);

    const trigger = page.getByRole("button", { name: "Copy", exact: true });
    await expect(trigger).toHaveAttribute("aria-haspopup", "menu");
    await expect(trigger).toHaveAttribute("aria-expanded", "false");

    await trigger.click();
    await expect(trigger).toHaveAttribute("aria-expanded", "true");
    const menu = page.getByRole("menu", { name: "Copy answer" });
    await expect(menu.getByRole("menuitem", { name: "Copy Markdown" })).toBeVisible();
    await expect(menu.getByRole("menuitem", { name: "Copy rich HTML" })).toBeVisible();

    await menu.getByRole("menuitem", { name: "Copy Markdown" }).click();
    // Menu closes and the answer text reached the clipboard.
    await expect(page.getByRole("menu", { name: "Copy answer" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Copied!" })).toBeVisible();
    const clip = await page.evaluate(() => navigator.clipboard.readText());
    expect(clip).toContain("The copyable answer.");
  });

  // Both copy helpers swallow their errors and return false, and neither call
  // site had an `else` — so a denied clipboard was indistinguishable from
  // success: no toast, no state change, the trigger still reading "Copy". The
  // user believes the answer is on their clipboard and pastes something else.
  //
  // Blocking BOTH routes matters: copyText falls through from
  // navigator.clipboard to a document.execCommand fallback, so stubbing only
  // the first would still succeed and the test would prove nothing.
  async function blockTheClipboard(page) {
    await page.addInitScript(() => {
      Object.defineProperty(globalThis.navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: () => Promise.reject(new Error("denied")),
          write: () => Promise.reject(new Error("denied")),
          readText: () => Promise.resolve(""),
        },
      });
      globalThis.document.execCommand = () => false;
    });
  }

  test("a blocked clipboard reports the failure instead of claiming success", async ({ page }) => {
    await blockTheClipboard(page);
    await openAnswer(page);

    const trigger = page.getByRole("button", { name: "Copy", exact: true });
    await trigger.click();
    await page.getByRole("menu", { name: "Copy answer" })
      .getByRole("menuitem", { name: "Copy Markdown" }).click();

    const toast = page.locator(".toast");
    await expect(toast).toHaveClass(/\berror\b/);
    await expect(toast).toContainText(/couldn't copy/i);
    // And it must never claim success — "Copied!" appearing at all is the bug.
    await expect(page.getByRole("button", { name: "Copied!" })).toHaveCount(0);
    await expect(trigger).toBeVisible();
  });

  test("a blocked clipboard reports the failure from Copy SQL too", async ({ page }) => {
    await blockTheClipboard(page);
    await mockMe(page, USER);
    await mockConversations(page, [{ id: 9, title: "Past chat" }]);
    await mockConversation(page, 9, [
      { id: 1, role: "user", content: "an earlier question" },
      { id: 2, role: "assistant", content: "The copyable answer.", sql_log: ["SELECT 1"] },
    ]);
    await page.goto("/chat/9");
    // The second handler with the same shape — a separate call site, so a fix
    // applied only to doCopy would leave this one silent.
    await page.getByRole("button", { name: /^SQL/ }).click();
    await page.getByRole("button", { name: "Copy SQL" }).click();

    const toast = page.locator(".toast");
    await expect(toast).toHaveClass(/\berror\b/);
    await expect(toast).toContainText(/couldn't copy/i);
    await expect(page.getByRole("button", { name: "Copied!" })).toHaveCount(0);
  });

  test("Escape closes the menu and restores focus to the trigger; click-outside closes", async ({ page }) => {
    await openAnswer(page);
    const trigger = page.getByRole("button", { name: "Copy", exact: true });

    await trigger.click();
    await expect(page.getByRole("menu", { name: "Copy answer" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("menu", { name: "Copy answer" })).toHaveCount(0);
    await expect(trigger).toBeFocused();

    // Reopen, then click outside → closes.
    await trigger.click();
    await expect(page.getByRole("menu", { name: "Copy answer" })).toBeVisible();
    await page.getByText("The copyable answer.").click();
    await expect(page.getByRole("menu", { name: "Copy answer" })).toHaveCount(0);
  });
});

// Re-asking an EARLIER prompt deletes that turn and every turn after it, in the
// UI and server-side, permanently and with no undo. Browser truth for the
// confirmation that now gates it — and, just as important, for the last-turn
// path staying modal-free so the ordinary refine gesture isn't nagged.
test.describe("destructive edit/rerun confirmation", () => {
  test("editing a mid-conversation prompt confirms first, naming what is lost; "
    + "cancel sends nothing", async ({ page }) => {
    await mockMe(page, USER);
    await mockConversations(page, [{ id: 5, title: "Chat", updated_at: 0 }]);
    await mockConversation(page, 5, longConversation(4));   // 4 turns
    const stream = await mockStreamChat(page, { conversationId: 5, answer: "Replaced." });
    await page.goto("/chat/5");

    // Turn 0 of 4 -> three later exchanges die.
    await page.getByText("Question 0?").hover({ force: true });
    await page.getByRole("button", { name: "Edit", exact: true }).first().click();
    await page.locator(".edit-box .md-editor-ta").fill("Revised question 0?");
    await page.locator(".edit-box").getByRole("button", { name: "Send" }).click();

    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("3 later questions");
    await expect(dialog).toContainText("can't be undone");

    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toHaveCount(0);
    expect(stream.calls.length).toBe(0);            // nothing was sent
    await expect(page.getByText("Answer 3.")).toBeVisible();  // thread intact
    // The typed text survives the cancel — the editor was never torn down.
    await expect(page.locator(".edit-box .md-editor-ta")).toHaveValue("Revised question 0?");
  });

  test("confirming sends the edit with the right edit_message_id", async ({ page }) => {
    await mockMe(page, USER);
    await mockConversations(page, [{ id: 5, title: "Chat", updated_at: 0 }]);
    await mockConversation(page, 5, longConversation(4));
    const stream = await mockStreamChat(page, { conversationId: 5, answer: "Replaced." });
    await page.goto("/chat/5");

    await page.getByText("Question 1?").hover({ force: true });
    await page.getByRole("button", { name: "Edit", exact: true }).nth(1).click();
    await page.locator(".edit-box .md-editor-ta").fill("Revised question 1?");
    await page.locator(".edit-box").getByRole("button", { name: "Send" }).click();

    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("2 later questions");
    await dialog.getByRole("button", { name: /^Edit and remove/ }).click();

    await expect.poll(() => stream.calls.length).toBe(1);
    expect(stream.calls[0].question).toBe("Revised question 1?");
    // longConversation ids: turn 1's user message is id 3.
    expect(stream.calls[0].edit_message_id).toBe(3);
  });

  test("editing the LAST prompt is not gated — the ordinary refine stays "
    + "modal-free", async ({ page }) => {
    await mockMe(page, USER);
    await mockConversations(page, [{ id: 5, title: "Chat", updated_at: 0 }]);
    await mockConversation(page, 5, longConversation(3));
    const stream = await mockStreamChat(page, { conversationId: 5, answer: "Replaced." });
    await page.goto("/chat/5");

    await page.getByText("Question 2?").hover({ force: true });
    await page.getByRole("button", { name: "Edit", exact: true }).last().click();
    await page.locator(".edit-box .md-editor-ta").fill("Revised last?");
    await page.locator(".edit-box").getByRole("button", { name: "Send" }).click();

    await expect.poll(() => stream.calls.length).toBe(1);
    await expect(page.getByRole("alertdialog")).toHaveCount(0);
    expect(stream.calls[0].question).toBe("Revised last?");
  });

  test("Rerun on a mid-conversation prompt confirms with its own verb",
    async ({ page }) => {
    await mockMe(page, USER);
    await mockConversations(page, [{ id: 5, title: "Chat", updated_at: 0 }]);
    await mockConversation(page, 5, longConversation(3));
    const stream = await mockStreamChat(page, { conversationId: 5, answer: "Replaced." });
    await page.goto("/chat/5");

    await page.getByText("Question 0?").hover({ force: true });
    await page.getByRole("button", { name: "Rerun" }).first().click();

    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("button", { name: /^Rerun and remove 2 later exchanges$/ }))
      .toBeVisible();
    await dialog.getByRole("button", { name: "Cancel" }).click();
    expect(stream.calls.length).toBe(0);
  });
});
