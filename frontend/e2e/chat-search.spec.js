import { test, expect } from "@playwright/test";
import { mockConversation, mockConversations, mockMe, mockStreamChat, mockVersion } from "./mocks.js";

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

test("the sidebar search sends the query and narrows the list", async ({ page }) => {
  const convos = await openChat(page);

  await page.getByRole("searchbox", { name: "Search your chats" }).fill("nursing 2023");

  await expect(titles(page)).toHaveText(["Nursing completions 2023"]);
  // Sent to the SERVER, not filtered from the list already loaded — the text
  // being searched lives in messages the browser has never fetched.
  await expect
    .poll(() => convos.queries.at(-1))
    .toBe("nursing 2023");
});

test("clearing the search restores the whole list", async ({ page }) => {
  await openChat(page);
  const box = page.getByRole("searchbox", { name: "Search your chats" });

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

    await page.getByRole("searchbox", { name: "Search your chats" }).fill("haddock");

    await expect(page.getByText("No chats match your search.")).toBeVisible();
    await expect(page.getByText("No chats yet.")).toHaveCount(0);
    await expect(titles(page)).toHaveCount(0);
  });

test("a user with no chats at all gets the other empty state", async ({ page }) => {
  await mockMe(page, { email: "user@example.edu", is_admin: false });
  await mockVersion(page);
  await mockConversations(page, []);
  await page.goto("/");

  await expect(page.getByText("No chats yet.")).toBeVisible();
  await expect(page.getByText("No chats match your search.")).toHaveCount(0);
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

    await expect(page.getByRole("alert")).toContainText("Couldn't load your chats");
    await expect(page.getByText("No chats yet.")).toHaveCount(0);
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

    await page.getByRole("searchbox", { name: "Search your chats" }).fill("nursing");
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
