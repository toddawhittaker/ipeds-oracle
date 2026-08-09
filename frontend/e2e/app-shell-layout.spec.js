import { test, expect } from "@playwright/test";
import { mockMe, mockConversations, mockConversation } from "./mocks.js";

// The app SHELL must never scroll. `html, body { overflow: hidden }`, and every
// screen owns an inner scroller, so anything that makes `.app` taller than the
// viewport has no legitimate way to be reached.
//
// Reported from a real session: "chat 51 on a reload loses the top bar. It is
// partially cut off. New chat restores it." The shell was 24px too tall, and
// `overflow: hidden` (which `.app` used to carry) still permits PROGRAMMATIC
// scrolling — so the focus that runs when a conversation loads scrolled the
// whole shell by exactly that 24px and took the header with it, with no
// affordance to bring it back.
//
// The 24px came from a `<span class="sr-only">` inside the chart's delta badge.
// `.sr-only` is `position: absolute`, and NOTHING in its ancestor chain is
// positioned — so its containing block is the initial one (the viewport), not
// the `.messages` scroller it lives in. It escapes that scroller's clipping and
// is laid out at its STATIC position in the page: deep in a long thread, that
// lands below the fold, and the box becomes scrollable overflow on `.app`.
// Measured at y=923 in a 900px viewport.
//
// Two independent fixes. Only the first is pinned here, deliberately:
//   - `.sr-only` is pinned to `top/left: 0`, so it can never sit past content.
//     That is the root cause, and the tests below fail without it.
//   - `.app` is `overflow: clip` rather than `hidden`, so the NEXT such mistake
//     can only clip a pixel instead of stealing the header. That one has NO
//     test, on purpose: two attempts at one both turned out to be unfailable.
//     A flex-child spacer is absorbed by `.chat { flex: 1 }` so nothing ever
//     overflows; an absolutely-positioned probe with an explicit `top` is not
//     in `.app`'s containing-block chain and so adds no scrollable overflow to
//     it (only a STATICALLY positioned one does, which is why the real bug
//     behaved that way); and asserting `scrollHeight > clientHeight` as the
//     premise is unsatisfiable, because a `clip` box reports no scrollable
//     overflow by definition. A test that cannot fail is worse than no test,
//     so there is none — the hardening rests on the comment in styles.css.

const USER = { email: "user@example.edu", is_admin: false };

const CONVOS = Array.from({ length: 12 }, (_, i) => ({
  id: i + 1, title: `Conversation ${i}`, updated_at: 1_700_000_000 - i,
}));

// A chart fence produces the delta badge, and that badge carries the offending
// sr-only span. It has to sit at the END of a thread long enough to push its
// static position below the fold — that is the entire condition for the bug,
// and a short thread cannot express it.
const CHART = "```chart\n" + JSON.stringify({
  type: "line", x: "year", y: ["awards"], title: "Awards by year",
  data: [{ year: 2020, awards: 900 }, { year: 2021, awards: 870 },
    { year: 2022, awards: 840 }, { year: 2023, awards: 810 },
    { year: 2024, awards: 800 }, { year: 2025, awards: 790 }],
}) + "\n```";

const LONG_THREAD = (() => {
  const msgs = [];
  for (let i = 0; i < 6; i++) {
    msgs.push({ id: i * 2 + 1, role: "user", content: `Question ${i}?` });
    msgs.push({
      id: i * 2 + 2, role: "assistant",
      content: `Answer ${i}.\n\nEnough prose that each exchange takes real `
        + "vertical space in the thread and pushes what follows down the page.",
    });
  }
  msgs.push({ id: 100, role: "user", content: "And chart it?" });
  msgs.push({ id: 101, role: "assistant", content: `Here it is.\n\n${CHART}` });
  return msgs;
})();

async function open(page) {
  await mockMe(page, USER);
  await mockConversations(page, CONVOS);
  await mockConversation(page, 7, LONG_THREAD);
  await page.setViewportSize({ width: 1400, height: 900 });
  await page.goto("/chat/7");
  await expect(page.getByText("Here it is.")).toBeVisible();
}

const shell = (page) => page.evaluate(() => {
  const app = globalThis.document.querySelector(".app");
  return {
    topbarY: Math.round(
      globalThis.document.querySelector(".topbar").getBoundingClientRect().y),
    appScroll: app.scrollHeight, appClient: app.clientHeight, appTop: app.scrollTop,
  };
});

test.describe("the app shell never scrolls the top bar away", () => {
  test("a hidden live region deep in a thread does not grow the shell",
    async ({ page }) => {
      // The root cause, asserted at the source rather than through its symptom.
      await open(page);
      const g = await shell(page);
      expect(g.appScroll, "the shell must not overflow the viewport")
        .toBe(g.appClient);
    });

  test("the top bar stays put on a conversation route, and after a reload",
    async ({ page }) => {
      // The reported symptom. The reload is its own case: loading a
      // conversation is what triggers the focus that did the scrolling.
      await open(page);
      expect((await shell(page)).topbarY).toBe(0);

      await page.reload();
      await expect(page.getByText("Here it is.")).toBeVisible();
      const g = await shell(page);
      expect(g.topbarY, "the top bar was scrolled out of view").toBe(0);
      expect(g.appTop).toBe(0);
    });
});
