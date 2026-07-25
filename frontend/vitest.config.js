import { readdirSync } from "node:fs";

import { defineConfig } from "vitest/config";

// Every pure-logic module that HAS a co-located unit test is gated, derived from
// the filesystem rather than a hand-kept list. The old explicit array drifted in
// exactly the way every other hand-maintained duplicate list in this repo has:
// adding src/foo.test.js without also adding src/foo.js left the module silently
// UNGATED -- no failure, no signal, which is the worst shape a coverage gap can
// take. Deriving it means the only way to escape the floor is to have no test at
// all, which is visible.
// RECURSIVE on purpose. The `include` glob below is src/**, so a test in a
// SUBDIRECTORY (src/admin/format.test.js) is collected and runs — but a
// top-level-only readdir never put its module in `coverage.include`, so it
// silently escaped the 80% floor while looking fully tested. That is the exact
// drift shape this derivation exists to kill, reintroduced by a directory: no
// failure, no signal. Splitting Admin.jsx into src/admin/* is what would have
// triggered it.
const gatedModules = readdirSync("src", { recursive: true })
  .map((f) => String(f).replaceAll("\\", "/")) // recursive readdir yields nested paths
  .filter((f) => f.endsWith(".test.js"))
  .map((f) => `src/${f.replace(/\.test\.js$/, ".js")}`)
  .sort();

// Vitest — the FAST unit tier for pure logic (see CLAUDE.md "How we work" -> the
// test pyramid). Genuine browser truth (routing, focus, aria-live/AT, back/
// forward, SSE-driven DOM) lives in web/e2e/ under Playwright; jsdom's focus and
// history models are NOT the browser's, so anything that leans on them belongs
// in Playwright, not here.
export default defineConfig({
  test: {
    // jsdom gives leaf DOM utilities (Blob, document) a home without booting a
    // browser; the seeded tests are pure input->output logic and mostly never
    // touch it.
    environment: "jsdom",
    // Unit tests are co-located as src/**/*.test.{js,jsx}. web/e2e/*.spec.js is
    // Playwright's alone and MUST NOT be collected here -- those import
    // @playwright/test and would blow up under vitest.
    include: ["src/**/*.test.{js,jsx}"],
    coverage: {
      provider: "v8",
      // SCOPED floor: gate coverage on ONLY the pure-logic modules we've
      // committed to unit-testing. This mirrors backend/app/'s per-module >=80% line
      // floor (scripts/coverage_check.sh) for the JS side. Browser-tested
      // components (Chat.jsx, Admin.jsx, App.jsx, Chart.jsx, ...) are
      // deliberately NOT listed -- they're covered by Playwright, and unit-
      // testing them through jsdom would fake the very browser behaviour they
      // exist to guarantee. Add a module here when (and only when) it gets real
      // unit tests, so JS coverage never silently escapes a gate. The list is
      // DERIVED above from the co-located *.test.js files -- see that comment.
      include: gatedModules,
      all: true,
      thresholds: {
        // Per-file so one weak module can't hide behind strong ones -- same
        // shape as coverage_check.sh's per-module check. Line coverage is the
        // gated metric, matching backend/app/.
        perFile: true,
        lines: 80,
      },
      reporter: ["text", "text-summary"],
    },
  },
});
