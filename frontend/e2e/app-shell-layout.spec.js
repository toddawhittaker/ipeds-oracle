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
// Two independent fixes, both pinned below:
//   - `.sr-only` is pinned to `top/left: 0`, so it can never sit past content.
//     That is the root cause.
//   - `.app` is `overflow: clip` rather than `hidden`, so the NEXT such mistake
//     can only clip a pixel instead of stealing the header.
//
// CORRECTION, recorded because the wrong version was written here first and
// would have stopped the next reader trying the thing that works: this file
// used to claim the `clip` half could not be tested, "because a clip box
// reports no scrollable overflow by definition." That is false. Measured in
// this repo's own Chromium on the shell's exact CSS, with a 1000px in-flow
// child in a 600px box:
//     overflow:hidden  -> {scrollHeight:1018, clientHeight:600, scrollTop:418}
//     overflow:clip    -> {scrollHeight:1018, clientHeight:600, scrollTop:0}
// `clip` suppresses scrollTop, NOT scrollHeight. The earlier attempts failed
// for a different and duller reason: a 200px spacer is absorbed by
// `.chat { flex: 1 }`, which shrinks to fit, so nothing ever overflowed. The
// spacer just has to be taller than `.chat` can give up.

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

  test("the shell cannot be scrolled even when something overflows it",
    async ({ page }) => {
      // The `clip` half. The spacer must be taller than `.chat` can shrink by
      // (`.chat { flex: 1; min-height: 0 }` will give up everything it has), or
      // flex absorbs it and there is nothing to scroll — which is exactly how
      // the first two versions of this test came to pass against `hidden`.
      await open(page);
      const after = await page.evaluate(() => {
        const app = globalThis.document.querySelector(".app");
        const spacer = globalThis.document.createElement("div");
        spacer.style.cssText = "height:2000px;flex:none";
        app.appendChild(spacer);
        const grew = app.scrollHeight > app.clientHeight;
        app.scrollTop = 500;            // what focus()/scrollIntoView() does
        return {
          grew, top: app.scrollTop,
          y: Math.round(globalThis.document
            .querySelector(".topbar").getBoundingClientRect().y),
        };
      });
      // The premise: the shell really is overflowing now. Without this the two
      // assertions below are satisfiable by there being nothing to scroll.
      expect(after.grew, "the spacer failed to overflow the shell").toBe(true);
      expect(after.top, "the shell scrolled — it must not be scrollable").toBe(0);
      expect(after.y, "the top bar moved").toBe(0);
    });
});
