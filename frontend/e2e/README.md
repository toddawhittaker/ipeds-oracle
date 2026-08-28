# Playwright e2e suite

End-to-end browser tests for the React UI in `frontend/src`. They drive the real
app but intercept every `/api/**` request with `page.route(...)` (see
`mocks.js`), so the suite runs deterministically with **no `LLM_API_KEY`, no
`ipeds.db`, and no backend process**.

## Running

```sh
cd frontend
npm install
npx playwright install chromium   # one-time browser download
npm run test:e2e
```

Useful variants:

```sh
npx playwright test --list                    # list every spec without running
npx playwright test e2e/auth-login.spec.js    # one file
npx playwright test -g "stop generating"      # one describe/test by name
npx playwright test --ui                      # interactive UI mode
npx playwright show-report                    # the HTML report after a run
E2E_PREVIEW=1 npm run test:e2e                # static build (see below)
```

### Which server backs the run

`playwright.config.js` starts the `webServer` itself; `baseURL` points at it. The
dev server's `/api` proxy to `:8000` is never used — every API call is fulfilled
by a mock before it leaves the page.

- **Default: `npm run dev`.** Instant start, and `reuseExistingServer` keeps a
  warm one between runs. Right for iterating on one spec.
- **`E2E_PREVIEW=1` (and always on CI): the static production build.** Measurably
  faster over a full run — **107s → 31s** — because `npm run dev` transforms
  modules on demand per route and re-pays that on every `page.goto` across 342
  tests. `scripts/run_ci_local.sh` sets it.

> **Reuse is deliberately OFF in preview mode.** A lingering preview server keeps
> serving whatever was built when it started, so reusing one runs the suite
> against **stale source and reports a false green**. The dev server re-reads from
> disk, which is why reuse is safe there. If a local run disagrees with CI,
> `pgrep -af 'vite|npm run dev'` before believing it.

## What's here

Specs are named for the surface they cover (`admin-*`, `chat-*`, `auth-*`, …);
`npx playwright test --list` is the current index, and each spec's header comment
explains the regression it exists for. Two files carry more than their name
suggests:

- **`mocks.js`** — every fixture. `mockStreamChat` fulfils an SSE body in one
  shot after a delay; **`mockStreamChatDripped`** patches `window.fetch` and
  enqueues into a `ReadableStream` on timers, which is the only way to observe a
  *partially delivered* stream (with the one-shot mock, a brand-new chat's id
  never arrives until the turn is over). It takes one event script for every
  call, or an array of scripts consumed one per call (the last repeats; the
  counter resets on any full page load) for multi-turn scenarios.
- **`a11y.spec.js`** — the axe gate. Fails on `critical` **and `serious`**, and
  scans the app as it actually renders: a full answer with its disclosures open,
  a mid-stream answer, all six admin paths (Users' three sub-tabs counted
  separately) and the user-facing `/keys` page, in **both themes** (23 scans).

## Four traps that make a spec pass while the product is broken

Every one of these has shipped a defect past a green suite here.

1. **Auto-retrying matchers cannot assert "not true right now."**
   `expect(locator).toHaveCount(1)` retries until it matches, so against a 1.5s
   stream it simply waits the turn out and passes having never seen the
   duplicate. For anything transient by construction, count synchronously:
   `expect(await locator.count()).toBe(1)`.
2. **A fixture that can't express the failure proves nothing.** A "the finished
   answer must not replace the stopped note" test passed with the fix deleted,
   because its mocked conversation never contained that answer. Ask: *if the bug
   were present, would anything in this fixture look different?*
3. **A wait between actions can hide a real race.** Polling for the URL between
   two arrow-key presses is what hid a 100%-reproducible keyboard bug — a real
   user holding an arrow key performs no such wait. Ask whether a user would
   pause there; if not, the wait is hiding a race, not stabilizing a test.
4. **Playwright's role engine does not prune presentational children.** ARIA's
   presentational-children rule strips descendants of a `role="img"` from the
   a11y tree, but `getByRole` still finds them — so toolbar specs passed the
   entire time the toolbar was hidden from assistive tech. For a11y semantics,
   assert *containment* (`[role="img"] .chart-head` → 0), not role.

Also: mock admin lists with **content, not empty arrays** — an empty table
renders none of the elements whose contrast could be wrong, so the axe scan sees
nothing. And axe files a one-character element as `incomplete` rather than a
violation, so a count badge's contrast needs a direct computed-style assertion.
