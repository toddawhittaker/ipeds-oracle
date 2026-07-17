import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockAllowlist,
  mockAccessRequests,
  mockDeniedRequests,
  mockClearDenial,
} from "./mocks.js";

// See a denied-addresses list, and undo a denial without granting access
// (GET /api/admin/access-requests/denied, DELETE
// /api/admin/access-requests/{email}/denial).
//
// Base contract (SHIPPED, all green below), per .plan-undeny.md AS
// OVERRIDDEN by the ui-ux agent's spec:
//   * the section heading is "Blocked from requesting access", hidden
//     entirely when the denied list is empty.
//   * each row renders the ORIGINAL address(es) (`emails.join(", ")`).
//   * clicking Undo fires the DELETE immediately -- NO window.confirm.
//   * the success flash states BOTH negatives: no access granted, no email
//     sent (matches /not given access|no email/i).
//   * a failed undo (non-2xx) surfaces a `.notice` and doesn't wedge the UI.
//
// SECOND ROUND (security + a11y review of the shipped round-3 UI) -- the
// tests below marked SEC #n / A11Y #n are RED against today's code and pin
// the fixes those reviews called for. See the PM's review-round message for
// full writeups; short version:
//   * SEC #1 (HIGH): an attacker who files ONLY a +tag variant (never the
//     base address) gets the ADMIN to block the base address by Rejecting
//     -- but the denied list only ever rendered `emails` (the variant that
//     was filed), never `canon_email` (the base address that's actually
//     blocked), and the "same mailbox" scope note was gated on
//     `emails.length > 1`, which a tagged-only single-entry group never
//     satisfies. Net effect: the real victim's address never appears
//     anywhere in the admin UI, even though it's the one actually blocked.
//     THIS SUPERSEDES the ui-ux spec's original "canon_email is NEVER
//     rendered" constraint above -- that constraint is what let the bug
//     ship. See the removed/replaced test below.
//   * SEC #2 (MEDIUM): Reject's confirm names the blast radius backwards
//     for a +tag input (canonicalization propagates the block towards the
//     BASE address, not "variants of" whatever was typed).
//   * SEC #3 (LOW): a failed denied-list load must not silently look
//     identical to "nothing is blocked".
//   * SEC #4 (LOW): the date shown is when the request was FILED, not when
//     it was decided (no decided_at column exists) -- must be labeled as
//     such.
//   * A11Y #1 (HIGH, WCAG 2.5.3 Label in Name): the Undo button's
//     aria-label interpolated the address into the MIDDLE of the phrase, so
//     the visible label ("Allow to request again") was never a substring of
//     the accessible name -- unreachable by speech-input ("click Allow to
//     request again").
//   * A11Y #2 (MEDIUM, WCAG 1.4.10-adjacent): `.denied-row` has no
//     flex-wrap, so at a 320px viewport the address column collapses to
//     near-zero width and the row balloons to ~1500px tall.

async function openAllowlistTab(
  page, { allowlist = [], reqs = [], denied = [], deniedHttpStatus = 200 } = {},
) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAllowlist(page, allowlist);
  await mockAccessRequests(page, reqs);
  await mockDeniedRequests(page, denied, { httpStatus: deniedHttpStatus });
  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
}

const ONE_DENIED_GROUP = [
  {
    id: 4,
    canon_email: "victim@example.edu",
    emails: ["victim+1@example.edu", "victim@example.edu"],
    created_at: 1_700_000_000,
  },
];

test.describe("see and undo a denied-address block", () => {
  test("the denied list renders one control per canonical group, showing the ORIGINAL addresses", async ({ page }) => {
    await openAllowlistTab(page, { denied: ONE_DENIED_GROUP });

    await expect(page.getByText("victim@example.edu")).toBeVisible();
    await expect(page.getByText("victim+1@example.edu")).toBeVisible();

    // Exactly ONE control for the whole group -- not one per original address.
    await expect(
      page.getByRole("button", { name: /allow to request again/i }),
    ).toHaveCount(1);
  });

  // ---------------------------------------------------------------------
  // SEC #1 (HIGH) -- REPLACES a test that used to be here, named "the
  // canonical form is never displayed, only the originals". That test
  // encoded the ui-ux spec's original constraint verbatim and was CORRECT
  // against that spec -- but the spec itself turned out to be the bug: a
  // security review found that ALWAYS hiding canon_email defeats the denied
  // list's purpose whenever no original equals it (the tagged-only
  // griefing case below), so the requirement changed and the old test is
  // deleted rather than kept "for coverage". The originals-are-shown half
  // of the old test is still true and re-asserted where relevant below.
  // ---------------------------------------------------------------------

  const TAGGED_ONLY_GROUP = [
    {
      id: 12,
      // The ACTUALLY-BLOCKED address -- is_denied() blocks this exact
      // string, per test_denied_list_surfaces_the_canonical_address_even_
      // when_no_original_matches_it in eval/test_admin_router.py.
      canon_email: "onlytagged@example.edu",
      // The victim never filed anything -- the attacker filed ONLY this
      // +tag variant, and the admin Rejected THAT. canon_email is not
      // itself a member of `emails` -- this is what a tagged-only griefing
      // denial looks like on the wire (verified against the real backend).
      emails: ["onlytagged+newsletter@example.edu"],
      created_at: 1_700_000_000,
    },
  ];

  test("SEC #1: the ACTUALLY-BLOCKED (canonical) address is surfaced even when no original request matches it", async ({ page }) => {
    // The griefing scenario: an attacker files only a +tag variant of a
    // real address, the admin Rejects it, and the base address (never
    // itself requested) is the one that's actually blocked. If the denied
    // list only ever shows `emails` (what was requested), the real victim's
    // address never appears anywhere an admin would search for it.
    await openAllowlistTab(page, { denied: TAGGED_ONLY_GROUP });

    await expect(page.getByText("onlytagged@example.edu")).toBeVisible();
  });

  test("SEC #1: the originally-requested address is also retained, not replaced by the canonical one", async ({ page }) => {
    // The fix direction is additive (canonical primary, original
    // secondary), not a swap -- losing the original would just trade one
    // blind spot for another (an admin who searches for the literal
    // spoofed address that showed up in their inbox would find nothing).
    await openAllowlistTab(page, { denied: TAGGED_ONLY_GROUP });

    await expect(page.getByText("onlytagged+newsletter@example.edu")).toBeVisible();
  });

  test("SEC #1: a single-entry group still distinguishes the blocked address from the requested one (not gated on emails.length > 1)", async ({ page }) => {
    // The old scope note ("-- all the same mailbox...") only rendered when
    // `emails.length > 1`. A tagged-only denial has exactly ONE entry in
    // `emails`, so that gate alone would hide the distinction even after
    // canon_email starts being surfaced elsewhere on the page -- this pins
    // that BOTH addresses appear together, scoped to this row specifically
    // (not just "somewhere on the page"), which is what actually lets an
    // admin connect "the request I saw" to "the address that's blocked".
    await openAllowlistTab(page, { denied: TAGGED_ONLY_GROUP });

    const row = page.locator(".denied-row", { hasText: "onlytagged" });
    await expect(row).toContainText("onlytagged@example.edu");
    await expect(row).toContainText("onlytagged+newsletter@example.edu");
  });

  test("the 'Blocked from requesting access' heading is absent entirely when nothing is denied", async ({ page }) => {
    await openAllowlistTab(page, { denied: [] });

    await expect(page.getByText(/blocked from requesting access/i)).toHaveCount(0);
  });

  test("the heading is present when a denial exists", async ({ page }) => {
    await openAllowlistTab(page, { denied: ONE_DENIED_GROUP });

    await expect(page.getByText(/blocked from requesting access/i)).toBeVisible();
  });

  test("undo fires with NO confirm dialog, and DELETEs the CANONICAL address", async ({ page }) => {
    const clear = await mockClearDenial(page, { httpStatus: 200 });
    await openAllowlistTab(page, { denied: ONE_DENIED_GROUP });

    // Pins the deliberate no-confirm decision (.plan-undeny.md section on
    // Admin.jsx) against a future cargo-cult "for symmetry with Reject":
    // if a confirm() is ever added, this dialog handler firing would hang
    // the click (no listener accepts/dismisses it) and the test times out.
    let dialogFired = false;
    page.on("dialog", () => { dialogFired = true; });

    await page.getByRole("button", { name: /allow to request again/i }).click();

    await expect.poll(() => clear.calls.length).toBe(1);
    // The argument sent is the row's canon_email, not either displayed
    // original -- the display/match non-swap holds for the API call too.
    expect(clear.calls[0]).toBe("victim@example.edu");
    expect(dialogFired).toBe(false);
  });

  test("the success flash states BOTH negatives: no access granted, no email sent", async ({ page }) => {
    await mockClearDenial(page, { httpStatus: 200 });
    await openAllowlistTab(page, { denied: ONE_DENIED_GROUP });

    await page.getByRole("button", { name: /allow to request again/i }).click();

    await expect(page.locator(".notice")).toBeVisible();
    await expect(page.locator(".notice")).toContainText(/not given access|no email/i);
  });

  test("a failed undo surfaces a notice and doesn't wedge the UI", async ({ page }) => {
    await mockClearDenial(page, { httpStatus: 500 });
    await openAllowlistTab(page, { denied: ONE_DENIED_GROUP });

    await page.getByRole("button", { name: /allow to request again/i }).click();

    // Pin the RESULTING STATE the failure notice must convey (the block is
    // still active), not the verb the design chose for the action itself
    // ("undo" vs "unblock" vs anything else) -- the verb isn't part of the
    // behavioral contract, and a regex tied to one specific word just pushes
    // production copy to match the test instead of the other way around
    // (the same anti-pattern the round-1 flash-copy episode hit). Both
    // candidate failure strings seen in review ("Could not undo the block
    // on X. They are still blocked from requesting access." and ui-ux's
    // "Could not unblock X. They are still blocked from requesting
    // access.") share this clause; matching on it leaves the verb free
    // while still turning red if the failure path shows no notice, or a
    // notice that never says the block persisted.
    await expect(page.locator(".notice")).toContainText(/still blocked from requesting access/i);
    // The row (and its control) must still be there, still operable.
    await expect(
      page.getByRole("button", { name: /allow to request again/i }),
    ).toBeEnabled();
  });

  test("Reject's confirm no longer claims allowlisting is the only escape hatch", async ({ page }) => {
    // Round 2's confirm copy said Reject was effectively permanent unless
    // allowlisted -- now false (an admin can undo it directly), and the
    // copy must not still assert that. Doesn't over-specify the exact new
    // wording, only that the old, now-false claim is gone.
    const reqs = [
      { id: 1, email: "onepending@example.edu", reason: null, status: "pending",
        created_at: 1_700_000_000 },
    ];
    await openAllowlistTab(page, { reqs });

    let dialogMessage = "";
    page.once("dialog", (dialog) => {
      dialogMessage = dialog.message();
      dialog.dismiss();
    });
    await page.getByRole("button", { name: "Reject the access request from onepending@example.edu" }).click();

    expect(dialogMessage).not.toMatch(
      /unless you add them to the allowlist|only.*way|can't be undone/i);
  });

  test("SEC #2: Reject's confirm names the address that will actually be blocked, not the literal typed-in one", async ({ page }) => {
    // Canonicalization blocks TOWARD the base address -- denying
    // victim+newsletter@ blocks victim@ (and every other variant), not "any
    // +tag variants of victim+newsletter@" as the old copy claimed. That
    // phrasing has the direction backwards for exactly this input: victim@
    // is not itself a "+tag variant of" victim+newsletter@.
    const reqs = [
      { id: 3, email: "victim+newsletter@example.edu", reason: null, status: "pending",
        created_at: 1_700_000_000 },
    ];
    await openAllowlistTab(page, { reqs });

    let dialogMessage = "";
    page.once("dialog", (dialog) => {
      dialogMessage = dialog.message();
      dialog.dismiss();
    });
    await page.getByRole(
      "button", { name: "Reject the access request from victim+newsletter@example.edu" },
    ).click();

    expect(dialogMessage).toContain("victim@example.edu");
  });

  test("SEC #3: a failed denied-list load keeps the section visible with an error state, not silent absence", async ({ page }) => {
    // Today: Admin.jsx's load() does
    // `api.deniedRequests().then(setDenied).catch(() => {})`, so a failed
    // fetch leaves `denied` at its initial `[]` -- byte-identical to "no one
    // is blocked". That's the one view whose entire job is revealing an
    // active block; a silent failure here is a false negative, not a
    // graceful degradation (contrast: the allowlist table still renders,
    // visibly empty, on its own load failure).
    await openAllowlistTab(page, { deniedHttpStatus: 500 });

    await expect(page.getByText(/could(n.t| not) load blocked addresses/i)).toBeVisible();
  });

  test("SEC #4: the date shown next to a blocked address is labeled as when the request was FILED, not when it was blocked", async ({ page }) => {
    // created_at is MAX(created_at) from access_requests -- there is no
    // decided_at column, so this is unavoidably the request's filing time,
    // not a denial timestamp. Rendered as a bare date it reads as "blocked
    // on", which overstates what the app actually knows.
    await openAllowlistTab(page, { denied: ONE_DENIED_GROUP });

    const row = page.locator(".denied-row", { hasText: "victim@example.edu" });
    await expect(row).toContainText(/requested/i);
  });

  // -------------------------------------------------------------------
  // A11Y #1 (HIGH, WCAG 2.5.3 Label in Name) -- the Undo button's old
  // aria-label interpolated the address into the MIDDLE of the phrase
  // ("Allow {emails} to request access again"), so the visible label
  // ("Allow to request again") was never a substring of the accessible
  // name. Every getByRole(...) locator above already exercises the FIXED
  // shape (/allow to request again/i, updated from the old
  // /allow .* to request access again/i -- see the top-of-file comment);
  // this test is the one that pins the actual WCAG contract generically,
  // by reading the real rendered visible text and real computed accessible
  // name at runtime rather than assuming either.
  // -------------------------------------------------------------------

  test("A11Y #1 (WCAG 2.5.3): the Undo button's accessible name CONTAINS its visible label", async ({ page }) => {
    await openAllowlistTab(page, { denied: ONE_DENIED_GROUP });

    const btn = page.locator(".denied-row button");
    const visible = (await btn.innerText()).trim();
    // Sanity: a vacuous pass (empty visible text trivially "contained" in
    // anything) must not be possible.
    expect(visible.length).toBeGreaterThan(0);
    const escaped = visible.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    await expect(btn).toHaveAccessibleName(new RegExp(escaped, "i"));
  });

  test("A11Y #2 (WCAG 1.4.10-adjacent): a blocked-address row wraps instead of collapsing to a sliver at a 320px viewport", async ({ page }) => {
    // Measured on today's code before this fix: .denied-row 238x1587px,
    // .denied-who (the address column) squeezed to 0-5px wide -- the two
    // flex:none siblings (date, button) never yield width, and with no
    // flex-wrap the address has nowhere to go but down (overflow-wrap:
    // anywhere breaks it one character at a time). In-repo precedent for
    // the fix: styles.css's `@media (max-width: 559.98px) { .skill-head {
    // flex-wrap: wrap; } }` (PR #55). Bounds below are generous --
    // comfortably below the ~1500px/near-0px failure mode, comfortably
    // above what a genuinely wrapped 2-3-line row needs -- so this doesn't
    // pin exact pixels, only that the degradation actually happened.
    await page.setViewportSize({ width: 320, height: 700 });
    await openAllowlistTab(page, { denied: ONE_DENIED_GROUP });

    const row = page.locator(".denied-row").first();
    await expect(row).toBeVisible();
    const box = await row.boundingBox();
    expect(box).not.toBeNull();
    expect(box.height).toBeLessThan(300);

    const who = page.locator(".denied-who").first();
    const whoBox = await who.boundingBox();
    expect(whoBox).not.toBeNull();
    expect(whoBox.width).toBeGreaterThan(100);
  });
});
