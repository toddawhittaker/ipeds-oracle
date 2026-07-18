import { test, expect } from "@playwright/test";
import { mockMe, mockConversations, mockAllowlist, mockAccessRequests } from "./mocks.js";

// frontend/src/Admin.jsx's Allowlist component (`inviteFlash(addr, res)`) shows one
// of FIVE distinct messages after an add-allowlist POST, driven by the
// `delivery` value in backend/app/routers/admin.py's add_allowlist response:
//   {ok, email, invited, mail_configured, delivery}
// `delivery` is the single source of truth for WHY no email was sent (or
// whether one was sent at all) -- `invited`/`mail_configured` alone can't
// distinguish "already on the allowlist, nothing attempted" from "a
// configured provider genuinely failed to send" (that conflation was the
// bug: PR #57 inferred the cause from the two booleans alone and told the
// admin an invite had FAILED to send for someone who was simply already
// allowlisted -- see delivery === "already_allowlisted" below).
// Each branch needs an opposite admin reaction, so each gets its own spec
// rather than one parametrized "flash renders something" smoke test.

async function openAllowlistAndSubmit(page, email = "newperson@example.edu") {
  await page.goto("/");
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByPlaceholder("email", { exact: true }).fill(email);
  await page.getByRole("button", { name: "Add" }).click();
}

test("delivery=emailed -> 'a sign-in link was emailed to' flash", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAccessRequests(page, []);
  await mockAllowlist(page, [], {
    postBody: {
      ok: true, email: "newperson@example.edu",
      invited: true, mail_configured: true, delivery: "emailed",
    },
  });

  await openAllowlistAndSubmit(page);

  await expect(page.locator(".toast-msg")).toHaveText(
    "Approved — a sign-in link was emailed to newperson@example.edu.");
});

test("delivery=failed (send failed WITH a key configured) -> "
  + "'FAILED to send... check the Logs tab' flash", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAccessRequests(page, []);
  await mockAllowlist(page, [], {
    postBody: {
      ok: true, email: "newperson@example.edu",
      invited: false, mail_configured: true, delivery: "failed",
    },
  });

  await openAllowlistAndSubmit(page);

  const status = page.locator(".toast-msg");
  await expect(status).toContainText("newperson@example.edu added, but the invite email FAILED to send");
  await expect(status).toContainText("check the Logs tab for the error");
  await expect(status).toContainText("ask them to request one from the sign-in page");
});

test("delivery=logged_to_console (no key configured, dev mode) -> "
  + "'server console, not the Logs tab' flash", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAccessRequests(page, []);
  await mockAllowlist(page, [], {
    postBody: {
      ok: true, email: "newperson@example.edu",
      invited: false, mail_configured: false, delivery: "logged_to_console",
    },
  });

  await openAllowlistAndSubmit(page);

  const status = page.locator(".toast-msg");
  await expect(status).toContainText("newperson@example.edu added. No email was sent");
  await expect(status).toContainText("the sign-in link is in the server console, not the Logs tab");
});

// The bug fixed by this feature: re-adding someone ALREADY on the allowlist
// mints no invite link and attempts no send at all, so `invited` is False for
// a reason that has NOTHING to do with mail -- distinct from delivery=failed
// above, which is a real, configured-provider send failure. Before `delivery`
// existed, both cases collapsed to the same (invited=False, mail_configured=
// true) pair, and the UI confidently (and wrongly) told the admin the email
// had failed to send.
test("delivery=already_allowlisted -> 'was already on the allowlist' flash, "
  + "never the FAILED-to-send message", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAccessRequests(page, []);
  await mockAllowlist(page, [], {
    postBody: {
      ok: true, email: "existing@example.edu",
      invited: false, mail_configured: true, delivery: "already_allowlisted",
    },
  });

  await openAllowlistAndSubmit(page, "existing@example.edu");

  const status = page.locator(".toast-msg");
  await expect(status).toContainText("existing@example.edu was already on the allowlist");
  await expect(status).toContainText("They can sign in from the sign-in page whenever they like");
  await expect(status).not.toContainText("FAILED to send");
});

// The bug this whole change fixed: a failed POST (network error, 4xx/5xx —
// nothing was ever added) used to fall through `.catch(() => ({}))` into the
// same "added" copy as a real success, sending the admin off to chase an
// email for an account that was never created. This is the branch most
// likely to regress, per the PM's flag.
test("POST failure -> \"Couldn't add ... the request failed\" flash, "
  + "never a false 'added' claim", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAccessRequests(page, []);
  await mockAllowlist(page, [], { postStatus: 500, postBody: { detail: "boom" } });

  await openAllowlistAndSubmit(page);

  const status = page.locator(".toast-msg");
  await expect(status).toHaveText(
    "Couldn't add newperson@example.edu — the request failed. Try again.");
  await expect(status).not.toContainText("added");
});

// Regression pin: an unknown/absent `delivery` (e.g. a future backend build
// the frontend hasn't caught up to, or a malformed mock) must fall back to a
// bare, non-committal "{addr} added." rather than guessing -- see
// inviteFlash's comment on silence beating a confident wrong answer.
test("missing delivery field -> bare '{addr} added.' fallback, never a "
  + "guessed cause", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAccessRequests(page, []);
  await mockAllowlist(page, [], {
    postBody: { ok: true, email: "newperson@example.edu", invited: false, mail_configured: true },
  });

  await openAllowlistAndSubmit(page);

  await expect(page.locator(".toast-msg")).toHaveText("newperson@example.edu added.");
});
