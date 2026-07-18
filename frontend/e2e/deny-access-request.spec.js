import { test, expect } from "@playwright/test";
import {
  mockMe,
  mockConversations,
  mockAllowlist,
  mockAccessRequests,
  mockDenyAccessRequest,
} from "./mocks.js";

// Reject an access request (POST /api/admin/access-requests/{email}/deny).
// Not implemented yet -- Admin.jsx's Allowlist component has no Reject
// button, so every test below is expected to fail red until the implementer
// ships it. Contract (see .plan-deny.md):
//   * a Reject button, aria-label `Reject the access request from {email}`,
//     renders next to Approve for each pending request row.
//   * clicking it opens the app-styled confirmation modal (role="alertdialog",
//     confirm button "Reject request"), and only on confirm POSTs
//     /api/admin/access-requests/{email}/deny for that address.
//   * cancelling the modal fires no request.
//   * a failed deny (non-2xx) surfaces an error toast + in-modal error and does
//     not wedge the UI (the modal stays recoverable).
//   * a successful deny reloads the pending-requests list.

async function openAllowlistTab(page, { allowlist = [], reqs = [] } = {}) {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAllowlist(page, allowlist);
  await mockAccessRequests(page, reqs);
  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
}

const TWO_PENDING = [
  { id: 1, email: "one@example.edu", reason: null, status: "pending", created_at: 1_700_000_000 },
  { id: 2, email: "two@example.edu", reason: null, status: "pending", created_at: 1_700_000_100 },
];

test.describe("reject an access request", () => {
  test("a Reject button renders beside Approve for each pending request", async ({ page }) => {
    await openAllowlistTab(page, { reqs: TWO_PENDING });

    await expect(
      page.getByRole("button", { name: "Reject the access request from one@example.edu" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Reject the access request from two@example.edu" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: /^Reject the access request from/ }),
    ).toHaveCount(2);

    // Approve is still there too -- Reject is additive, not a replacement.
    await expect(page.getByRole("button", { name: "Approve" }).first()).toBeVisible();
  });

  test("confirm -> POST fires the deny for the right address", async ({ page }) => {
    const deny = await mockDenyAccessRequest(page, { httpStatus: 200 });
    await openAllowlistTab(page, { reqs: TWO_PENDING });

    await page.getByRole("button", { name: "Reject the access request from one@example.edu" }).click();
    const dialog = page.getByRole("alertdialog");
    // The modal names the address and explains the block, then a specific confirm.
    await expect(dialog).toContainText("one@example.edu");
    await expect(dialog).toContainText(/block/i);
    await dialog.getByRole("button", { name: "Reject request" }).click();

    await expect.poll(() => deny.calls.length).toBe(1);
    expect(deny.calls[0]).toBe("one@example.edu");
  });

  test("cancelling the confirm modal does not fire a POST", async ({ page }) => {
    const deny = await mockDenyAccessRequest(page, { httpStatus: 200 });
    await openAllowlistTab(page, { reqs: TWO_PENDING });

    await page.getByRole("button", { name: "Reject the access request from one@example.edu" }).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toHaveCount(0);
    await page.waitForTimeout(200);

    expect(deny.calls.length).toBe(0);
  });

  test("a failed deny surfaces an error toast + in-modal error and stays recoverable", async ({ page }) => {
    await mockDenyAccessRequest(page, { httpStatus: 500 });
    await openAllowlistTab(page, { reqs: TWO_PENDING });

    await page.getByRole("button", { name: "Reject the access request from one@example.edu" }).click();
    const dialog = page.getByRole("alertdialog");
    await dialog.getByRole("button", { name: "Reject request" }).click();

    await expect(page.locator(".toast")).toContainText(/Could not reject/i);
    // The modal stays open on failure with a contextual in-modal error, and stays
    // recoverable (Cancel works) -- the background is inert by design meanwhile.
    await expect(dialog).toBeVisible();
    await expect(dialog.locator(".notice.error")).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toHaveCount(0);
    // Background restored: the other row's Reject button works again.
    await expect(
      page.getByRole("button", { name: "Reject the access request from two@example.edu" }),
    ).toBeEnabled();
  });

  test("the list refreshes after a successful deny", async ({ page }) => {
    await mockMe(page, { email: "admin@example.edu", is_admin: true });
    await mockConversations(page, []);
    await mockAllowlist(page, []);
    await mockDenyAccessRequest(page, { httpStatus: 200 });

    let call = 0;
    await page.route("**/api/admin/access-requests", async (route) => {
      call += 1;
      const body = call === 1 ? TWO_PENDING.slice(0, 1) : [];
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    });

    await page.goto("/");
    await page.getByRole("link", { name: "Admin" }).click();
    // Scope to the pending-request ROW (`.req`, per Admin.jsx), not the whole
    // page: a page-wide getByText would also match anything else on the page
    // that happens to contain this address (e.g. a flash message naming who
    // was rejected) and over-specify a UI decision this test isn't about.
    // The intent here is narrower and purely structural: the pending-request
    // row for this address is gone after a successful deny + reload.
    const pendingRow = page.locator(".req", { hasText: "one@example.edu" });
    await expect(pendingRow).toBeVisible();

    await page.getByRole("button", { name: "Reject the access request from one@example.edu" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Reject request" }).click();

    await expect(pendingRow).toHaveCount(0);
    // The Reject button unmounted with its row; focus must land on the stable
    // add-email input rather than dropping to <body> (the toast takes no focus).
    await expect(page.getByLabel("Email", { exact: true })).toBeFocused();
  });

  // Regression: `.req` used to right-align Approve with a bare
  // `margin-left: auto` on `.req button`. That worked while `.req` held
  // exactly one button, but flexbox splits free space EQUALLY between
  // multiple auto margins -- once Reject joined Approve beside it, each row
  // divided its OWN slack between the two buttons, and since each row's
  // slack depends on its email's rendered width, Approve landed at a
  // different x in every row. The fix moves the auto margin onto the email
  // span instead (`.req > span { margin-right: auto }`), so the button PAIR
  // stays glued together and right-aligned regardless of address length.
  //
  // The rows below use WILDLY different email lengths on purpose: the old
  // bug only manifests when rows have different slack. Same-length emails
  // would pass on the broken CSS too (vacuous test) -- see PR #55's pill-wrap
  // regression for the same trap with white-space wrapping.
  test("Approve and Reject line up at the same x across rows of very different email length", async ({ page }) => {
    const rows = [
      { id: 1, email: "a@b.edu", reason: null, status: "pending", created_at: 1_700_000_000 },
      {
        id: 2,
        email: "a-very-long-departmental-mailbox-address@subdomain.example.edu",
        reason: null,
        status: "pending",
        created_at: 1_700_000_100,
      },
      { id: 3, email: "mid.length@example.edu", reason: null, status: "pending", created_at: 1_700_000_200 },
    ];
    await openAllowlistTab(page, { reqs: rows });

    const approveBoxes = [];
    const rejectBoxes = [];
    for (const r of rows) {
      const approveBox = await page
        .getByRole("button", { name: `Approve the access request from ${r.email}` })
        .boundingBox();
      const rejectBox = await page
        .getByRole("button", { name: `Reject the access request from ${r.email}` })
        .boundingBox();
      expect(approveBox).not.toBeNull();
      expect(rejectBox).not.toBeNull();
      approveBoxes.push(approveBox);
      rejectBoxes.push(rejectBox);
    }

    // The assertion that actually catches the bug class: every row's Approve
    // sits at the same x as every other row's Approve, and likewise for
    // Reject -- NOT a hardcoded pixel value (that would break on any
    // unrelated layout change), just the cross-row invariant.
    const approveXs = approveBoxes.map((b) => b.x);
    const rejectXs = rejectBoxes.map((b) => b.x);
    for (const x of approveXs) expect(x).toBeCloseTo(approveXs[0], 0);
    for (const x of rejectXs) expect(x).toBeCloseTo(rejectXs[0], 0);

    // And Reject must sit to the right of Approve in every row (the pair
    // stayed together, not just each button independently aligned).
    for (let i = 0; i < rows.length; i += 1) {
      expect(rejectBoxes[i].x).toBeGreaterThan(approveBoxes[i].x);
    }
  });

  // Companion regression: Approve used to be a bare, unstyled `<button>` --
  // raw browser chrome (Chromium's UA default is a flat grey, `rgb(239, 239,
  // 239)`, with no border-radius). Deliberately NOT asserting the exact
  // accent color (a brittle literal that would break on any theme/palette
  // change) -- just that it's neither the browser default background nor
  // square-cornered, which is what "unstyled" actually looks like.
  test("Approve is a styled pill, not a bare unstyled <button>", async ({ page }) => {
    await openAllowlistTab(page, { reqs: TWO_PENDING });

    const approve = page.getByRole("button", { name: "Approve the access request from one@example.edu" });
    await expect(approve).not.toHaveCSS("background-color", "rgb(239, 239, 239)");
    await expect(approve).not.toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
    await expect(approve).not.toHaveCSS("border-radius", "0px");
  });
});
