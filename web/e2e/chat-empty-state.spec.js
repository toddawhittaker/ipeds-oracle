import { test, expect } from "@playwright/test";
import { mockMe, mockConversations } from "./mocks.js";

// The empty chat screen: the DeepSeek/no-proprietary-data warning, clickable
// example prompts that fill the composer, and the keyboard-resizable sidebar.
test("empty chat: privacy warning, example chips fill the composer, sidebar resizes", async ({ page }) => {
  await mockMe(page, { email: "user@franklin.edu", is_admin: false });
  await mockConversations(page, []);
  await page.goto("/");

  // The warning must call out proprietary Franklin data + DeepSeek training.
  await expect(
    page.getByText(/Do not enter proprietary or confidential Franklin/i),
  ).toBeVisible();
  await expect(
    page.getByText(/DeepSeek, which may train on the data you submit/i),
  ).toBeVisible();

  // Clicking an example prompt drops it into the composer for review.
  const example = page.getByRole("button", { name: /Registered Nursing/i }).first();
  await expect(example).toBeVisible();
  await example.click();
  await expect(page.getByPlaceholder("Ask about IPEDS data…")).toHaveValue(
    /Registered Nursing/,
  );

  // The sidebar is a keyboard-resizable separator: ArrowRight widens it.
  const sep = page.getByRole("separator", { name: "Resize sidebar" });
  const before = Number(await sep.getAttribute("aria-valuenow"));
  await sep.focus();
  await page.keyboard.press("ArrowRight");
  const after = Number(await sep.getAttribute("aria-valuenow"));
  expect(after).toBeGreaterThan(before);
});
