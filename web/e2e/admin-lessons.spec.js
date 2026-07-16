import { test, expect } from "@playwright/test";
import { mockMe, mockConversations, mockSkills } from "./mocks.js";

// The redesigned Skills → "Learned lessons" admin view: a short generalized
// HEADLINE leads, a longer generalized DESCRIPTION is tucked into its own
// collapsible "Details" (not dumped as a wall of text), the SQL worked
// example stays collapsed under its own "Example query", the source is
// shown, and an unverified critic-proposed lesson can be approved.
test("lessons view leads with the headline, collapses description and SQL separately, verifies", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockSkills(page, [
    {
      id: 7,
      question: "national total of bachelor's degrees",
      headline: "Add majornum=1 for every completions total.",
      lesson: "Summing c_a without majornum=1 double-counts a student's declared "
        + "second major; add the filter to any total or grouped SUM over ctotalt.",
      canonical_sql: "SELECT SUM(ctotalt) FROM c_a WHERE cipcode='99' -- grand total\n"
        + "AND majornum=1; -- don't double-count second majors",
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

  // The headline is front-and-center; the source and unverified state are shown.
  await expect(page.getByText("Add majornum=1 for every completions total.")).toBeVisible();
  await expect(page.getByText("from critic")).toBeVisible();
  await expect(page.locator("span.tag.warn")).toHaveText("unverified");

  // The longer description is NOT shown up front — it lives inside a
  // collapsed "Details" <details>, separate from the SQL example.
  const descriptionText = "double-counts a student's declared";
  await expect(page.getByText(new RegExp(descriptionText))).toBeHidden();
  await page.getByText("Details").click();
  await expect(page.getByText(new RegExp(descriptionText))).toBeVisible();

  // The SQL is NOT shown up front either — it lives inside a collapsed
  // "Example query", independent of the description's <details>.
  await expect(page.getByText("SELECT SUM(ctotalt)")).toBeHidden();
  await page.getByText("Example query").click();
  await expect(page.getByText("SELECT SUM(ctotalt)")).toBeVisible();

  // Approving the lesson PATCHes verified=true. The button's accessible name is
  // per-lesson (includes the headline) so screen-reader/voice users can
  // disambiguate.
  const patch = page.waitForRequest(
    (r) => r.url().includes("/api/admin/skills/7") && r.method() === "PATCH");
  await page.getByRole("button", { name: /Verify lesson:/ }).click();
  const body = (await patch).postDataJSON();
  expect(body).toMatchObject({ verified: true });
});

test("rejecting a verified lesson asks for confirmation before deleting", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockSkills(page, [
    {
      id: 3, question: "q", headline: "Curated verified headline.",
      lesson: "Curated verified rule, spelled out at length for the admin to read.",
      canonical_sql: "SELECT 1 -- example", notes: "", verified: true,
      created_by: "seed", upvotes: 3, downvotes: 0, hits: 9,
    },
  ]);
  await page.route("**/api/admin/skills/*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' }));

  await page.goto("/");
  await page.getByRole("button", { name: "Admin" }).click();
  await page.getByRole("button", { name: "Skills" }).click();

  // Dismissing the confirm must NOT fire a DELETE.
  let deleted = false;
  page.on("request", (r) => {
    if (r.method() === "DELETE" && r.url().includes("/api/admin/skills/3")) deleted = true;
  });
  page.once("dialog", (d) => {
    expect(d.message()).toMatch(/can't be undone/i);
    d.dismiss();
  });
  await page.getByRole("button", { name: /Reject lesson:/ }).click();
  await page.waitForTimeout(200);
  expect(deleted).toBe(false);
});
