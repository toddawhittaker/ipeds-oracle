import { test, expect } from "@playwright/test";
import { mockMe, mockConversations, mockStreamChat, mockConversation } from "./mocks.js";

// The disambiguation "clarify" turn: when a request is materially ambiguous, the
// backend asks ONE short clarifying question with 2-4 answer-phrase chips instead
// of guessing a scope (backend/app/llm.py _extract_clarify -> a `clarify` SSE
// event, mirroring figure/followups — see docs/plan "happy-squishing-kahn").
// Browser truth pinned here: the clarifying question's prose + its chips render,
// clicking a chip submits the short phrase as an ordinary follow-up turn, the
// free-text composer is ALWAYS still usable as an escape hatch (a TESTED
// requirement per the plan), a clarify turn survives a reload like figure/
// suggestions, and a normal (non-ambiguous) answer shows no clarify chips at all.
// The pure normalizer is unit-tested in src/clarify.test.js.
//
// Contract choice made here (not pinned verbatim in the plan): the chip group
// carries the accessible name "Did you mean" (the plan's own example wording for
// the component's "distinct label"), mirroring Suggestions.jsx's
// `role="group" aria-label="Suggested follow-up questions"` pattern.

const CLARIFY = {
  question: "Do you mean bachelor's degrees only, or all award levels?",
  options: ["Bachelor's only", "Include all levels"],
};
const CLARIFY_ANSWER = CLARIFY.question;
const AMBIGUOUS_Q = "which undergraduate major produces the most graduates?";

async function signedIn(page) {
  await mockMe(page, { email: "u@example.edu", is_admin: false, trust_llm_provider: true });
  await mockConversations(page, []);
}

test("a clarify event renders the question prose and answer-phrase chips", async ({ page }) => {
  await signedIn(page);
  await mockStreamChat(page, {
    conversationId: 30, answer: CLARIFY_ANSWER, clarify: CLARIFY,
    messageId: 1, userMessageId: 2 });
  await page.goto("/");
  await page.getByPlaceholder("Ask about IPEDS data…").fill(AMBIGUOUS_Q);
  await page.getByRole("button", { name: "Send" }).click();

  // The clarifying question rides in the assistant bubble prose.
  await expect(page.getByText(CLARIFY.question)).toBeVisible();

  const group = page.getByRole("group", { name: "Did you mean" });
  await expect(group).toBeVisible();
  for (const opt of CLARIFY.options) {
    await expect(group.getByRole("button", { name: opt })).toBeVisible();
  }
});

test("clicking a chip submits its short phrase as an ordinary follow-up turn", async ({ page }) => {
  await signedIn(page);
  const chat = await mockStreamChat(page, {
    conversationId: 31, answer: CLARIFY_ANSWER, clarify: CLARIFY,
    messageId: 1, userMessageId: 2 });
  await page.goto("/");
  await page.getByPlaceholder("Ask about IPEDS data…").fill(AMBIGUOUS_Q);
  await page.getByRole("button", { name: "Send" }).click();

  const group = page.getByRole("group", { name: "Did you mean" });
  await group.getByRole("button", { name: "Bachelor's only" }).click();

  await expect.poll(() => chat.calls.length).toBe(2);
  expect(chat.calls[1].question).toBe("Bachelor's only");
});

test("the free-text composer stays a working escape hatch on a clarify turn", async ({ page }) => {
  // Regression this test exists to catch: chips must never disable or hijack the
  // composer -- the user can ignore them entirely and type their own reply, which
  // must resolve exactly like clicking a chip (an ordinary follow-up POST).
  await signedIn(page);
  const chat = await mockStreamChat(page, {
    conversationId: 32, answer: CLARIFY_ANSWER, clarify: CLARIFY,
    messageId: 1, userMessageId: 2 });
  await page.goto("/");
  await page.getByPlaceholder("Ask about IPEDS data…").fill(AMBIGUOUS_Q);
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("group", { name: "Did you mean" })).toBeVisible();

  const composer = page.getByPlaceholder("Ask about IPEDS data…");
  await expect(composer).toBeEnabled();
  await composer.fill("Just bachelor's, thanks");
  await page.getByRole("button", { name: "Send" }).click();

  await expect.poll(() => chat.calls.length).toBe(2);
  expect(chat.calls[1].question).toBe("Just bachelor's, thanks");
});

test("a clarify turn survives a reload (persisted like the figure/suggestions)", async ({ page }) => {
  await signedIn(page);
  await mockConversation(page, 33, [
    { role: "user", content: AMBIGUOUS_Q },
    { role: "assistant", content: CLARIFY_ANSWER, clarify: CLARIFY },
  ]);
  await page.goto("/chat/33");

  const group = page.getByRole("group", { name: "Did you mean" });
  await expect(group).toBeVisible();
  await expect(group.getByRole("button", { name: "Include all levels" })).toBeVisible();
});

test("a normal (non-ambiguous) answer shows no clarify chips", async ({ page }) => {
  await signedIn(page);
  await mockStreamChat(page, {
    conversationId: 34, answer: "California publics awarded 7,679 CS bachelor's degrees.",
    messageId: 1, userMessageId: 2 });
  await page.goto("/");
  await page.getByPlaceholder("Ask about IPEDS data…").fill("how many CS degrees in CA?");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.getByText("7,679")).toBeVisible();
  await expect(page.getByRole("group", { name: "Did you mean" })).toHaveCount(0);
});
