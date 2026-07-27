// Config for the documentation screenshot run ONLY — see e2e/docs.capture.js
// and scripts/docs-shots.sh. Separate from playwright.config.js (which ignores
// `*.capture.js`) so the capture never runs as part of the test suite: it
// asserts nothing, writes into the repo, and would just be slow, flaky weight
// in CI.
//
// Fixed viewport + deviceScaleFactor 2: the guides are read on desktop and on
// GitHub's retina renderings, and a stable viewport keeps a re-shoot a diff of
// what CHANGED rather than a diff of the window size.
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: /docs\.capture\.js/,
  // One at a time: several browsers writing PNGs into the same directory is
  // needless risk, and the whole run is well under a minute.
  workers: 1,
  retries: 0,
  reporter: "list",
  timeout: 60_000,
  use: {
    // The spread MUST come first: devices["Desktop Chrome"] specifies its own
    // viewport and deviceScaleFactor, so anything set above it is silently
    // overwritten and every shot comes out 1280x720 @1x.
    ...devices["Desktop Chrome"],
    baseURL: "http://localhost:5173",
    viewport: { width: 1280, height: 860 },
    deviceScaleFactor: 2,
  },
  projects: [{ name: "chromium" }],
  webServer: {
    command: "npm run dev -- --port 5173 --strictPort",
    url: "http://localhost:5173",
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
