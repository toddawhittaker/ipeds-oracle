// Regenerates every screenshot in docs/images/ for the user + admin guides.
//
// NOT A TEST. It asserts almost nothing; it drives the real app to a state and
// photographs it. It lives here to reuse `mocks.js`, and `playwright.config.js`
// ignores `*.capture.js` so CI never runs it. Run it with:
//
//     scripts/docs-shots.sh
//
// WHY IT IS COMMITTED. The first set of guide screenshots was made with a
// throwaway spec in #114. Every one of them was still dated 2026-07-20 when the
// top bar was redesigned on 07-25 — Admin and the theme toggle moved into the
// avatar menu, the grounding marks shipped, the attention badges shipped — so
// the guides showed a product that no longer existed, which is the worst kind
// of documentation error: it is invisible to every test and it is the first
// thing a new user sees. A committed regenerator makes the fix one command.
//
// WHY THE MOCK HARNESS RATHER THAN A LIVE APP. Two reasons, both from #114:
// admin screens rendered against a real deployment would publish real users'
// email addresses, and a live LLM answer is nondeterministic, so a re-shoot
// would silently change the prose in the docs. Every fixture below is
// example.edu and public IPEDS-shaped data.
//
// THEMES. Shots are light by default (what most people run). A few subjects are
// captured in BOTH and stitched side-by-side by scripts/docs-shots.sh, so the
// guides can show that dark mode exists without doubling every image.
import { test, expect } from "@playwright/test";
import {
  mockMe, mockAuthConfig, mockVersion, mockAttention, mockConversations,
  mockConversation, mockAllowlist, mockAccessRequests, mockDeniedRequests,
  mockSkills, mockSkillCategories, mockSkillRejections,
  mockLogs, mockMarkLogsSeen, mockImportJobs, mockImportCatalog,
  mockUsage, mockApiKeys, mockAdminKeys, gotoAdmin,
} from "./mocks.js";

const OUT = "../docs/images";        // relative to frontend/
const PAIR = "../.docs-shots";       // scratch for the light/dark pairs

// ---------------------------------------------------------------------------
// Fixtures. Public IPEDS-shaped figures, example.edu people. The numbers only
// have to be PLAUSIBLE — nothing here is asserted against the real dataset, and
// a reader who checks them against IPEDS should find the shape right, so keep
// magnitudes honest (~350k nursing awards/yr nationally, not 3.5M).
// ---------------------------------------------------------------------------

const NURSING_SQL =
  "SELECT year, SUM(ctotalt) AS awards\n"
  + "FROM c_a\n"
  + "WHERE cipcode = '51.3801' AND majornum = 1\n"
  + "  AND year > (SELECT MAX(year) - 5 FROM _years)\n"
  + "GROUP BY year\nORDER BY year;";

const NURSING_MD =
  "Nursing degree production peaked in **2022** and has eased slightly since, "
  + "down 4.1% over the following three years. The associate's level drove the "
  + "run-up through 2021; bachelor's completions have held roughly flat.\n\n"
  + "| Year | Awards | Change |\n| --- | --- | --- |\n"
  + "| 2021 | 318,402 | — |\n"
  + "| 2022 | 324,575 | +1.9% |\n"
  + "| 2023 | 320,118 | −1.4% |\n"
  + "| 2024 | 315,690 | −1.4% |\n"
  + "| 2025 | 311,240 | −1.4% |\n\n"
  + "```chart\n"
  + JSON.stringify({
    type: "line", x: "Year", y: ["Awards"], title: "Nursing awards, 2021–2025",
    data: [
      { Year: "2021", Awards: 318402 }, { Year: "2022", Awards: 324575 },
      { Year: "2023", Awards: 320118 }, { Year: "2024", Awards: 315690 },
      { Year: "2025", Awards: 311240 },
    ],
  })
  + "\n```\n\n"
  + "*Method: IPEDS Completions (C\\_A), CIP 51.3801 Registered Nursing, all "
  + "award levels, first majors only.*\n";

const NURSING_TURN = [
  { id: 1, role: "user",
    content: "How have nursing degrees changed nationally over the last 5 years?" },
  { id: 2, role: "assistant",
    content: NURSING_MD,
    sql_log: [NURSING_SQL],
    thinking: [
      { kind: "status", text: "Looking up the CIP code for Registered Nursing…" },
      { kind: "text", text: "51.3801 is Registered Nursing (RN, ASN, BSN, MSN). Totalling all award levels with majornum = 1 so a second major isn't counted twice." },
      { kind: "sql", text: NURSING_SQL },
      { kind: "status", text: "Checking the magnitude against the national rate…" },
    ],
    figure: { value: "324,575", unit: "awards",
      label: "peak national nursing awards (2022)",
      source: "IPEDS Completions, 2021–2025" },
    figure_grounding: "exact",
    table_grounding: "matched", table_cells_checked: 14, table_cells_matched: 14,
    suggestions: ["Break this out by award level",
      "Which states grew fastest?", "Compare with allied health"],
    duration_ms: 11200 },
];

// A categorical (entity) table — this is what turns on the compare checkboxes.
const BY_STATE_MD =
  "The five states awarding the most Registered Nursing degrees in 2025:\n\n"
  + "| State | Awards | Institutions |\n| --- | --- | --- |\n"
  + "| California | 28,410 | 142 |\n"
  + "| Texas | 24,873 | 118 |\n"
  + "| Florida | 22,109 | 133 |\n"
  + "| New York | 19,554 | 96 |\n"
  + "| Pennsylvania | 15,338 | 87 |\n";

const BY_STATE_TURN = [
  { id: 1, role: "user", content: "Which states award the most nursing degrees?" },
  { id: 2, role: "assistant", content: BY_STATE_MD,
    sql_log: ["SELECT stabbr, SUM(ctotalt) AS awards FROM c_a WHERE cipcode='51.3801' GROUP BY stabbr ORDER BY awards DESC LIMIT 5;"],
    table_grounding: "matched", table_cells_checked: 10, table_cells_matched: 10,
    suggestions: ["Show this per capita", "Break out by award level"] },
];

const CONVOS = [
  { id: 7, title: "Nursing degrees, 5-year trend", updated_at: 1_760_000_000 },
  { id: 8, title: "Top states for nursing", updated_at: 1_759_900_000 },
  { id: 9, title: "CS bachelor's in California", updated_at: 1_759_800_000 },
];

// One person's MCP keys. `last4` only — the server never returns more, and a
// screenshot is the last place a whole credential should be able to appear.
const USER_KEYS = [
  { id: 1, last4: "9f2a", label: "Work laptop", created_at: 1_757_000_000,
    created_by: null, last_used_at: 1_759_990_000, revoked_at: null },
  { id: 2, last4: "1c40", label: "Weekly enrollment report", created_at: 1_755_400_000,
    created_by: "admin@example.edu", last_used_at: 1_759_600_000, revoked_at: null },
];

// The admin table also carries the owner, and a revoked row — the status column
// reads as an absence without one, which is the thing the column exists to say.
const ALL_KEYS = [
  { id: 1, email: "taylor.rivera@example.edu", last4: "9f2a", label: "Work laptop",
    created_at: 1_757_000_000, created_by: null, last_used_at: 1_759_990_000, revoked_at: null },
  { id: 2, email: "taylor.rivera@example.edu", last4: "1c40", label: "Weekly enrollment report",
    created_at: 1_755_400_000, created_by: "admin@example.edu", last_used_at: 1_759_600_000, revoked_at: null },
  { id: 3, email: "jordan.avery@example.edu", last4: "77b1", label: "Desktop",
    created_at: 1_754_100_000, created_by: null, last_used_at: null, revoked_at: null },
  { id: 4, email: "sam.okafor@example.edu", last4: "0e63", label: "Old laptop",
    created_at: 1_752_000_000, created_by: null, last_used_at: 1_753_000_000,
    revoked_at: 1_756_000_000 },
];

async function userMocks(page) {
  await mockMe(page, { email: "taylor.rivera@example.edu", is_admin: false });
  await mockVersion(page);
  await mockConversations(page, CONVOS);
  await mockConversation(page, 7, NURSING_TURN);
  await mockConversation(page, 8, BY_STATE_TURN);
  await mockApiKeys(page, USER_KEYS);
}

async function adminMocks(page) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockVersion(page);
  await mockAdminKeys(page, ALL_KEYS);
  await mockAttention(page, { users: 2, skills: 3, logs: 1 });
  await mockConversations(page, CONVOS);
  // The user-menu shot is taken over a real answer, so this admin needs the
  // same conversation the user fixtures serve.
  await mockConversation(page, 7, NURSING_TURN);
  await mockAllowlist(page, [
    { email: "taylor.rivera@example.edu", note: "Institutional Research", is_admin: 0,
      added_by: "admin@example.edu", added_at: 1_755_000_000, last_login: 1_759_990_000, last_active: 1_759_990_000 },
    { email: "jordan.avery@example.edu", note: "Provost's office", is_admin: 0,
      added_by: "admin@example.edu", added_at: 1_754_000_000, last_login: 1_759_900_000, last_active: 1_759_900_000 },
    { email: "admin@example.edu", note: "Owner", is_admin: 1,
      added_by: "bootstrap", added_at: 1_750_000_000, last_login: 1_760_000_000, last_active: 1_760_000_000 },
    { email: "sam.okafor@example.edu", note: "Enrollment", is_admin: 0,
      added_by: "admin@example.edu", added_at: 1_756_000_000, last_login: null, last_active: null },
  ]);
  await mockAccessRequests(page, [
    { id: 11, email: "casey.nguyen@example.edu", canon_email: "casey.nguyen@example.edu",
      created_at: 1_759_950_000, status: "pending" },
    { id: 12, email: "robin.patel@example.edu", canon_email: "robin.patel@example.edu",
      created_at: 1_759_960_000, status: "pending" },
  ]);
  await mockDeniedRequests(page, [
    { id: 21, email: "spam@elsewhere.test", canon_email: "spam@elsewhere.test",
      // `emails` is REQUIRED: the blocked list is grouped canonically and the
      // row renders r.emails.filter(...). Omitting it crashed the whole Users
      // page into the error boundary — and did so AFTER the readiness
      // assertion, which is why `shot()` now checks for that boundary.
      emails: ["spam@elsewhere.test", "spam+promo@elsewhere.test"],
      created_at: 1_758_000_000, denied_at: 1_758_100_000 },
  ]);
  await mockSkills(page, [
    { id: 1, question: "national total of bachelor's degrees",
      headline: "Add majornum = 1 to every completions total.",
      description: "Summing c_a without majornum = 1 double-counts a student's declared second major. Add the filter to any total or grouped SUM over ctotalt.",
      lesson: "Summing c_a without majornum = 1 double-counts a student's declared second major.",
      sql_example: "SELECT SUM(ctotalt) FROM c_a\nWHERE cipcode = '99'   -- the grand-total row\n  AND majornum = 1;    -- don't double-count second majors",
      canonical_sql: "SELECT SUM(ctotalt) FROM c_a WHERE cipcode='99' AND majornum=1;",
      verified: 1, created_by: "critic", created_at: 1_757_000_000,
      upvotes: 6, downvotes: 0, hits: 41 },
    { id: 2, question: "degrees in a CIP family",
      headline: "Match an exact 6-digit CIP, never LIKE '51.%'.",
      description: "cipcode exists at 2-, 4- and 6-digit levels plus a '99' grand-total row, and each level sums to the same total. A LIKE prefix therefore counts the same awards several times.",
      lesson: "cipcode exists at several rollup levels; a LIKE prefix double-counts.",
      sql_example: "-- one program:\nWHERE cipcode = '51.3801'\n-- a whole family: use the 2-digit rollup row, not LIKE\nWHERE cipcode = '51'",
      canonical_sql: "SELECT SUM(ctotalt) FROM c_a WHERE cipcode='51.3801';",
      verified: 0, created_by: "user-feedback", created_at: 1_759_500_000,
      upvotes: 2, downvotes: 0, hits: 7, category: "CIP_ROLLUP" },
  ]);
  // Without these two the Skills shot loses its category pill and "Reject &
  // mute" (both fail CLOSED on a missing category list) AND renders a red
  // "couldn't load rejected lessons" box — a published screenshot of an error
  // state. Tokens and labels are the real ones from backend/app/lessoncats.py.
  await mockSkillCategories(page, [
    { token: "CIP_ROLLUP", label: "CIP rollup double-count", learnable: true,
      muted: false, pending: 1 },
    { token: "SECOND_MAJOR", label: "Second-major double-count", learnable: true,
      muted: false, pending: 0 },
    { token: "MAGNITUDE", label: "Implausible magnitude", learnable: true,
      muted: true, pending: 0 },
  ]);
  await mockSkillRejections(page, [
    { id: 4, headline: "Always state the award level in the answer.",
      description: "Proposed three times and declined each time — the method line already carries it.",
      category: "QUESTION_MISMATCH", created_by: "critic", created_at: 1_759_000_000 },
  ]);
  await mockLogs(page, [
    { ts: 1_759_990_000, level: "INFO", name: "ipeds.app", msg: "Answered question in 8.4s (cache miss, 2 tool calls)" },
    { ts: 1_759_989_400, level: "INFO", name: "ipeds.mail", msg: "Sign-in link sent to taylor.rivera@example.edu" },
    { ts: 1_759_988_100, level: "WARNING", name: "ipeds.llm", msg: "Escalated to the pro model after a lint warning" },
    { ts: 1_759_987_000, level: "INFO", name: "ipeds.import", msg: "Integrated 2024-25 (final): 8 families, 1.9 GB" },
    { ts: 1_759_986_000, level: "INFO", name: "ipeds.app", msg: "Started; 6 collection years loaded" },
  ]);
  await mockMarkLogsSeen(page);
  await mockImportJobs(page, [
    { id: 3, filename: "integrate:2024", status: "swapped", updated_at: 1_759_987_000 },
  ]);
  await mockImportCatalog(page, {
    probed_at: 1_760_000_000, partial: false,
    years: [2019, 2020, 2021, 2022, 2023, 2024].map((sy, i) => ({
      start_year: sy, year: sy + 1, year_label: `${sy}-${String(sy + 1).slice(2)}`,
      status: i < 5 ? "integrated" : "final",
      integrated: i < 5, available: true,
      release: i < 5 ? "Final" : "Final", selectable: i >= 5,
      zip_bytes: 780_000_000,
    })),
    disk: { free_bytes: 412e9, total_bytes: 1e12, used_bytes: 588e9 },
    calibration: null,
  });
  await mockUsage(page, {
    bucket: "day",
    series: [
      { t: "2026-07-20", queries: 14, tokens: 121_000, spend: 0.18 },
      { t: "2026-07-21", queries: 22, tokens: 190_400, spend: 0.29 },
      { t: "2026-07-22", queries: 9, tokens: 78_200, spend: 0.11 },
      { t: "2026-07-23", queries: 31, tokens: 268_900, spend: 0.41 },
      { t: "2026-07-24", queries: 27, tokens: 231_500, spend: 0.35 },
      { t: "2026-07-25", queries: 18, tokens: 154_300, spend: 0.23 },
      { t: "2026-07-26", queries: 24, tokens: 205_800, spend: 0.31 },
    ],
    top_users: [
      { email: "taylor.rivera@example.edu", queries: 61, tokens: 528_400, spend: 0.79 },
      { email: "jordan.avery@example.edu", queries: 44, tokens: 381_200, spend: 0.57 },
      { email: "admin@example.edu", queries: 27, tokens: 231_900, spend: 0.35 },
      { email: "sam.okafor@example.edu", queries: 13, tokens: 108_600, spend: 0.16 },
    ],
    totals: {
      queries: 145, tokens: 1_250_100, spend: 1.88, cache_hits: 19,
      escalations: 4, failures: 1,
      prompt_tokens: 1_042_000, cached_prompt_tokens: 831_000,
      first_call_prompt_tokens: 402_000, first_call_cached_prompt_tokens: 356_000,
      figures_checked: 88, figures_ungrounded: 3,
      table_cells_checked: 1_412, table_cells_matched: 1_386,
      emit_turns: 145, structured_turns: 145, leaked_turns: 0,
      exhausted_turns: 2, degraded_turns: 0,
    },
  });
}

/** Force a theme before the app boots — main.jsx reads localStorage.theme. */
async function useTheme(page, theme) {
  await page.addInitScript((t) => {
    try { localStorage.setItem("theme", t); } catch { /* ignore */ }
  }, theme);
}

/** Settle animations/streaming so two runs produce comparable pixels. */
async function settle(page) {
  await page.waitForTimeout(400);
}

/** Bring the top of the answer into view — the chat lands scrolled to the
 *  bottom, which clips the hero figure the "anatomy" shot exists to show. */
async function scrollToAnswerTop(page) {
  const fig = page.locator(".figure").first();
  if (!(await fig.count())) return;
  await fig.evaluate((el) => {
    el.scrollIntoView({ block: "start" });
    // scrollIntoView aligns the element's top to the viewport top, which tucks
    // the figure's own mono caption up under the sticky header. Back off inside
    // whichever ancestor actually scrolls so the whole device — caption, number,
    // rule, source — is in frame.
    let n = el.parentElement;
    while (n && n.scrollHeight <= n.clientHeight) n = n.parentElement;
    if (n) n.scrollTop -= 72;
  });
}

async function shot(page, file) {
  await settle(page);
  // A readiness assertion proves the page was alive THEN. Data that arrives
  // afterwards can still crash the tree — which is exactly what happened: a
  // blocked-users row missing its `emails` field took the whole Users page into
  // the error boundary a few hundred ms after `getByRole("heading")` passed,
  // and two guide screenshots were published as a "Something went wrong" card.
  // Check at the moment of capture, which is the only moment that matters.
  await expect(page.getByRole("heading", { name: /Something went wrong/i }))
    .toHaveCount(0);
  await page.screenshot({ path: file });
}

// --------------------------------------------------------------------------
// The user guide
// --------------------------------------------------------------------------

test("signin", async ({ page }) => {
  await mockMe(page, null);
  await mockAuthConfig(page, "example.edu");
  await page.goto("/");
  await expect(page.getByRole("button", { name: /sign-in link/i })).toBeVisible();
  await shot(page, `${OUT}/signin.png`);
});

test("empty-state", async ({ page }) => {
  await userMocks(page);
  await mockConversations(page, []);
  await page.goto("/");
  await expect(page.getByRole("textbox", { name: /Ask about IPEDS data/i })).toBeVisible();
  await shot(page, `${OUT}/empty-state.png`);
});

test("answer-anatomy", async ({ page }) => {
  await userMocks(page);
  await page.goto("/chat/7");
  await expect(page.getByRole("table")).toBeVisible();
  await scrollToAnswerTop(page);
  await shot(page, `${OUT}/answer-anatomy.png`);
});

test("thinking-trace", async ({ page }) => {
  await userMocks(page);
  await page.goto("/chat/7");
  await expect(page.getByRole("table")).toBeVisible();
  await page.getByRole("button", { name: /Thinking/i }).first().click();
  // The trace opens full-width BELOW the actions row, so scrolling to the top
  // of the answer (what the anatomy shot wants) pushes the thing this shot
  // exists to show off the bottom of the frame. Frame the panel itself.
  const trace = page.locator(".trace-panel");
  await expect(trace).toBeVisible();
  await trace.evaluate((el) => el.scrollIntoView({ block: "center" }));
  await shot(page, `${OUT}/thinking-trace.png`);
});

test("chart-maximized", async ({ page }) => {
  await userMocks(page);
  await page.goto("/chat/7");
  await expect(page.getByRole("table")).toBeVisible();
  await page.getByRole("button", { name: /maximi[sz]e/i }).first().click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await shot(page, `${OUT}/chart-maximized.png`);
});

test("compare", async ({ page }) => {
  // Taller viewport for this one shot. The compare panel opens BELOW the table
  // and together they exceed the standard frame — so at the default height the
  // bars get cut off, and scrolling up to fix that disengages auto-scroll and
  // pops the "Jump to latest" pill over the chart legend. Giving the shot room
  // shows the real UI with nothing hidden and nothing overlapping.
  await page.setViewportSize({ width: 1280, height: 1180 });
  await userMocks(page);
  await page.goto("/chat/8");
  await expect(page.getByRole("table")).toBeVisible();
  const boxes = page.getByRole("checkbox");
  await boxes.nth(1).check();
  await boxes.nth(2).check();
  await boxes.nth(3).check();
  await page.getByRole("button", { name: /^Compare/i }).click();
  // The panel opens BELOW the table, so the default view cuts the bars off at
  // the fold and the whole point of the shot — labelled bars for the rows you
  // ticked — is missing. Bring the panel fully into frame.
  await expect(page.locator(".compare-panel")).toBeVisible();
  await shot(page, `${OUT}/compare.png`);
});

test("keys", async ({ page }) => {
  await userMocks(page);
  await page.goto("/keys");
  // Readiness on real CONTENT, not just the heading: this page renders its load
  // failure as a paragraph rather than an error boundary, so `shot`'s
  // "Something went wrong" check cannot see a missing mock — it would publish a
  // picture of an error message instead.
  await expect(page.getByText("Work laptop")).toBeVisible();
  await shot(page, `${OUT}/keys.png`);
});

test("user-menu", async ({ page }) => {
  await adminMocks(page);
  await page.goto("/chat/7");
  await expect(page.getByRole("table")).toBeVisible();
  await page.getByRole("button", { name: /Account menu/ }).click();
  await expect(page.getByRole("menu")).toBeVisible();
  await shot(page, `${OUT}/user-menu.png`);
});

// --------------------------------------------------------------------------
// The admin guide
// --------------------------------------------------------------------------

test("admin-users", async ({ page }) => {
  await adminMocks(page);
  await page.goto("/");
  await gotoAdmin(page);
  await expect(page.getByRole("heading", { name: /Users/i })).toBeVisible();
  await shot(page, `${OUT}/admin-users.png`);
});

test("admin-pending", async ({ page }) => {
  await adminMocks(page);
  await page.goto("/admin/users/pending");
  await expect(page.getByRole("heading", { name: /Users/i })).toBeVisible();
  await shot(page, `${OUT}/admin-pending.png`);
});

test("admin-imports", async ({ page }) => {
  await adminMocks(page);
  await page.goto("/admin/imports");
  await expect(page.getByRole("heading", { name: /Load IPEDS years/i })).toBeVisible();
  await shot(page, `${OUT}/admin-imports.png`);
});

test("admin-usage", async ({ page }) => {
  await adminMocks(page);
  await page.goto("/admin/usage");
  await expect(page.getByText(/Queries/i).first()).toBeVisible();
  await shot(page, `${OUT}/admin-usage.png`);
});

test("admin-skills", async ({ page }) => {
  await adminMocks(page);
  await page.goto("/admin/skills");
  await expect(page.getByRole("heading", { name: /Learned lessons/i })).toBeVisible();
  await shot(page, `${OUT}/admin-skills.png`);
});

test("admin-keys", async ({ page }) => {
  await adminMocks(page);
  await page.goto("/admin/keys");
  // Same reason as the /keys shot: a failed load here is a paragraph, not the
  // error boundary. Wait for a row.
  await expect(page.getByRole("cell", { name: "taylor.rivera@example.edu", exact: true }).first())
    .toBeVisible();
  await shot(page, `${OUT}/admin-keys.png`);
});

test("admin-logs", async ({ page }) => {
  await adminMocks(page);
  await page.goto("/admin/logs");
  await expect(page.getByRole("heading", { name: /Server logs/i })).toBeVisible();
  await shot(page, `${OUT}/admin-logs.png`);
});

// --------------------------------------------------------------------------
// Light/dark pairs — stitched into one image by scripts/docs-shots.sh.
// Deliberately only TWO subjects: enough to show the theme exists, without
// doubling the maintenance surface of every screenshot in the guides.
// --------------------------------------------------------------------------

for (const theme of ["light", "dark"]) {
  test(`pair-answer-${theme}`, async ({ page }) => {
    await useTheme(page, theme);
    await userMocks(page);
    await page.goto("/chat/7");
    await expect(page.getByRole("table")).toBeVisible();
    await scrollToAnswerTop(page);
    await shot(page, `${PAIR}/answer-${theme}.png`);
  });

  test(`pair-admin-usage-${theme}`, async ({ page }) => {
    await useTheme(page, theme);
    await adminMocks(page);
    await page.goto("/admin/usage");
    await expect(page.getByText(/Queries/i).first()).toBeVisible();
    await shot(page, `${PAIR}/admin-usage-${theme}.png`);
  });
}
