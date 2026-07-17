# Playwright e2e suite

End-to-end browser tests for the React UI in `web/src`. These drive the real
app (via Vite dev server) but intercept every `/api/**` request with
`page.route(...)` (see `mocks.js`), so the suite runs deterministically with
**no `LLM_API_KEY` and no `ipeds.db`** — no backend process is started.

## Running

```sh
cd web
npm install
npx playwright install chromium   # one-time browser download
npm run test:e2e
```

Useful variants:

```sh
npx playwright test --list                 # list specs without running them
npx playwright test e2e/auth-login.spec.js  # run a single file
npx playwright test --ui                    # interactive UI mode
npx playwright show-report                  # open the HTML report after a run
```

`playwright.config.js` starts `npm run dev -- --port 5173 --strictPort` as the
`webServer` and points `baseURL` at it. The dev server's `/api` proxy to
`:8000` (see `web/vite.config.js`) is never actually used — every API call is
fulfilled by a mock before it leaves the page.

## Specs

- `auth-login.spec.js` — logged-out state renders `<Login/>`; requesting a
  sign-in link shows the `.notice` message.
- `app-shell-roles.spec.js` — Admin tab visibility keyed off `is_admin`; sign
  out returns to Login.
- `chat-happy-path.spec.js` — ask a question, watch the SSE-streamed markdown
  answer (with a table) render, expand the SQL log, then confirm the
  follow-up conversation fetch attaches the message id that unlocks the
  CSV download link.
- `admin-tabs.spec.js` — click through Allowlist / Imports / Usage / Skills
  and assert each panel's mocked content; submit the add-allowlist form and
  assert the POST body. Also carries the Skills-tab-unmount crash regression
  test (see comment in that file for the bug history).
- `nces-catalog.spec.js` — the Imports tab's NCES year catalog: per-year
  cards' selectable/integrated state, the "Integrate selected (N)" button, the
  disk-headroom meter, per-file download progress, "update"-status cards, and
  keyboard/selection styling.
- `year-remove.spec.js` — the trashcan (`.year-remove`, DELETE
  `/api/admin/import/year/{start_year}` via `mockDeintegrate`) that removes an
  already-integrated year: button visibility gated on integrated/update
  status, the confirm-dialog gate, and the resulting job poll/success notice;
  plus the rebuild progress bar (`[data-testid="rebuild-progress"]`) driven by
  a polled job's `progress.rebuild` JSON.
- `a11y.spec.js` — coverage for the accessibility fixes: conversation list
  items are real buttons (with `aria-current` on the active one), the
  streamed assistant answer container has `aria-live`, the Login/Chat/Admin
  inputs are reachable via `getByLabel`/`getByRole`, primary-nav and Admin
  subtab active state uses `aria-current`, the markdown result-table wrapper
  is a focusable `role="region"`, Admin has a `main` landmark, and the Login
  notice becomes `role="alert"` after submission. Also runs a couple of
  `@axe-core/playwright` smoke scans (Login, Chat) asserting no *critical*
  violations.
- `admin-lessons.spec.js` — the Learned-lessons admin view: the headline
  leads, the longer description collapses behind its own "Details", the SQL
  worked example stays collapsed under "Example query", and verify/reject
  actions (incl. the destructive-delete confirm dialog).
- `delete-focus.spec.js` — focus management after deleting a conversation:
  deleting the open chat navigates to `/` and focuses the composer; deleting a
  different chat focuses whatever now occupies the deleted row's index (next
  sibling, else previous, else "+ New chat", never `<body>`); a dedicated
  bare-`aria-live` "delete-announcer" region reports what happened (with a
  remaining-chat count so two same-titled deletes in a row still produce
  distinct announcements); dismissing the confirm or a failed DELETE leave
  focus sane and never falsely announce "Deleted"; an unrelated later
  `refreshConvos()` must not steal focus back into the sidebar; and the
  pre-existing unscoped `[role="status"].sr-only` locator must keep resolving
  to exactly one node.
