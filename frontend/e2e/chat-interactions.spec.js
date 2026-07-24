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
    await expect(page.getByText(/^Stopped\. If the answer finishes/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Send" })).toBeVisible();
    await expect(page.getByPlaceholder("Ask about IPEDS data…")).toBeFocused();

    // The abandoned stream still drains (deliberately no abort — the server
    // must be allowed to persist). Its answer must NOT replace the stopped
    // note or navigate the viewer to /chat/7.
    await page.waitForTimeout(1800);
    await expect(page.getByText(/^Stopped\. If the answer finishes/)).toBeVisible();
    await expect(page.getByText("The eventual answer.")).toHaveCount(0);
    await expect(page).toHaveURL("/");
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
