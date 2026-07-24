import { test, expect } from "@playwright/test";
import { mockMe, mockConversations, mockConversation } from "./mocks.js";

// Compare mode: pick 2+ rows from a categorical result table and instantly chart just
// those rows from the numbers already in the table (no new query). Browser truth —
// the sortable result table (SortableTable in Markdown.jsx) renders the leading
// checkbox column inline for a comparable table, and selection drives an inline
// snapshot <Chart>. The pure gate/spec logic is unit-tested in src/compare.test.js;
// this pins the DOM flow and the comparability gate.

// A categorical ranking table (one row per university + a numeric metric) — comparable.
const RANKING = "Largest public universities:\n\n"
  + "| Rank | University | Enrollment |\n|---|---|---|\n"
  + "| 1 | Ohio State | 60,000 |\n| 2 | Michigan | 50,000 |\n| 3 | Penn State | 48,000 |\n";

// A year-over-year table — a trend, NOT an entity comparison. Must offer no checkboxes.
const TIMESERIES = "Degrees by year:\n\n"
  + "| Year | Degrees |\n|---|---|\n| 2020 | 100 |\n| 2021 | 120 |\n| 2022 | 140 |\n";

async function signedIn(page) {
  await mockMe(page, { email: "u@example.edu", is_admin: false, trust_llm_provider: true });
  await mockConversations(page, []);
}

test("select rows from a ranking table and compare them in an instant chart", async ({ page }) => {
  await signedIn(page);
  await mockConversation(page, 9, [
    { role: "user", content: "largest public universities?" },
    { role: "assistant", content: RANKING },
  ]);
  await page.goto("/chat/9");

  // A checkbox per body row (3), none on the header row.
  await expect(page.getByRole("checkbox")).toHaveCount(3);

  // Pick two; the compare bar appears with a live count and an enabled action.
  await page.getByRole("checkbox", { name: "Compare Ohio State" }).check();
  await page.getByRole("checkbox", { name: "Compare Penn State" }).check();
  await expect(page.getByText("2 selected")).toBeVisible();
  const compareBtn = page.getByRole("button", { name: /Compare 2/ });
  await expect(compareBtn).toBeEnabled();
  await compareBtn.click();

  // The snapshot chart renders in its panel with only the two chosen entities.
  const panel = page.locator(".compare-panel");
  await expect(panel.locator("figure.chart")).toBeVisible();
  await expect(panel).toContainText("Ohio State");
  await expect(panel).toContainText("Penn State");
  await expect(panel).not.toContainText("Michigan");
  // A categorical snapshot is not a trend: no %-change delta badge, no trend line.
  await expect(panel.locator(".chart-delta")).toHaveCount(0);

  // Adding a third selection live-updates the same panel.
  await page.getByRole("checkbox", { name: "Compare Michigan" }).check();
  await expect(panel).toContainText("Michigan");
});

// Long institution names collide on the x-axis, and Recharts silently DROPS the
// overlapping ticks — the reported bug where the #1 bar had no label. Every selected
// entity must stay labeled (wrapped onto multiple lines).
const LONG_NAMES = "Largest Texas public universities:\n\n"
  + "| Rank | University | Total Enrollment |\n|---|---|---|\n"
  + "| 1 | Texas A&M University–College Station | 78,321 |\n"
  + "| 2 | The University of Texas at Austin | 53,864 |\n"
  + "| 3 | University of Houston | 47,980 |\n"
  + "| 4 | University of North Texas | 46,864 |\n";

test("every compared bar keeps its label, even long ones (no dropped x-axis tick)", async ({ page }) => {
  await signedIn(page);
  await mockConversation(page, 9, [{ role: "assistant", content: LONG_NAMES }]);
  await page.goto("/chat/9");

  for (let i = 0; i < 4; i++) await page.getByRole("checkbox").nth(i).check();
  await page.getByRole("button", { name: /Compare 4/ }).click();

  const panel = page.locator(".compare-panel");
  await expect(panel.locator("figure.chart")).toBeVisible();
  // The #1 bar (longest name, the one Recharts used to drop) is labeled.
  await expect(panel).toContainText("Texas A&M");
  await expect(panel).toContainText("North Texas");
});

test("a single selection can't be compared, and Clear resets everything", async ({ page }) => {
  await signedIn(page);
  await mockConversation(page, 9, [
    { role: "assistant", content: RANKING },
  ]);
  await page.goto("/chat/9");

  await page.getByRole("checkbox", { name: "Compare Ohio State" }).check();
  await expect(page.getByText("1 selected")).toBeVisible();
  await expect(page.getByRole("button", { name: /Compare 1/ })).toBeDisabled();

  await page.getByRole("button", { name: "Clear" }).click();
  await expect(page.getByText("1 selected")).toHaveCount(0);
  await expect(page.getByRole("checkbox", { name: "Compare Ohio State" })).not.toBeChecked();
});

test("a year-over-year (trend) table offers no compare checkboxes", async ({ page }) => {
  await signedIn(page);
  await mockConversation(page, 9, [
    { role: "assistant", content: TIMESERIES },
  ]);
  await page.goto("/chat/9");

  // The table renders...
  await expect(page.getByRole("cell", { name: "2020" })).toBeVisible();
  // ...but it's a trend, so no selection column and no compare bar.
  await expect(page.getByRole("checkbox")).toHaveCount(0);
});

test("a compare checkbox is keyboard-operable", async ({ page }) => {
  await signedIn(page);
  await mockConversation(page, 9, [
    { role: "assistant", content: RANKING },
  ]);
  await page.goto("/chat/9");

  const box = page.getByRole("checkbox", { name: "Compare Ohio State" });
  await box.focus();
  await page.keyboard.press("Space");
  await expect(box).toBeChecked();
  await expect(page.getByText("1 selected")).toBeVisible();
});
