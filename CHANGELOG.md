# Changelog

Notable changes per release. The same text is published as the body of each
[GitHub Release](https://github.com/toddawhittaker/ipeds-oracle/releases) —
that's what the in-app "update available" banner links to.

Written **at tag time**, not per PR: seed it from
`git log --oneline <previous-tag>..HEAD` and curate. Per-PR upkeep would be one
more hand-maintained list to drift, and the commit history already holds the
detail.

---

## v0.4.0

The hardening release. Nothing here changes what the app is for — it is 48
commits of closing holes, most of them found by reading the code rather than by
anything going wrong in production. The container no longer runs as root, four
importer paths that could report the wrong outcome now report the right one, a
single SQL query can no longer exhaust the container's memory, and the two
"grounded" figures on Admin → Usage stopped crediting numbers they could not
actually verify.

### Read this before upgrading

- **Run `sudo chown -R 10001:10001 ./srv-data` before you pull.** The container
  now runs as the numeric uid/gid **10001**, and Docker never chowns a bind
  mount for you. Skip this and the app **exits on first boot** rather than
  starting — printing the exact command and the uid it is running as. That is a
  startup check doing its job, not a broken release. If your host files must
  keep another owner, set `IPEDS_UID`/`IPEDS_GID` in `.env` instead.
- **`BIND_ADDR` now defaults to loopback.** `compose.yaml` publishes :8000 on
  `127.0.0.1` instead of `0.0.0.0`, because Docker inserts published ports into
  its own iptables chain that a host `ufw`/`firewalld` policy does **not**
  filter — so the old default was reachable from the network however the host
  firewall was set. If you reach the app directly by LAN address rather than
  through a proxy or tunnel, set `BIND_ADDR=0.0.0.0` explicitly.
- **Rolling back to v0.3.0 refuses to start.** `app.db` goes from schema 33 to
  35 in this release. To actually go back, restore the `app.db.pre-v33` snapshot
  the upgrade takes automatically, alongside the older image.
- **Running the backup script inside the container needs `BACKUP_DIR`.** Its
  `--out-dir` defaults to a relative `backups/`, which uid 10001 cannot create.
  Set `BACKUP_DIR=/data/backups`. Running it on the host against the bind mount
  is unaffected. Likewise, if you override `EMBED_MODEL`, set
  `FASTEMBED_CACHE_PATH=/data/models` — the baked model cache is read-only.

### Numbers on Admin → Usage will move

Both of these are the meter getting more honest, not a regression.

- **Spend goes up, and may now show a `~`.** Every LLM call a turn causes is
  billed, not just the agent's — the topical guard (which runs on *every*
  question, including one the answer cache serves), the title call, and the
  feedback distiller were all invisible before. Pulling the other way,
  cached-prompt tokens are now priced at their own much lower rate, which took a
  measured estimate from 5.0× over the provider's real bill to about 1.5×. A
  tile whose window contains any estimated row is marked `~` and says how many.
- **Grounded figures steps down and Grounded cells with it.** A total computed
  over a truncated page can no longer ground itself against those same partial
  rows. The kernel was corroborating the exact error it exists to catch.

### Security

- **The container runs as a non-root user** (uid/gid 10001) with
  `no-new-privileges` and all Linux capabilities dropped.
- **A startup preflight explains itself.** An unwritable data directory used to
  surface as a `sqlite3.OperationalError` traceback from inside uvicorn's app
  import, mentioning neither ownership, nor the uid, nor the fix. It now exits
  with a plain instruction naming every failing path.
- **HSTS**, sent only from a deployment whose `APP_PUBLIC_URL` is actually
  https, with no `includeSubDomains` and no `preload` — a blanket policy would
  force https on anything else the host serves.
- **CSV exports can't smuggle a formula.** A cell (or a column header, which
  comes from SQL aliases the model wrote) beginning `=`, `+`, `-`, `@`, tab or
  carriage return is prefixed so Excel treats it as text.
- **One SQL query can no longer exhaust the container.** Three bounds, each
  added after the previous was defeated: one value, one row, and the whole
  result. Measured worst case went from 5,046 MB to 35 MB, with an ordinary
  100k-row export unchanged at 0.18s.
- **A restart no longer re-grants a removed admin.** An address in
  `ADMIN_EMAILS` was re-promoted on every boot, so demoting someone lasted until
  the next restart.

### Data safety

- **Four importer paths reported the wrong outcome.** A failure *after* the
  atomic swap wrote `status='failed'` over a dataset that was already live, and
  the tab toasted "the live database was not changed" — the opposite of the
  truth. The NCES integrate path and year-removal had the same bug.
- **Staging records what it is about to change, before it changes it.** A
  disk-full mid-batch could strand a previous year's file as `<name>.accdb.bak`,
  which the year-drop guard then refused to see — blocking every later import.
- **A manual import stops leaving 1–3 GB of Access files on disk forever**, and
  "is this the same file?" is now decided by device+inode identity rather than
  by comparing path strings, which also catches a hard link.
- **Migrations are atomic**, so a part-way failure no longer bricks every later
  boot on "duplicate column name".

### Answers

- **A truncated result may not supply the total the model claimed.** See above.
- **The reviewer's findings are gated before they become lessons.** Only the
  five data-modeling categories can be learned; the prompt no longer reveals
  which, so relabeling cannot route around the gate. The revision round still
  fires for every category.
- **A rejected lesson is remembered.** Rejecting used to delete the only
  evidence that would have suppressed the next identical proposal, so the same
  lesson came back forever. Admin → Skills gains a category pill, a
  **Reject & mute** action, and collapsed **Rejected (N)** / **Muted
  categories (N)** sections with Undo and Unmute.
- **A provider's non-JSON 200 no longer kills the stream** and loses the turn.
- **Tool calls run off the event loop.** One 25-second query used to stall every
  other user's stream, the admin console, and `/api/health`.

### Chat and admin

- **A stopped turn's note is true now.** It said "reopen it in a moment to
  check" and nothing in the app could do that. It now says whether the answer is
  still being written or has been saved, and offers a **Check now** that works.
- **Edit, Rerun and a suggestion chip scroll your new question into view**, the
  way Send always has.
- **A copy that failed says so** instead of looking like it worked.
- **A failed admin refresh says so** instead of leaving stale rows looking
  current, and an empty table no longer means "nobody can sign in" when the
  truth is the list could not be fetched.
- **Integrate confirms first** and says what the rebuild will actually do; a
  rebuild started by another session locks the tab and says who started it.
- **The row cap in a truncated-table caption comes from the server**, not a
  hardcoded 200.
- **A crash leaves a way out.** A throw in one route no longer takes the account
  menu with it, and the offered "Reload" no longer recommends the one action
  that discards an answer still being written.

### Accessibility

- **Every admin table scrolls inside its own region** (WCAG 1.4.10) instead of
  making the whole page scroll in two directions.
- **A touch tap opens the help popover** instead of opening and immediately
  closing it — while the Usage tab was telling admins to "hover or tap the ⓘ".
- **The axe gate scans below the fold**, in a tall viewport. It only
  contrast-checks text inside the viewport, so a third of `/admin/logs` was
  going unmeasured; widening it found four real fixes.
- Two AA contrast fixes, and the help popover is keyboard-reachable.

### For developers

- The persisted-answer field list, the `done` event's fields, and the browser
  side of both are all **derived** now rather than hand-maintained in ten
  places — a miss used to render correctly after a refresh and wrongly only on
  the turn that produced it.
- CI: every job has a timeout; `ci_env.sh` pins the two settings that bleed from
  a developer's `.env`; the CodeQL action is pinned to an exact patch.
- Dependencies swept, including three advisories `npm audit` was never run to
  find.

---

## v0.3.0

A small release with one consequential change: **the app no longer ships a
default model.** It has always run against any OpenAI-compatible provider, but
it read as a DeepSeek app and quietly sent your questions there unless you said
otherwise. That default is gone. Alongside it: shipped lessons that were never
reaching existing deployments now do, Admin → Users answers a more useful
question, and the dependencies are swept.

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
