import { test, expect } from "@playwright/test";

import { borderContrast, contrastRatio, nonTextContrast } from "./contrast.js";
import {
  mockMe, mockConversations, mockAttention, mockMarkLogsSeen,
  mockAllowlist, mockAccessRequests, mockDeniedRequests, mockSkills, mockLogs,
} from "./mocks.js";

// The switch animates its track/thumb over .15s. Sampling mid-transition reads a
// BLENDED colour -- the same trap that once reported 3.56:1 against text resting
// at 4.85:1. The stylesheet zeroes these transitions under prefers-reduced-motion,
// so scan resting pixels by construction rather than racing an easing curve.
test.use({ contextOptions: { reducedMotion: "reduce" } });

// Contrast on the Admin surfaces the axe gate structurally cannot see.
//
// Two separate blind spots, and both are why these are direct measurements
// rather than another axe scan:
//
//  1. COVERAGE — a11y.spec.js scans Login and the EMPTY Chat state. No admin
//     page is scanned at all, so nothing here has ever been checked. A WARNING
//     log level in particular only renders when a WARNING record exists.
//  2. RULE — axe files a pseudo-element (no text) as `incomplete`, never a
//     violation, and has no rule for 1.4.11 non-text contrast in the first
//     place. So the switch thumb is unreachable at any threshold.
//
// Measured on resolved pixels, so a token retune keeps these honest instead of
// pinning colour literals.

const ADMIN = { email: "admin@example.edu", is_admin: true };

async function adminMocks(page) {
  await mockMe(page, ADMIN);
  await mockConversations(page, []);
  await mockAllowlist(page, []);
  await mockAccessRequests(page, []);
  await mockDeniedRequests(page, []);
  await mockSkills(page, []);
  await mockAttention(page, { users: 0, skills: 0, logs: 0 });
  await mockMarkLogsSeen(page);
}

const RECORDS = [
  { ts: 1700000000, level: "INFO", name: "ipeds.app", msg: "started" },
  { ts: 1700000100, level: "WARNING", name: "ipeds.app", msg: "something looks off" },
  { ts: 1700000200, level: "ERROR", name: "ipeds.app", msg: "it failed" },
];

for (const theme of ["light", "dark"]) {
  test(`the WARNING log level is readable in the ${theme} theme`, async ({ page }) => {
    // THE REGRESSION: this rule hardcoded #c98a1a, which measures 2.52:1 on the
    // light theme's --bg — under the 4.5 floor, on the admin's primary triage
    // signal, on the one screen whose whole job is "is something wrong". Its
    // ERROR sibling one line below already used a token. No scan renders a
    // WARNING record, so nothing caught it.
    await page.emulateMedia({ colorScheme: theme });
    await adminMocks(page);
    await mockLogs(page, RECORDS);
    await page.goto("/admin/logs");

    const warn = page.locator(".logline.lvl-WARNING .loglvl");
    await expect(warn).toBeVisible();
    const ratio = await contrastRatio(page, ".logline.lvl-WARNING .loglvl");
    expect(ratio, `WARNING level measured ${ratio?.toFixed(2)}:1 in ${theme}`)
      .toBeGreaterThanOrEqual(4.5);
  });

  for (const state of ["off", "on"]) {
    test(`the switch thumb has a visible boundary when ${state} in the ${theme} theme`,
      async ({ page }) => {
        // WCAG 1.4.11: the thumb's POSITION is the state, so its boundary
        // against the track must clear 3:1. A hardcoded #fff failed two of the
        // four combinations — light-OFF at 1.66 and dark-ON at 2.43 (the same
        // #fff-on---accent pairing that put the avatar badge at 2.43).
        //
        // The assertion is max(fill, ring) because NO single fill can satisfy
        // both states: in the light theme the thumb would need luminance
        // >= 0.296 to clear the --accent track and <= 0.161 to clear
        // --line-strong. So the fill carries the ON state and a --muted ring
        // carries the OFF state. Demanding the fill alone would be a bound the
        // design cannot meet, and demanding neither is the bug.
        await page.emulateMedia({ colorScheme: theme });
        await adminMocks(page);
        await mockLogs(page, RECORDS);
        await page.goto("/admin/logs");

        const box = page.locator(".switch input[type='checkbox']");
        await expect(box).toBeVisible();
        // The Logs auto-refresh toggle starts checked; drive it to the state
        // under test rather than assuming a default.
        if ((await box.isChecked()) !== (state === "on")) await box.click();
        await expect(box).toBeChecked({ checked: state === "on" });

        const { fill, ring } = await nonTextContrast(page, ".switch input[type='checkbox']");
        const best = Math.max(fill ?? 0, ring ?? 0);
        expect(best,
          `thumb ${state}/${theme}: fill ${fill?.toFixed(2)}:1, ring ${ring?.toFixed(2) ?? "none"}:1`)
          .toBeGreaterThanOrEqual(3);
      });
  }

  test(`form control borders are findable in the ${theme} theme`, async ({ page }) => {
    // WCAG 1.4.11: the border IS the affordance on an empty text field and on
    // the outline buttons, which carry no fill of their own. They all used
    // --line, a 1.2-1.3:1 hairline -- measured 1.33:1 on --panel in light and
    // as low as 1.14:1 on --panel-2 in dark. --line is deliberately that soft,
    // because it is also every table rule and panel edge in the app, so the fix
    // was a second token (--line-control) rather than darkening the first.
    //
    // These four are representatives of the ~19 rules that moved: an input, a
    // select, an outline button, and the search field. They share the token, so
    // one retune below 3:1 reddens all of them; each also catches its own rule
    // being pointed back at --line. Nothing else covers this -- axe has no
    // 1.4.11 rule.
    await page.emulateMedia({ colorScheme: theme });
    await adminMocks(page);
    await page.goto("/admin/usage");

    const measured = [];
    const measure = async (sel, name) => {
      await expect(page.locator(sel).first()).toBeVisible();
      const ratio = await borderContrast(page, sel);
      measured.push(name);
      expect(ratio, `${name} border measured ${ratio?.toFixed(2)}:1 in ${theme}`)
        .toBeGreaterThanOrEqual(3);
    };

    await measure(".usage-range button", "outline button");
    // Custom reveals a real <input>, the headline case: on an EMPTY field the
    // border is the only thing saying a control is there at all.
    await page.getByRole("button", { name: "Custom" }).click();
    await measure(".usage-custom input", "date input");

    // mockLogs BEFORE navigating: the search field renders with the log view,
    // and leaving /api/admin/logs unrouted makes its appearance depend on how a
    // dead request resolves. That is a flaky test, not a fast one -- this
    // assertion passed three runs in a row before the missing mock was spotted.
    await mockLogs(page, RECORDS);
    await page.goto("/admin/logs");
    await measure(".searchwrap .logsearch", "search field");

    // Named rather than counted, so a selector that silently stops matching
    // shows up as a missing name instead of a quietly smaller number.
    expect(measured).toEqual(["outline button", "date input", "search field"]);
  });
}
