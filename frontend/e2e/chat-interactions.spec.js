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
