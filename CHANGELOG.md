# Changelog

Notable changes per release. The same text is published as the body of each
[GitHub Release](https://github.com/toddawhittaker/ipeds-oracle/releases) —
that's what the in-app "update available" banner links to.

Written **at tag time**, not per PR: seed it from
`git log --oneline <previous-tag>..HEAD` and curate. Per-PR upkeep would be one
more hand-maintained list to drift, and the commit history already holds the
detail.

---

## v0.3.0

A small release with one consequential change: **the app no longer ships a
default model.** It has always run against any OpenAI-compatible provider, but
it read as a DeepSeek app and quietly sent your questions there unless you said
otherwise. That default is gone. Alongside it, two fixes for things that were
silently not working, and a dependency sweep.

### Read this before upgrading

- **`MODEL_DEFAULT` is now required — set it before you pull.** Earlier releases
  fell back to `deepseek/deepseek-v4-flash` when the setting was blank, so a
  deployment that never set it was using DeepSeek without choosing to. Nothing
  is chosen for you now: if a provider key is configured with no model, the app
  logs a **CRITICAL at startup** naming the problem, and every question fails
  upstream. Set `MODEL_DEFAULT` in your `.env` to a model your `LLM_BASE_URL`
  actually serves. `MODEL_ESCALATION` stays optional — blank means never
  escalate.
- **Nothing else to do.** There are no migrations in this release — `app.db`
  stays at schema 33, exactly where v0.2.0 left it — so rolling back to v0.2.0
  is safe and needs no snapshot. (Rolling back to v0.1.0 still refuses to start,
  as described under v0.2.0 below.)

### Fixed

- **Seed lessons now reach existing deployments.** New shipped exemplars only
  ever arrived on *fresh* installs: the seeder skipped its work whenever the
  `skills` table held any row at all, which is true of every deployment that has
  ever run. Found in the wild on v0.2.0, where an upgraded install sat on its
  original 3 lessons while the image shipped 8. Seeding is now tracked
  per-lesson, so each one arrives exactly once — including on databases that
  predate the change. Deleting a seed from Admin → Skills is still respected as
  a decision; it will not come back on the next restart.

### Added

- **Admin → Users shows "Last active" instead of "Last login".** The old column
  reported the last magic-link sign-in, so a colleague who signed in months ago
  and has asked questions every day since read as stale. It now shows the latest
  of the sign-in, their most recent conversation, and their most recent
  question, with the time as well as the date. It reads retroactively over
  history you already have. It is deliberately not a "last page hit" — a
  sign-in-and-browse session shows only the sign-in.

### Changed

- **The app presents as provider-neutral.** DeepSeek is no longer named in the
  docs, `.env.example`, or the configuration as though it were the product's
  choice of model. The one protection that is genuinely vendor-specific — a
  scrubber for tool-call markup that one model family leaks into its prose — is
  kept, because it is inert for every other provider and costs nothing.

### For developers

- `requirements.lock` is now checked against `requirements.txt` in CI. Nothing
  installs `requirements.txt` — CI and the Dockerfile both install the lock — so
  a raised floor with a stale lock was invisible drift that left every check
  green while the suites exercised the version that had not changed.
- Dependency sweep: FastAPI 0.140.7, Resend 2.35.0, React 19.2.8, Recharts
  3.10.1, Playwright 1.62.0, TypeScript 7, jsdom 30, ruff 0.16. The Playwright
  container image and `@playwright/test` must be bumped together or every e2e
  spec fails at browser launch.

---

## v0.2.0

The first release with real users in mind. The headline is **answer integrity**:
the app now checks its own numbers against the rows its queries returned and
tells you what it found. Around that, a security pass, an accessibility pass,
and the operational plumbing a self-hoster needs.

### Read this before upgrading

- **Rolling back to v0.1.0 will refuse to start.** Migrations are forward-only
  (this release takes `app.db` from schema 17 to 33), and rather than run against
  a schema it doesn't understand and silently corrupt your only irreplaceable
  file, an older build now logs a CRITICAL naming both versions and exits. To
  actually go back, restore the `app.db.pre-v<N>` snapshot the upgrade took
  alongside the older image.
- **Cached answers are no longer shared between users.** The semantic cache had
  no user predicate, so a colleague asking a similar question could be served
  your stored answer prose verbatim. It is now scoped to the person who asked.
  Expect the **Answer cache** count on Admin → Usage to fall — a popular question
  is now answered once per person rather than once per deployment. Rows written
  before the upgrade are reachable by nobody and get swept.
- **Editing or re-running an earlier question now asks first.** It always
  discarded every later exchange in the thread; now it says how many. Re-running
  your most recent question is unchanged and still modal-free.
- **Sign-in links changed shape** — the token now rides in the URL fragment.
  Links already sitting in inboxes keep working; there is nothing to do.

### Answer integrity

- Every answer's **hero figure** and **results-table cells** are checked against
  the rows the queries actually returned — verbatim, at the displayed rounding,
  or via a declared derivation (total, mean, share, % change, row total,
  cross-result share). Pure arithmetic, no second model.
- Readers see the verdict: a **✓ verified** figure and a per-answer line counting
  the values that reproduced. Where some didn't, the line asks you to **check them
  against the SQL or CSV** — phrased as an instruction, not a verdict, because
  every time it fired on real data it was a gap in the checker, not a model error.
- Admins get the rates on **Admin → Usage**: Grounded figures, Grounded cells,
  Answer leaks, and Exhausted.
- The checker itself was measured in both directions, repeatedly: table-row
  anchoring cut fabricated grounds from 24.0% to 0.63% with real cells unmoved,
  and pivot-row grouping later took recall from 83.3% to 98.0% with the
  fabrication rate unchanged.

### Trustworthy tables

- A truncated result **says so** ("First 200 rows · the full result is larger"),
  and sorting one warns that the sort covers only that page.
- The CSV button names what it gives you: **Download full result (CSV)**
  (re-run server-side) vs **Download these N rows (CSV)**.
- Numeric columns right-align; a failed query in the log no longer breaks the
  export.

### Chat

- **A running question survives navigation.** Leave and come back and you'll see
  it in progress, then the answer. Refreshing still cancels it — the browser now
  warns you first.
- **"Did you mean"** — a question that could be read two ways gets a short
  clarifying question with one-click answers instead of a guess.
- A stopped turn keeps its message ids, so **Rerun replaces it** rather than
  silently appending a duplicate.
- The composer highlights Markdown as you type; conversations rename inline.

### Admin

- **Attention badges** wherever work is waiting — pending requests, unverified
  lessons, new log problems, an available update — on the avatar and per section.
- **Users** is a three-tab section (current / pending / blocked) with bulk
  actions on every table.
- **About** shows the running version and whether a newer release exists; admins
  also get a non-dismissible banner until they're current.
- Imports no longer leaves a full spare copy of the dataset on disk after a swap.

### Security

- The magic-link token never appears in a server-visible URL: it rides in the
  fragment, `verify-info` takes it in a POST body, and `uvicorn.access` is
  scrubbed. Previously a `?token=` link was written to the access log on page
  load, before any API call — readable by anyone with `docker logs`.
- Request bodies are capped **before** authentication runs, so an unauthenticated
  POST can no longer spool megabytes to disk on its way to a 401.
- A single SQL result value is bounded, closing a path where one query allocated
  1.2 GB in under a second — faster than the timeout watchdog could fire.
- uvicorn no longer interprets `X-Forwarded-For` itself, which had defeated
  `TRUSTED_PROXY_COUNT=0` behind any loopback ingress.
- Per-user chat throttle (`CHAT_RATE_MAX_PER_USER`, default 30/60s).
- Magic links now last **30 minutes** rather than 15.

### Accessibility

The automated gate now fails on `serious` as well as `critical`, and scans the
app as it actually renders — a full answer, a mid-stream answer, and every admin
page, in both themes. Widening it found and fixed real defects: keyboard-
unreachable scroll regions, focus stranded on `<body>`, an invisible switch
thumb, unreadable WARNING text, and a chart toolbar hidden from assistive tech
while visible on screen.

### For developers

Backend suites run from one glob; the axe gate scans the real app; e2e runs
against a prebuilt bundle (3.4× faster locally); prop contracts are derived from
JSDoc and committed; screenshots regenerate with one command. Full detail in
[CONTRIBUTING.md](CONTRIBUTING.md).

---

## v0.1.0

First public release.
