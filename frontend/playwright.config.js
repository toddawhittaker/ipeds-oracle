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
  webServer: {
    command: "npm run dev -- --port 5173 --strictPort",
    url: "http://localhost:5173",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
