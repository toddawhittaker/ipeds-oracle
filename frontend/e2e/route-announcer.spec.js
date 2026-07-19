import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockAllowlist,
  mockAccessRequests,
  mockDeniedRequests,
} from "./mocks.js";

// A11y (code review MEDIUM): a visually-hidden route announcer, keyed on
// pathname, using the same always-mounted mechanic as Admin.jsx's flash
// live region (Admin.jsx:258) and the Chat bad-conversation notice fix (see
// routing-chat.spec.js) -- populated via an effect, never a brand-new node.
// It closes three silent-navigation gaps: swapping Chat<->Admin's main
// content announces nothing to a screen reader; the /admin -> /admin/users
// redirect is silent; and a non-admin's /admin/x -> / bounce (route-guards)
// is silent too.
//
// Contract: an always-mounted `data-testid="route-announcer"` node (a
// dedicated test id, not a CSS class/role, because role="status" is already
// used by several OTHER live regions on these pages -- Admin's flash box,
// Skills' status region, Chat's bad-conversation notice -- and a bare
// getByRole("status") would ambiguously match more than one of them).
// Wording is intentionally left up to the implementer: assertions here check
// non-empty + a key substring, not a full string match.

test.describe("route announcer", () => {
  test("navigating Chat -> Admin populates the route announcer", async ({ page }) => {
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockConversations(page, []);
    await mockAllowlist(page, []);
    await mockAccessRequests(page, []);
    await mockDeniedRequests(page, []);

    await page.goto("/");
    const announcer = page.getByTestId("route-announcer");
    await expect(announcer).toBeAttached();

    await page.getByRole("link", { name: "Admin", exact: true }).click();
    await expect.poll(() => new URL(page.url()).pathname).toBe("/admin/users/current");

    await expect(announcer).not.toHaveText("");
    await expect(announcer).toContainText(/admin/i);
  });

  test("a non-admin bouncing off /admin/users to / announces the landing", async ({ page }) => {
    await mockMe(page, { email: "user@example.edu", is_admin: false });
    await mockConversations(page, []);

    await page.goto("/admin/users");

    await expect.poll(() => new URL(page.url()).pathname).toBe("/");
    const announcer = page.getByTestId("route-announcer");
    await expect(announcer).not.toHaveText("");
    await expect(announcer).toContainText(/chat/i);
  });
});
