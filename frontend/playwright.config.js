import { defineConfig, devices } from "@playwright/test";

// Serve a prebuilt static bundle rather than the dev server. Always on CI;
// locally opt in with E2E_PREVIEW=1 for a full-suite run (see webServer below).
const USE_PREVIEW = !!process.env.CI || process.env.E2E_PREVIEW === "1";

// e2e tests drive the real built/dev UI and mock every /api/** call at the
// network layer (see e2e/mocks.js). No backend, no LLM_API_KEY, no
// ipeds.db is required to run this suite.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  // "100%" = one worker per available CPU. The old hardcoded 2 came in with the
  // original scaffolding (73c9530), copied from Playwright's template — it was
  // never a measured choice, and it left the runner idle: the suite is I/O-bound
  // (page loads against a static server, every /api/** mocked in-process), not
  // CPU-bound, so it parallelises nearly linearly. `list` prints the resolved
  // worker count on every run, which is also how we learn the runner's size.
  workers: process.env.CI ? "100%" : undefined,
  reporter: "list",
  timeout: 30_000,
  expect: { timeout: 5_000 },

  use: {
    baseURL: "http://localhost:5173",
    trace: "retain-on-failure",
  },

  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],

  // The Vite dev server's proxy to :8000 (vite.config.js) is irrelevant here —
  // every /api/** request is intercepted by page.route() before it reaches the
  // network, so no real backend process needs to be running.
  //
  // Serve the STATIC production build via `vite preview` instead of the dev
  // server whenever we're running the WHOLE suite — always on CI, and locally
  // when E2E_PREVIEW=1 (which scripts/run_ci_local.sh sets).
  //
  // MEASURED: the suite is dominated by fixed per-test cost, not by sleeps. No
  // test finishes faster than 2.8s and the median is 4.6s, while all 38
  // waitForTimeout calls together total under 20s — ~1% of the run. Most of
  // that floor is `npm run dev` transforming modules ON DEMAND per route, paid
  // again on every page.goto across 342 tests. CI, on a prebuilt static server
  // with only TWO workers, finishes the same suite faster than a local 16-worker
  // dev-server run.
  //
  // The dev server stays the default for ITERATION (instant start, and
  // reuseExistingServer keeps a warm one between runs) — the cost only pays off
  // across a full run, where the one-off build is amortised over 342 tests.
  //
  // reuseExistingServer is deliberately OFF in preview mode: a lingering
  // preview server serves whatever was built when it started, so reusing one
  // would run the suite against STALE source and report a false green. That is
  // a trap this repo has hit before; a dev server re-transforms from disk and
  // does not have it, which is why reuse is still fine there.
  //
  // `vite preview` keeps SPA history-fallback (appType 'spa'), so deep links
  // like /admin/users/pending still resolve to index.html.
  webServer: {
    command: USE_PREVIEW
      ? "npm run build && npm run preview -- --port 5173 --strictPort"
      : "npm run dev -- --port 5173 --strictPort",
    url: "http://localhost:5173",
    reuseExistingServer: !USE_PREVIEW,
    // The build has to finish before the URL answers, so give preview mode more
    // headroom than the dev server's near-instant start.
    timeout: USE_PREVIEW ? 120_000 : 30_000,
  },
});
