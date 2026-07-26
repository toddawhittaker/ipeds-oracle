import { test, expect } from "@playwright/test";

import { contrastRatio, nonTextContrast } from "./contrast.js";
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
}
