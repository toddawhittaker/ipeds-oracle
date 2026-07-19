import { test, expect } from "@playwright/test";
import { mockMe, mockConversations } from "./mocks.js";

// The empty chat screen: the (generic, deployment-agnostic) third-party-LLM
// privacy warning, clickable example prompts that fill the composer, and the
// keyboard-resizable sidebar.
test("empty chat: privacy warning, example chips fill the composer, sidebar resizes", async ({ page }) => {
  await mockMe(page, { email: "user@example.edu", is_admin: false });
  await mockConversations(page, []);
  await page.goto("/");

  // The warning must call out confidential/non-public info and the third-party
  // model, without hardcoding the institution's name or "DeepSeek" or any
  // cost-savings framing. (Redesign: it's now a quiet margin note, not a box —
  // same substance, calmer treatment.)
  await expect(
    page.getByText(/no student records, confidential figures, or other non-public information/i),
  ).toBeVisible();
  await expect(
    page.getByText(/third-party model that may use them to improve its service/i),
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

// TRUST_LLM_PROVIDER=true (resolved to me.trust_llm_provider) suppresses the
// warning entirely — icon and text gone, and nothing else about the empty
// screen changes (examples still there, no reserved gap where it used to be).
test("trusted provider: privacy warning is absent, examples remain", async ({ page }) => {
  await mockMe(page, { email: "user@example.edu", is_admin: false, trust_llm_provider: true });
  await mockConversations(page, []);
  await page.goto("/");

  // The example chips still render — the trusted flag hides ONLY the warning.
  await expect(
    page.getByRole("button", { name: /Registered Nursing/i }).first(),
  ).toBeVisible();

  // No part of the warning survives: neither its text nor its container.
  await expect(
    page.getByText(/no student records, confidential figures/i),
  ).toHaveCount(0);
  await expect(page.locator(".privacy-warning")).toHaveCount(0);
});
