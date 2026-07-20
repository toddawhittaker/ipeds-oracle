import { test, expect } from "@playwright/test";
import { mockMe, mockConversations, mockConversation, mockStreamChat } from "./mocks.js";

// The Markdown-highlighting composer stays a REAL <textarea> whose value is the
// plain Markdown source, layered over a colored <pre> mirror. Browser truth: the
// highlight renders, the source is preserved character-for-character (the `---` ->
// `--` case), and the chat keyboard contract (Enter sends, Shift+Enter newlines,
// empty guard) still holds. The tokenizer itself is unit-tested in mdhighlight.test.js.

async function signedIn(page) {
  await mockMe(page, { email: "u@example.edu", is_admin: false, trust_llm_provider: true });
  await mockConversations(page, []);
}
const composer = (page) => page.getByPlaceholder("Ask about IPEDS data…");

test("highlights Markdown while keeping the textarea value as plain source", async ({ page }) => {
  await signedIn(page);
  await page.goto("/");
  const ta = composer(page);
  await ta.fill("**bold** and `code`");

  // The value is the raw Markdown, unchanged.
  await expect(ta).toHaveValue("**bold** and `code`");
  // The colored mirror renders the tokens...
  const hl = page.locator(".md-editor-hl");
  await expect(hl.locator(".md-hl-strong")).toHaveText("bold");
  await expect(hl.locator(".md-hl-code")).toHaveText("code");
  // ...and it's hidden from assistive tech (the textarea is the real control).
  await expect(hl).toHaveAttribute("aria-hidden", "true");
  await expect(page.getByRole("textbox")).toBeVisible();
});

test("character-level: Backspace after --- reveals -- (source, not a rendered rule)", async ({ page }) => {
  await signedIn(page);
  await page.goto("/");
  const ta = composer(page);
  await ta.fill("---");
  await expect(page.locator(".md-editor-hl .md-hl-hr")).toHaveText("---");

  await ta.press("Backspace");
  await expect(ta).toHaveValue("--");
  // '--' is not an HR, so the rule styling dissolves — the remaining source shows.
  await expect(page.locator(".md-editor-hl .md-hl-hr")).toHaveCount(0);
});

test("Enter sends the plain Markdown; Shift+Enter inserts a newline without sending", async ({ page }) => {
  await signedIn(page);
  const stream = await mockStreamChat(page, { conversationId: 7, answer: "ok" });
  await page.goto("/");
  const ta = composer(page);

  // Shift+Enter builds a multi-line draft, no send.
  await ta.click();
  await ta.pressSequentially("line one");
  await ta.press("Shift+Enter");
  await ta.pressSequentially("line two");
  await expect(ta).toHaveValue("line one\nline two");
  expect(stream.calls.length).toBe(0);

  // Enter sends the canonical Markdown and clears the composer.
  await ta.press("Enter");
  await expect.poll(() => stream.calls.length).toBe(1);
  expect(stream.calls[0].question).toBe("line one\nline two");
  await expect(ta).toHaveValue("");
});

test("the inline prompt-edit box highlights Markdown too, keeping the plain source", async ({ page }) => {
  await signedIn(page);
  const md = "# Results\n\n- one\n\n**bold** and `code`";
  await mockConversation(page, 5, [
    { role: "user", content: md },
    { role: "assistant", content: "an answer" },
  ]);
  await page.goto("/chat/5");

  // Open the edit box on the user prompt (the button's accessible name is "Edit").
  await page.getByRole("button", { name: "Edit", exact: true }).first().click();
  const editHl = page.locator(".edit-box .md-editor-hl");
  const editTa = page.locator(".edit-box .md-editor-ta");
  // Same overlay + tokenizer as the composer: highlighted, source preserved.
  await expect(editHl.locator(".md-hl-strong")).toHaveText("bold");
  await expect(editHl.locator(".md-hl-code")).toHaveText("code");
  await expect(editTa).toHaveValue(md);
});

test("Send is disabled for empty or whitespace-only input", async ({ page }) => {
  await signedIn(page);
  await page.goto("/");
  const send = page.getByRole("button", { name: "Send" });
  await expect(send).toBeDisabled();
  await composer(page).fill("   \n  ");
  await expect(send).toBeDisabled();
  await composer(page).fill("real question");
  await expect(send).toBeEnabled();
});
