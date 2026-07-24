import { test, expect } from "@playwright/test";
import {
  mockMe, mockConversations, mockAttention, mockAllowlist,
  mockAccessRequests, mockDeniedRequests, mockVersion,
} from "./mocks.js";

// The Admin update banner shows ONLY when a newer release is available (nothing
// when up to date). NON-dismissible on purpose — like every other attention
// item, it persists until acted on (the deployment is updated).

async function admin(page, version) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAttention(page, { users: 0, skills: 0, logs: 0 });
  await mockAllowlist(page, []);
  await mockAccessRequests(page, []);
  await mockDeniedRequests(page, []);
  await mockVersion(page, version);
  await page.goto("/admin/users/current");
  await expect(page.getByRole("tablist", { name: "User management" })).toBeVisible();
}

test("no banner when up to date", async ({ page }) => {
  await admin(page, { current: "0.1.0", latest: "0.1.0", update_available: false });
  await expect(page.getByText(/is available/)).toHaveCount(0);
});

test("banner appears when an update exists, and is not dismissible", async ({ page }) => {
  await admin(page, { current: "0.1.0", latest: "0.2.0", update_available: true });

  const banner = page.locator(".update-banner");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("v0.2.0 is available");
  await expect(banner).toContainText("0.1.0");
  await expect(banner.getByRole("link", { name: "Release notes" }))
    .toHaveAttribute("href", "https://github.com/toddawhittaker/ipeds-oracle/releases");
  // No dismiss — it persists until the deployment is updated (attention-item model).
  await expect(banner.getByRole("button")).toHaveCount(0);
});
