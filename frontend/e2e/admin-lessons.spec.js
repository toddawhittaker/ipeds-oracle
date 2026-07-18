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
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByRole("link", { name: "Skills" }).click();

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
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByRole("link", { name: "Skills" }).click();

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

// Regression: `.skill-head` is a flex row holding the headline (.lesson-rule)
// and the pill group (.tags). Neither .tags nor .tag used to opt out of flex
// shrinking, so a long enough headline squeezed the pill group until each
// pill's own text wrapped onto a second line (measured: 19px -> 34px tall). A
// pill is a label, not a paragraph — its text must never break.
test("pills never wrap onto a second line, even when a long headline squeezes them", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockSkills(page, [
    {
      // Short headline: pills sit at their natural, un-squeezed width — the
      // single-line baseline the long-headline card is compared against.
      id: 30, question: "q-short", headline: "Short headline.",
      lesson: "Short description.", canonical_sql: "SELECT 1", notes: "",
      verified: false, created_by: "seed", upvotes: 0, downvotes: 0, hits: 0,
    },
    {
      // Real seed #3 headline (from SCHEMA.md's worked examples) — long
      // enough to actually squeeze the pill group at this viewport, which is
      // what makes this a genuine regression test rather than a vacuous one.
      id: 31, question: "q-long",
      headline: "For a national or all-programs total, use the grand-total "
        + "row cipcode='99', never SUM across CIP codes.",
      lesson: "Long description.", canonical_sql: "SELECT 1", notes: "",
      verified: false, created_by: "seed", upvotes: 0, downvotes: 0, hits: 0,
    },
  ]);
  await page.route("**/api/admin/skills/*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' }));

  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByRole("link", { name: "Skills" }).click();

  const shortCard = page.locator(".skill").filter({ hasText: "Short headline." });
  const longCard = page.locator(".skill").filter({ hasText: "For a national or all-programs total" });
  const shortPill = shortCard.locator("span.tag", { hasText: "from seed" });
  const longPill = longCard.locator("span.tag", { hasText: "from seed" });
  await expect(shortPill).toBeVisible();
  await expect(longPill).toBeVisible();

  // States the intent directly and cheaply, but isn't the assertion that
  // actually catches the bug (see the height comparison below).
  await expect(longPill).toHaveCSS("white-space", "nowrap");

  // The assertion that actually catches it: compare the squeezed card's pill
  // height against a known-single-line baseline from the short-headline
  // card. Deliberately NOT comparing height to getComputedStyle().lineHeight
  // — that resolves to the literal string "normal" here, so parseFloat(...)
  // is NaN and every numeric comparison against it is silently false, making
  // the test pass even on a visibly wrapped (two-line) pill.
  const shortBox = await shortPill.boundingBox();
  const longBox = await longPill.boundingBox();
  expect(longBox.height).toBeCloseTo(shortBox.height, 0);
});

// --- Lesson editor (backlog: a critic-proposed lesson with no rule text
// can never be given one without an edit affordance). Backend is already
// complete (PATCH /api/admin/skills/{id}); these tests pin the not-yet-built
// frontend contract, so they are expected to be RED until the implementer
// adds the "edit" button + inline form to Admin.jsx Skills().
//
// Terminology per the agreed spec: the card's "description" is the `lesson`
// field (PR #45 repurposed it); `notes` is only a legacy display fallback.

const EDITABLE_LESSON = {
  id: 7,
  question: "national total of bachelor's degrees",
  headline: "Add majornum=1 for every completions total.",
  lesson: "Summing c_a without majornum=1 double-counts a student's declared "
    + "second major; add the filter to any total or grouped SUM over ctotalt.",
  canonical_sql: "SELECT SUM(ctotalt) FROM c_a WHERE cipcode='99' AND majornum=1;",
  notes: "",
  verified: false,
  created_by: "critic",
  upvotes: 0,
  downvotes: 0,
  hits: 0,
};

test("edit button opens a labelled, prefilled edit form and moves focus to Headline", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockSkills(page, [EDITABLE_LESSON]);
  await page.route("**/api/admin/skills/*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' }));

  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByRole("link", { name: "Skills" }).click();

  // The edit affordance sits alongside the existing Verify/Reject actions —
  // it doesn't replace them.
  const editBtn = page.getByRole("button", { name: "Edit lesson: Add majornum=1 for every completions total." });
  await expect(editBtn).toBeVisible();
  await expect(page.getByRole("button", { name: /Verify lesson:/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /Reject lesson:/ })).toBeVisible();

  await editBtn.click();

  // { exact: true } on Headline only: getByLabel() computes a wrapping
  // <label>'s text from its own DOM text nodes, which for an
  // *already-filled* <textarea> includes the control's own value (a
  // Playwright locator-engine quirk, not a real accessible-name bug — the
  // browser's actual accessibility tree, checked via ariaSnapshot(), reports
  // a clean "Description"/"Example query" name). Non-exact substring
  // matching sidesteps that for the two textareas; Headline needs `exact`
  // instead, to avoid colliding with the "Edit/Verify/Reject lesson: …"
  // button aria-labels (see the fixture-naming note further down).
  const headline = page.getByLabel("Headline", { exact: true });
  const description = page.getByLabel("Description");
  const example = page.getByLabel("Example query");

  await expect(headline).toHaveValue(EDITABLE_LESSON.headline);
  await expect(description).toHaveValue(EDITABLE_LESSON.lesson);
  await expect(example).toHaveValue(EDITABLE_LESSON.canonical_sql);

  await expect(headline).toHaveAttribute("maxlength", "300");
  await expect(description).toHaveAttribute("maxlength", "4000");
  await expect(example).toHaveAttribute("maxlength", "8000");

  await expect(page.getByRole("button", { name: "Save" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel" })).toBeVisible();

  // a11y: entering edit mode lands focus in the Headline input, matching the
  // existing inline-edit-then-restore-focus precedent (frontend/src/Chat.jsx
  // startEdit/cancelEdit around the composer's prompt editor).
  await expect(headline).toBeFocused();
});

test("Save PATCHes exactly {headline, lesson, notes, canonical_sql} trimmed, reloads, announces, and restores focus", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);

  let getCount = 0;
  await page.route("**/api/admin/skills", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    getCount += 1;
    await route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify([EDITABLE_LESSON]),
    });
  });
  let patchBody = null;
  await page.route("**/api/admin/skills/7", async (route) => {
    if (route.request().method() !== "PATCH") return route.continue();
    patchBody = route.request().postDataJSON();
    await route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' });
  });

  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByRole("link", { name: "Skills" }).click();
  expect(getCount).toBe(1);

  const editBtn = page.getByRole("button", { name: "Edit lesson: Add majornum=1 for every completions total." });
  await editBtn.click();

  // Untrimmed edits — Save must send the trimmed value, not the raw one.
  await page.getByLabel("Headline", { exact: true }).fill("  Add majornum=1 for every completions total.  ");
  await page.getByLabel("Description").fill(`  ${EDITABLE_LESSON.lesson}  `);
  await page.getByLabel("Example query").fill(`  ${EDITABLE_LESSON.canonical_sql}  `);

  await page.getByRole("button", { name: "Save" }).click();

  await expect.poll(() => patchBody).not.toBeNull();
  // `notes` rides along with `lesson`, kept in sync (see the dedicated
  // lesson/notes-sync regression tests below): every reader resolves the
  // description as `lesson || notes`, so writing lesson alone would let a
  // stale notes value silently resurface later.
  expect(patchBody).toEqual({
    headline: EDITABLE_LESSON.headline,
    lesson: EDITABLE_LESSON.lesson,
    notes: EDITABLE_LESSON.lesson,
    canonical_sql: EDITABLE_LESSON.canonical_sql,
  });

  // The list is re-loaded after a successful save.
  await expect.poll(() => getCount).toBeGreaterThanOrEqual(2);

  // The save outcome surfaces as an app-wide toast (Skills' old sr-only status
  // region was replaced by useToast — sighted admins now get visible feedback).
  await expect(page.locator(".toast-msg")).toHaveText("Lesson updated.");

  // Edit mode has closed and focus returns to the edit button that opened it.
  await expect(page.getByLabel("Headline", { exact: true })).toHaveCount(0);
  await expect(editBtn).toBeFocused();
});

test("Cancel discards changes without PATCHing and restores focus to the edit button", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockSkills(page, [EDITABLE_LESSON]);
  let patched = false;
  await page.route("**/api/admin/skills/*", async (route) => {
    if (route.request().method() === "PATCH") patched = true;
    await route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' });
  });

  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByRole("link", { name: "Skills" }).click();

  const editBtn = page.getByRole("button", { name: "Edit lesson: Add majornum=1 for every completions total." });
  await editBtn.click();
  await page.getByLabel("Headline", { exact: true }).fill("This edit should be discarded.");

  await page.getByRole("button", { name: "Cancel" }).click();
  await page.waitForTimeout(200);
  expect(patched).toBe(false);

  // Edit mode closed, discarding the draft; the original headline still shows.
  await expect(page.getByLabel("Headline", { exact: true })).toHaveCount(0);
  await expect(page.getByText(EDITABLE_LESSON.headline)).toBeVisible();
  await expect(editBtn).toBeFocused();

  // Re-opening confirms the draft really was discarded, not just hidden.
  await editBtn.click();
  await expect(page.getByLabel("Headline", { exact: true })).toHaveValue(EDITABLE_LESSON.headline);
});

test("only one lesson card is editable at a time", async ({ page }) => {
  // Deliberately avoids the words "headline"/"description"/"example query" in
  // this fixture's own text — those words are also field *labels* in the
  // editor, and getByLabel() matches by substring by default, so a fixture
  // that describes itself in that vocabulary can collide with the label
  // locator even with only one editor open (not the bug this test targets).
  const other = {
    id: 8, question: "q2", headline: "Second lesson rule.",
    lesson: "Second lesson explanatory text.", canonical_sql: "SELECT 2",
    notes: "", verified: true, created_by: "seed", upvotes: 1, downvotes: 0, hits: 2,
  };
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockSkills(page, [EDITABLE_LESSON, other]);
  await page.route("**/api/admin/skills/*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' }));

  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByRole("link", { name: "Skills" }).click();

  await page.getByRole("button", { name: "Edit lesson: Add majornum=1 for every completions total." }).click();
  await expect(page.getByLabel("Headline", { exact: true })).toHaveValue(EDITABLE_LESSON.headline);

  // Opening the second card's editor must close the first's — a single
  // editingId in state, not independent per-card toggles.
  await page.getByRole("button", { name: "Edit lesson: Second lesson rule." }).click();
  // getByLabel is strict-mode: if both editors were open this resolves to two
  // elements and throws, which is exactly the bug this test guards against.
  // { exact: true } also excludes the per-card action buttons, whose
  // aria-labels legitimately embed the headline text (e.g. "Edit lesson: …")
  // and would otherwise substring-match the "Headline" field label.
  await expect(page.getByLabel("Headline", { exact: true })).toHaveValue(other.headline);
});

test("Save is disabled once headline and description are both empty after trimming", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockSkills(page, [EDITABLE_LESSON]);
  await page.route("**/api/admin/skills/*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' }));

  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByRole("link", { name: "Skills" }).click();
  await page.getByRole("button", { name: "Edit lesson: Add majornum=1 for every completions total." }).click();

  const save = page.getByRole("button", { name: "Save" });
  await expect(save).toBeEnabled();

  // Whitespace-only counts as empty — trimmed, not just falsy-checked.
  await page.getByLabel("Headline", { exact: true }).fill("   ");
  await page.getByLabel("Description").fill("   ");
  await expect(save).toBeDisabled();

  // Typing real content into either field re-enables it.
  await page.getByLabel("Headline", { exact: true }).fill("A new headline.");
  await expect(save).toBeEnabled();
});

test("a rule-less lesson (no headline, no description) can be given a headline via the editor", async ({ page }) => {
  const ruleless = {
    id: 9, question: "why did the total look wrong",
    headline: "", lesson: "", canonical_sql: "SELECT 1 -- placeholder",
    notes: "", verified: false, created_by: "critic",
    upvotes: 0, downvotes: 0, hits: 0,
  };
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockSkills(page, [ruleless]);
  let patchBody = null;
  await page.route("**/api/admin/skills/9", async (route) => {
    if (route.request().method() !== "PATCH") return route.continue();
    patchBody = route.request().postDataJSON();
    await route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' });
  });

  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByRole("link", { name: "Skills" }).click();

  // This is the motivating case: no headline text renders, but ruleName()
  // falls back to the question for the accessible name.
  await expect(page.getByText("(no rule text)")).toBeVisible();
  const editBtn = page.getByRole("button", { name: "Edit lesson: why did the total look wrong" });

  // Save must be disabled before any text is entered (both fields blank).
  await editBtn.click();
  await expect(page.getByLabel("Headline", { exact: true })).toHaveValue("");
  await expect(page.getByRole("button", { name: "Save" })).toBeDisabled();

  await page.getByLabel("Headline", { exact: true }).fill("New: always filter majornum=1 on completions totals.");
  await expect(page.getByRole("button", { name: "Save" })).toBeEnabled();
  await page.getByRole("button", { name: "Save" }).click();

  await expect.poll(() => patchBody).not.toBeNull();
  expect(patchBody).toEqual({
    headline: "New: always filter majornum=1 on completions totals.",
    lesson: "",
    notes: "",
    canonical_sql: "SELECT 1 -- placeholder",
  });
});

// --- Regression: clearing a Description must not resurrect stale `notes`
// text (defect found in code review). `_lesson_text` (backend/app/skills.py) and the
// card's own render both resolve the description as `lesson || notes`, so
// writing `lesson` alone on save left a stale, DIFFERENT `notes` value ready
// to silently resurface — into the card AND the model's prompt — the moment
// an admin cleared the Description, while the embedding (recomputed from
// headline+lesson only) no longer matched what was actually served. Every
// real seed row has both columns populated with genuinely different text
// (`lesson` is a v2 rewrite, `notes` is older v1-era wording), so this isn't
// a hypothetical: it's the shape of the live data.
const STALE_NOTES_LESSON = {
  id: 11,
  question: "why did year filtering break",
  headline: "Use a constant year bound.",
  lesson: "Compare year against a constant MAX(year)-N bound; never join a "
    + "DISTINCT year list, which forces a full scan.",
  notes: "STALE v1 TEXT: join a DISTINCT year list and filter on it.",
  canonical_sql: "SELECT 1;",
  verified: true,
  created_by: "seed",
  upvotes: 2,
  downvotes: 0,
  hits: 5,
};

test("editing the Description writes the identical trimmed text to both lesson and notes", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockSkills(page, [STALE_NOTES_LESSON]);
  let patchBody = null;
  await page.route("**/api/admin/skills/11", async (route) => {
    if (route.request().method() !== "PATCH") return route.continue();
    patchBody = route.request().postDataJSON();
    await route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' });
  });

  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByRole("link", { name: "Skills" }).click();
  await page.getByRole("button", { name: "Edit lesson: Use a constant year bound." }).click();

  // The editor's Description is seeded from `lesson` (not `notes`) whenever
  // `lesson` is present — confirms which field is authoritative before we
  // even touch anything.
  await expect(page.getByLabel("Description")).toHaveValue(STALE_NOTES_LESSON.lesson);

  const newText = "Always compare year against a constant MAX(year)-N bound.";
  await page.getByLabel("Description").fill(newText);
  await page.getByRole("button", { name: "Save" }).click();

  await expect.poll(() => patchBody).not.toBeNull();
  expect(patchBody.lesson).toBe(newText);
  expect(patchBody.notes).toBe(newText);
});

test("clearing the Description clears notes too — a stale notes fallback cannot resurrect old guidance", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockSkills(page, [STALE_NOTES_LESSON]);
  let patchBody = null;
  await page.route("**/api/admin/skills/11", async (route) => {
    if (route.request().method() !== "PATCH") return route.continue();
    patchBody = route.request().postDataJSON();
    await route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' });
  });

  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByRole("link", { name: "Skills" }).click();
  await page.getByRole("button", { name: "Edit lesson: Use a constant year bound." }).click();

  // Headline is still non-empty, so Save stays enabled even with an empty
  // Description (draftIsEmpty only trips when BOTH are blank).
  await page.getByLabel("Description").fill("");
  await expect(page.getByRole("button", { name: "Save" })).toBeEnabled();
  await page.getByRole("button", { name: "Save" }).click();

  await expect.poll(() => patchBody).not.toBeNull();
  // The bug: `lesson` clears but `notes` (still "STALE v1 TEXT: …") is left
  // behind, and every reader falls back to it the instant `lesson` is empty.
  // The fix must clear (or otherwise sync) `notes` in the SAME request.
  expect(patchBody.lesson).toBe("");
  expect(patchBody.notes).toBe("");
  expect(patchBody.notes).not.toBe(STALE_NOTES_LESSON.notes);
});

// The "unverified" pill uses .tag.warn (amber, "needs review"), NOT .tag.danger
// (red, implies something's broken). Asserting the class the pill carries pins
// that behavioral distinction palette-independently; the exact --warn RGB and
// its AA contrast are verified separately, not re-pinned here (they'd only churn
// on a palette retune).
test("unverified pill renders as the warn pill, not the danger pill", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockSkills(page, [EDITABLE_LESSON]);
  await page.route("**/api/admin/skills/*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true}' }));

  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByRole("link", { name: "Skills" }).click();

  // span.tag.warn resolving to the "unverified" pill IS the assertion: had it
  // regressed onto .tag.danger, this locator would find no such text.
  await expect(page.locator("span.tag.warn")).toHaveText("unverified");
  await expect(page.locator("span.tag.danger")).toHaveCount(0);
});
