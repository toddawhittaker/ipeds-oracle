import { expect, test } from "@playwright/test";

import {
  mockAttention,
  mockConversations,
  mockMe,
  mockStreamChatError,
  mockVersion,
} from "./mocks.js";

// Browser truth for the "failures are visible, and are never raw" pass.
//
// The three states these pin all used to be SILENT or hostile: an expired
// session left the shell rendered and inert, a rate-limit reached the user as
// literal JSON braces in the answer bubble, and a failed Logs load rendered
// "No log records." to the one person whose job was to find out whether
// anything was wrong.

const USER = { email: "user@example.edu", is_admin: false };
const ADMIN = { email: "admin@example.edu", is_admin: true };

test.describe("session expiry mid-session", () => {
  test("a 401 from any request shows the login door with an explanation, "
    + "not a silently inert shell", async ({ page }) => {
    // The shell loads signed in, then the cookie dies under it — the way a
    // month-old session expires while a tab sits open. `alive` flips when the
    // stream 401s, so the app's confirming /auth/me check AGREES (one endpoint's
    // 401 is deliberately not enough on its own to sign anyone out).
    let alive = true;
    await page.route("**/api/auth/me", async (route) => {
      await route.fulfill(alive
        ? { status: 200, contentType: "application/json",
            body: JSON.stringify({ has_data: true, ...USER }) }
        : { status: 401, contentType: "application/json",
            body: JSON.stringify({ detail: "Not signed in." }) });
    });
    await mockVersion(page);
    await mockAttention(page);
    await mockConversations(page, []);
    await page.route("**/api/chat/stream", async (route) => {
      alive = false;   // the session is gone as of this request
      await route.fulfill({
        status: 401, contentType: "application/json",
        body: JSON.stringify({ detail: "Not signed in." }),
      });
    });
    await page.goto("/");
    await page.getByPlaceholder("Ask about IPEDS data…").fill("anything at all");
    await page.getByRole("button", { name: "Send" }).click();

    // The door, with a reason — previously the app just sat there.
    await expect(page.getByRole("alert")).toContainText(/session expired/i);
    await expect(page.getByRole("alert")).toContainText(/signed?.?in|sign in/i);
    // And it reassures rather than implying data loss.
    await expect(page.getByRole("alert")).toContainText(/saved/i);
  });

  test("a server error is NOT reported as an expired session", async ({ page }) => {
    // Different problem, different fix: telling someone to sign in again when
    // the backend is down wastes their time.
    await page.route("**/api/auth/me", async (route) => {
      await route.fulfill({
        status: 500, contentType: "application/json",
        body: JSON.stringify({ detail: "boom" }),
      });
    });
    await page.goto("/");
    const alert = page.getByRole("alert");
    await expect(alert).toContainText(/couldn't reach|connection/i);
    await expect(alert).not.toContainText(/expired/i);
  });
});

test.describe("a failed turn reads as a condition, not as raw JSON", () => {
  test("a 429 renders human copy with no JSON braces, and stays retryable",
    async ({ page }) => {
      await mockMe(page, USER);
      await mockVersion(page);
      await mockAttention(page);
      await mockConversations(page, []);
      await mockStreamChatError(page, {
        httpStatus: 429,
        detail: "Too many requests — please slow down and try again in a moment.",
      });
      await page.goto("/");
      await page.getByPlaceholder("Ask about IPEDS data…").fill("how many nursing degrees?");
      await page.getByRole("button", { name: "Send" }).click();

      const thread = page.locator(".msg.assistant").last();
      await expect(thread).toContainText(/faster than the assistant/i);
      // THE REGRESSION: the raw body used to land here verbatim.
      await expect(thread).not.toContainText("{");
      await expect(thread).not.toContainText("detail");
      // A failed turn is visually distinct from an answer, and recoverable.
      await expect(page.locator(".msg.assistant.failed")).toHaveCount(1);
      await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
    });
});

test.describe("admin panels don't report a failure as emptiness", () => {
  test("a failed Logs load says so, instead of 'No log records.'", async ({ page }) => {
    await mockMe(page, ADMIN);
    await mockVersion(page);
    await mockAttention(page);
    await page.route("**/api/admin/logs**", async (route) => {
      await route.fulfill({
        status: 500, contentType: "application/json",
        body: JSON.stringify({ detail: "logs.db is locked" }),
      });
    });
    await page.goto("/admin/logs");

    const alert = page.getByRole("alert");
    await expect(alert).toBeVisible();
    // The server's own sentence beats a generic apology when there is one.
    await expect(alert).toContainText(/locked/i);
    // And the confident lie is gone.
    await expect(page.getByText("No log records.")).toHaveCount(0);
  });
});
