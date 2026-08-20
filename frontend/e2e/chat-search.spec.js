import { test, expect } from "@playwright/test";
import { mockAccessRequests, mockAllowlist, mockConversation, mockConversations,
         mockDeniedRequests, mockMe, mockStreamChat, mockStreamChatDripped,
         mockVersion } from "./mocks.js";

// Browser truth for the sidebar's conversation search. The RULES (terms ANDed,
// a quoted run kept whole, LIKE wildcards escaped) are the server's and are
// pinned in backend/tests/test_search.py + test_chat_router.py; what only a
// browser can show is that the box reaches the server at all, that the query
// survives the refreshes other actions trigger, and that an empty list says
// which KIND of empty it is.

const CONVOS = [
  { id: 1, title: "Nursing completions 2023", body: "the nursing count for 2023",
    created_at: 1_700_000_000, updated_at: 1_700_000_000 },
  { id: 2, title: "Nursing completions 2019", body: "the nursing count for 2019",
    created_at: 1_699_000_000, updated_at: 1_699_000_000 },
  { id: 3, title: "Engineering enrollment", body: "engineering headcount",
    created_at: 1_698_000_000, updated_at: 1_698_000_000 },
];

async function openChat(page, rows = CONVOS) {
  await mockMe(page, { email: "user@example.edu", is_admin: false });
  await mockVersion(page);
  const convos = await mockConversations(page, rows);
  await page.goto("/");
  await expect(page.getByRole("link", { name: "Nursing completions 2023" })).toBeVisible();
  return convos;
}

const titles = (page) => page.locator(".convo-row .convo");
// The empty/failed states, and the always-mounted live region that mirrors
// them. Addressed separately because the two carry the same sentence, and a
// getByText would match both.
const emptyState = (page) => page.locator(".convo-empty");
const liveRegion = (page) => page.locator(".convo-live");

test("the sidebar search sends the query and narrows the list", async ({ page }) => {
  const convos = await openChat(page);

  await page.getByRole("searchbox", { name: "Search chats and messages" }).fill("nursing 2023");

  await expect(titles(page)).toHaveText(["Nursing completions 2023"]);
  // Sent to the SERVER, not filtered from the list already loaded — the text
  // being searched lives in messages the browser has never fetched.
  await expect
    .poll(() => convos.queries.at(-1))
    .toBe("nursing 2023");
});

test("clearing the search restores the whole list", async ({ page }) => {
  await openChat(page);
  const box = page.getByRole("searchbox", { name: "Search chats and messages" });

  await box.fill("engineering");
  await expect(titles(page)).toHaveText(["Engineering enrollment"]);

  // The in-field clear button, which SearchBox renders only while there is
  // something to clear.
  await page.getByRole("button", { name: "Clear search" }).click();

  await expect(titles(page)).toHaveCount(3);
  await expect(box).toHaveValue("");
});

test("a search with no matches says so, and is not the same as having no chats",
  async ({ page }) => {
    await openChat(page);

    await page.getByRole("searchbox", { name: "Search chats and messages" }).fill("haddock");

    await expect(emptyState(page)).toContainText("No chats match your search.");
    await expect(emptyState(page)).not.toContainText("No chats yet.");
    await expect(titles(page)).toHaveCount(0);
  });

test("a user with no chats at all gets the other empty state", async ({ page }) => {
  await mockMe(page, { email: "user@example.edu", is_admin: false });
  await mockVersion(page);
  await mockConversations(page, []);
  await page.goto("/");

  await expect(emptyState(page)).toHaveText("No chats yet.");
});

test("a failed load says the request failed, never that the history is empty",
  async ({ page }) => {
    // The one wrong answer this list can give: telling someone their chats are
    // gone when the request simply failed.
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockVersion(page);
    await page.route("**/api/chat/conversations*", (route) =>
      route.fulfill({ status: 500, contentType: "application/json",
        body: JSON.stringify({ detail: "nope" }) }));
    await page.goto("/");

    await expect(emptyState(page)).toContainText("Couldn't load your chats.");
    await expect(emptyState(page)).not.toContainText("No chats yet.");
  });

test("no refresh drops the active search, not even a transient one",
  async ({ page }) => {
    // Sending a message re-lists the sidebar, and a refresh that dropped the
    // query would un-filter it underneath a search box still showing the term.
    //
    // Asserting the list at the END is not enough, and this is the trap: a
    // later, correct refresh repairs it, so a refresh that DOES drop the query
    // is invisible by the time the dust settles (measured — the query sequence
    // read ["nursing", "", "nursing"] and every end-state assertion still
    // passed). The invariant is therefore about every request, not the last
    // one: once a search is active, nothing may ask for the unfiltered list.
    const convos = await openChat(page);
    await mockConversation(page, 1, []);
    await mockStreamChat(page, { conversationId: 1, answer: "42" });

    await page.getByRole("searchbox", { name: "Search chats and messages" }).fill("nursing");
    await expect(titles(page)).toHaveCount(2);
    const firstSearch = convos.queries.indexOf("nursing");
    expect(firstSearch).toBeGreaterThanOrEqual(0);

    await page.getByRole("textbox", { name: /Ask/i }).fill("another nursing question");
    await page.getByRole("button", { name: /^Send/ }).click();
    await expect(page.getByText("42")).toBeVisible();

    // Wait for the send's own re-list to have happened before judging the
    // sequence, or this passes on a sequence that has not been written yet.
    await expect.poll(() => convos.queries.length).toBeGreaterThan(firstSearch + 1);
    expect(convos.queries.slice(firstSearch)).toEqual(
      convos.queries.slice(firstSearch).map(() => "nursing"),
    );
    await expect(titles(page)).toHaveCount(2);
  });

test("the sidebar search box is chromed like every other one", async ({ page }) => {
  // SearchBox is shared with the admin screens, and its field chrome used to
  // come from `.row input` — the container every admin search happens to sit
  // in. Rendered anywhere else (here) it fell back to a bare browser input that
  // visibly did not belong. The chrome now lives on `.searchwrap .logsearch`,
  // i.e. on the component, and this pins that: the two must COMPUTE the same,
  // which no unit test can see and only an eye would otherwise catch.
  const chrome = (el) => {
    const s = globalThis.getComputedStyle(el);
    return [s.borderTopWidth, s.borderTopStyle, s.borderTopColor, s.borderRadius,
            s.paddingTop, s.paddingLeft, s.paddingRight, s.backgroundColor,
            s.color].join("|");
  };

  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockVersion(page);
  await mockConversations(page, CONVOS);
  await mockAllowlist(page, [{ email: "a@example.edu", is_admin: 0, added_at: 1 }]);
  await mockAccessRequests(page, []);
  await mockDeniedRequests(page, []);

  await page.goto("/");
  const sidebar = await page.getByRole("searchbox", { name: "Search chats and messages" })
    .evaluate(chrome);

  await page.goto("/admin/users");
  const admin = await page.getByRole("searchbox").first().evaluate(chrome);

  expect(sidebar).toBe(admin);
});

test("a turn that lands AFTER you start searching does not wipe the search",
  async ({ page }) => {
    // The order matters and is the whole test. submit() awaits the stream, so
    // the refresh it runs when the answer lands executes in the closure
    // captured when Send was CLICKED. Reading the query from that closure gives
    // whatever was in the box a minute ago — normally "". Measured before the
    // fix: the sequence read ["", "", "nursing", ""], the box still said
    // nursing, and all three chats were back. The earlier spec in this file
    // cannot see it, because it searches BEFORE sending, which is the one
    // ordering where the closure is fresh.
    // Set up BEFORE navigating: mockStreamChatDripped installs via
    // addInitScript, which only applies to loads that happen after it.
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockVersion(page);
    const convos = await mockConversations(page, CONVOS);
    await mockConversation(page, 1, []);
    await mockStreamChatDripped(page, [
      { atMs: 0, event: { type: "conversation", id: 1 } },
      { atMs: 50, event: { type: "status", text: "Thinking…" } },
      { atMs: 1200, event: { type: "answer", text: "42" } },
      { atMs: 1300, event: { type: "done", result: { answer: "42", model_used: "m", sql_log: [] } } },
    ]);
    await page.goto("/");
    await expect(titles(page)).toHaveCount(3);

    await page.getByRole("textbox", { name: /Ask/i }).fill("a question");
    await page.getByRole("button", { name: /^Send/ }).click();
    // Type the search WHILE the turn is still streaming.
    await page.getByRole("searchbox", { name: "Search chats and messages" }).fill("nursing");
    await expect(titles(page)).toHaveCount(2);

    await expect(page.getByText("42")).toBeVisible();
    await expect.poll(() => convos.queries.at(-1)).toBe("nursing");
    // Ordering, not counting: the mount load and the mid-stream re-list both
    // legitimately ask for "" — they happen before anything is typed. What must
    // not happen is an unfiltered request AFTER the search begins, which is
    // exactly what the stale closure produced.
    expect(convos.queries.lastIndexOf("")).toBeLessThan(convos.queries.indexOf("nursing"));
    await expect(titles(page)).toHaveCount(2);
  });

test("the first paint does not claim a returning user has no chats",
  async ({ page }) => {
    // `convos` starts empty with an empty query, which is exactly the
    // "no history" branch — so this told everyone with a full sidebar that they
    // had never used the app, for the length of the round trip. Slowest for the
    // people least able to shrug it off.
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockVersion(page);
    await page.route("**/api/chat/conversations*", async (route) => {
      await new Promise((r) => setTimeout(r, 600));
      await route.fulfill({ status: 200, contentType: "application/json",
        body: JSON.stringify(CONVOS) });
    });

    await page.goto("/");
    // NOT an auto-retrying matcher. `toHaveCount(0)` would simply wait out the
    // 600ms and pass once the data landed — it cannot assert "this was never
    // true", which is the whole claim here. A plain count(), read while the
    // first request is still in flight, can.
    expect(await emptyState(page).count()).toBe(0);
    // And the list does arrive, so the assertion above was not passing merely
    // because the page failed to load.
    await expect(titles(page)).toHaveCount(3);
  });

test("clearing a no-match search does not flash the no-history state",
  async ({ page }) => {
    // Clearing empties the box synchronously while the refetch sits behind the
    // 250ms debounce, so branching the empty states on the INPUT rendered
    // "No chats yet." to someone with history for that window.
    await openChat(page);
    await page.getByRole("searchbox", { name: "Search chats and messages" }).fill("haddock");
    await expect(emptyState(page)).toContainText("No chats match your search.");

    await page.getByRole("button", { name: "Clear search" }).click();

    // Immediately, with no wait: the state must never have been rendered.
    expect(await emptyState(page).filter({ hasText: "No chats yet." }).count()).toBe(0);
    await expect(titles(page)).toHaveCount(3);
  });

test("a search announces its outcome, and a repeat failure announces again",
  async ({ page }) => {
    // The live region is always mounted and fed only when a SEARCH commits: a
    // region inserted with its text already in it is announced unreliably, and
    // a delete already announces through the toast.
    let fail = false;
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockVersion(page);
    await page.route("**/api/chat/conversations*", async (route) => {
      const q = new URL(route.request().url()).searchParams.get("q") || "";
      if (fail) {
        return route.fulfill({ status: 500, contentType: "application/json",
          body: JSON.stringify({ detail: "nope" }) });
      }
      const rows = q ? CONVOS.filter((c) => c.title.toLowerCase().includes(q.toLowerCase())) : CONVOS;
      return route.fulfill({ status: 200, contentType: "application/json",
        body: JSON.stringify(rows) });
    });
    await page.goto("/");
    const live = liveRegion(page);

    // Mounted before any search — the region must EXIST while empty, or its
    // first message is inserted along with it and announced unreliably.
    await expect(titles(page)).toHaveCount(3);
    await expect(live).toHaveCount(1);
    await expect(live).toHaveText("");

    const box = page.getByRole("searchbox", { name: "Search chats and messages" });
    await box.fill("nursing");
    await expect(live).toHaveText("2 chats match your search.");

    await box.fill("haddock");
    await expect(live).toHaveText("No chats match your search.");

    fail = true;
    await box.fill("engineering");
    await expect(live).toHaveText("Couldn't search your chats.");
    // The visible copy says SEARCH failed, not "load", and offers a real
    // control rather than the words "Try again." with nothing to press.
    await expect(emptyState(page)).toContainText("Couldn't search your chats.");
    await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
  });

test("a failed search does not leave the old list under the error", async ({ page }) => {
  // Otherwise the sidebar says the search failed AND shows rows that match
  // neither the server nor the box — and the rows stay clickable, so the error
  // reads as a lie.
  await openChat(page);
  await expect(titles(page)).toHaveCount(3);

  await page.route("**/api/chat/conversations*", (route) =>
    route.fulfill({ status: 500, contentType: "application/json",
      body: JSON.stringify({ detail: "nope" }) }));
  await page.getByRole("searchbox", { name: "Search chats and messages" }).fill("nursing");

  await expect(emptyState(page)).toContainText("Couldn't search your chats.");
  await expect(titles(page)).toHaveCount(0);
});

test("New chat ends the search", async ({ page }) => {
  // The chat about to be created almost certainly does not contain the old
  // terms, so it would never appear in a sidebar still filtered by them.
  await openChat(page);
  const box = page.getByRole("searchbox", { name: "Search chats and messages" });
  await box.fill("nursing");
  await expect(titles(page)).toHaveCount(2);

  await page.getByRole("link", { name: "New chat" }).click();

  await expect(box).toHaveValue("");
  await expect(titles(page)).toHaveCount(3);
});
