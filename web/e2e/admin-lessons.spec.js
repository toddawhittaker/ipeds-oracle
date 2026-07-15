import { test, expect } from "@playwright/test";
import { mockMe, mockConversations, mockSkills } from "./mocks.js";

// The redesigned Skills → "Learned lessons" admin view: the human-readable RULE
// leads, the SQL is tucked into a collapsible example (no raw SQL up front), the
// source is shown, and an unverified critic-proposed lesson can be approved.
test("lessons view leads with the rule, hides SQL in a collapsible example, verifies", async ({ page }) => {
  await mockMe(page, { email: "admin@franklin.edu", is_admin: true });
  await mockConversations(page, []);
  await mockSkills(page, [
    {
      id: 7,
      question: "national total of bachelor's degrees",
      lesson: "Add majornum=1 — summing c_a without it double-counts second majors.",
      canonical_sql: "SELECT SUM(ctotalt) FROM c_a WHERE cipcode='99'",
      notes: "",
      verified: false,
      created_by: "critic",
      upvotes: 0,
      downvotes: 0,
      hits: 0,
    },
  ]);

  // PATCH/DELETE hit /api/admin/skills/{id}, which mockSkills (GET-only) doesn't cover.
  await page.route("**/api/admin/skills/*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' }));

  await page.goto("/");
  await page.getByRole("button", { name: "Admin" }).click();
  await page.getByRole("button", { name: "Skills" }).click();

  // The rule is front-and-center; the source and unverified state are shown.
  await expect(page.getByText(/Add majornum=1/)).toBeVisible();
  await expect(page.getByText("from critic")).toBeVisible();
  await expect(page.locator("span.tag.warn")).toHaveText("unverified");

  // The SQL is NOT shown up front — it lives inside a collapsed "Example query".
  await expect(page.getByText("SELECT SUM(ctotalt)")).toBeHidden();
  await page.getByText("Example query").click();
  await expect(page.getByText("SELECT SUM(ctotalt)")).toBeVisible();

  // Approving the lesson PATCHes verified=true.
  const patch = page.waitForRequest(
    (r) => r.url().includes("/api/admin/skills/7") && r.method() === "PATCH");
  await page.getByRole("button", { name: "verify" }).click();
  const body = (await patch).postDataJSON();
  expect(body).toMatchObject({ verified: true });
});
