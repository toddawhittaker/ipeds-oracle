import { defineConfig, devices } from "@playwright/test";

// e2e tests drive the real built/dev UI and mock every /api/** call at the
// network layer (see e2e/mocks.js). No backend, no LLM_API_KEY, no
// ipeds.db is required to run this suite.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 2 : undefined,
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
  // CI serves the STATIC production build via `vite preview` instead of the dev
  // server: `npm run dev` transforms modules ON DEMAND per route, and on CI's
  // weak shared runner that first-load transform cost (paid over and over as
  // tests navigate) is a large chunk of the e2e wall-clock. A prebuilt static
  // server has none of it. `vite preview` keeps SPA history-fallback (appType
  // 'spa'), so deep links like /admin/users/pending still resolve to index.html.
  // Locally we keep the dev server for fast iteration + reuseExistingServer.
  webServer: {
    command: process.env.CI
      ? "npm run build && npm run preview -- --port 5173 --strictPort"
      : "npm run dev -- --port 5173 --strictPort",
    url: "http://localhost:5173",
    reuseExistingServer: !process.env.CI,
    // The build has to finish before the URL answers, so give CI more headroom
    // than the dev server's near-instant start.
    timeout: process.env.CI ? 120_000 : 30_000,
  },
});
