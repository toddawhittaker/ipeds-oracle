import { test, expect } from "@playwright/test";
import { mockMe, mockConversations, mockAllowlist, mockAccessRequests } from "./mocks.js";

// web/src/Admin.jsx's Allowlist component (`inviteFlash(addr, res)`) shows one
// of four distinct messages after an add-allowlist POST, driven by the
// response shape from app/routers/admin.py's add_allowlist:
//   {ok, email, invited, mail_configured}
// Each branch needs an opposite admin reaction, so each gets its own spec
// rather than one parametrized "flash renders something" smoke test.

async function openAllowlistAndSubmit(page, email = "newperson@example.edu") {
  await page.goto("/");
  await page.getByRole("button", { name: "Admin" }).click();
  await page.getByPlaceholder("email").fill(email);
  await page.getByRole("button", { name: "Add" }).click();
}

test("invited=true -> 'a sign-in link was emailed to' flash", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAccessRequests(page, []);
  await mockAllowlist(page, [], {
    postBody: { ok: true, email: "newperson@example.edu", invited: true, mail_configured: true },
  });

  await openAllowlistAndSubmit(page);

  await expect(page.getByRole("status")).toHaveText(
    "Approved — a sign-in link was emailed to newperson@example.edu.");
});

test("invited=false, mail_configured=true (send failed WITH a key) -> "
  + "'FAILED to send... check the Logs tab' flash", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAccessRequests(page, []);
  await mockAllowlist(page, [], {
    postBody: { ok: true, email: "newperson@example.edu", invited: false, mail_configured: true },
  });

  await openAllowlistAndSubmit(page);

  const status = page.getByRole("status");
  await expect(status).toContainText("newperson@example.edu added, but the invite email FAILED to send");
  await expect(status).toContainText("check the Logs tab for the error");
  await expect(status).toContainText("ask them to request one from the sign-in page");
});

test("invited=false, mail_configured=false (no key configured, dev mode) -> "
  + "'server console, not the Logs tab' flash", async ({ page }) => {
  await mockMe(page, { email: "admin@example.edu", is_admin: true });
  await mockConversations(page, []);
  await mockAccessRequests(page, []);
  await mockAllowlist(page, [], {
    postBody: { ok: true, email: "newperson@example.edu", invited: false, mail_configured: false },
  });

  await openAllowlistAndSubmit(page);

  const status = page.getByRole("status");
  await expect(status).toContainText("newperson@example.edu added. No email was sent");
  await expect(status).toContainText("the sign-in link is in the server console, not the Logs tab");
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

  const status = page.getByRole("status");
  await expect(status).toHaveText(
    "Couldn't add newperson@example.edu — the request failed. Try again.");
  await expect(status).not.toContainText("added");
});
