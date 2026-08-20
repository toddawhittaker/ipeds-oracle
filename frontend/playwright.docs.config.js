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
    // A DEPLOYMENT-SHAPED HOSTNAME, mapped back to the dev server by Chromium.
    //
    // The /keys screenshot shows the MCP endpoint, and that string is
    // `window.location.origin` — the one thing on that page the app knows and a
    // document cannot. Shot against `localhost:5173` it published the capture
    // harness's own dev port as if it were the address to give an MCP client.
    // `origin` cannot be stubbed (it is non-configurable on Location), so the
    // fix is to genuinely serve the page under a name that reads like a real
    // install, matching the example.edu fixtures the rest of this run uses.
    baseURL: "http://ipeds.example.edu",
    viewport: { width: 1280, height: 860 },
    deviceScaleFactor: 2,
  },
  projects: [{
    name: "chromium",
    use: {
      launchOptions: {
        // Resolve the fictional host to the dev server. No /etc/hosts entry, no
        // DNS, and it cannot leak outside this run.
        // Maps the PORT too, so the page is served on the default one and
        // `location.origin` carries no port at all — `http://ipeds.example.edu`,
        // which is what an install behind a proxy actually looks like.
        args: ["--host-resolver-rules=MAP ipeds.example.edu:80 127.0.0.1:5173"],
      },
    },
  }],
  webServer: {
    // --host 127.0.0.1 so the resolver mapping above reaches it: vite otherwise
    // binds a name that resolves to ::1 first, and the mapped request lands on
    // an IPv4 socket nothing is listening on.
    command: "npm run dev -- --port 5173 --strictPort --host 127.0.0.1",
    url: "http://localhost:5173",
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
