# Testing and the gates

`CLAUDE.md` states the rules — test-first for real regressions, the per-module
coverage floors, pick the lowest tier, run the gate before pushing. This file is
the detail behind them: what each tier is for, what each gate actually runs, and
the traps that have made a test pass while the code was broken. Read the
relevant part before writing a test or changing a gate.

## The standard

**Testing standard — non-negotiable, but a floor met with real tests.** Keep
test-first for behavior that can realistically regress (ownership/authz scoping,
persistence invariants, security contracts, aggregation correctness); fix
presentation trivia (strings, labels, singular/plural, cosmetic shape) directly.
Every new test must **name the specific regression it catches** — one that only
re-echoes a constant or a UI string a function away is noise and doesn't ship.
**Every `backend/app/` module stays ≥ 80%** line coverage (per-module, not just the
total) — enforced by `scripts/coverage_check.sh` in CI and the pre-push gate —
but that floor is met with tests that **guard real behavior**, never padded with
assertions on constants. Tests are dependency-light scripts in `backend/tests/`
(`sys.exit(1)` on failure, no API key needed). New low-coverage code is not
"done" until it's tested.

## The test pyramid

**Test pyramid — pick the lowest tier that actually catches the regression.**
*Pure logic* — functions and leaf modules with real input→output behavior — is
unit-tested with **vitest** (`frontend/`, jsdom, no browser; co-located
`frontend/src/*.test.js`, table-driven). *Genuine browser truth* —
routing/navigation, focus management, aria-live/AT announcements, back/forward,
SSE-driven DOM — stays in **Playwright** (`frontend/e2e/`). jsdom's focus and history
models are **not** the browser's, so component tests that lean on routing,
portals, or focus belong in Playwright, not vitest. Don't boot a browser to
check a pure function; don't unit-test a navigation truth jsdom will fake and
get wrong. When a pure function is currently pinned through an e2e assertion,
**move it down** to vitest and thin the now-redundant e2e logic check — keep the
browser *flow* (focus, the aria-live announcement firing) around it. **JS
coverage is gated:** `frontend/vitest.config.js` enforces a per-file ≥80% line floor
over the pure-logic modules under test — the JS analogue of `coverage_check.sh`'s
per-`backend/app/`-module rule. The set is **derived from the filesystem** (any
`src/foo.js` with a co-located `src/foo.test.js`), so writing the test is the whole
opt-in and a tested module can't stay silently ungated. Browser-tested components
(`Chat.jsx`, `src/admin/*.jsx`, …) have no `*.test.js` and so stay out of the floor —
Playwright covers them. The derivation walks `src/` **recursively**; it must, or a
module in a subdirectory escapes the floor silently (see the `src/admin/` note above).
**Open a `HelpPopover` in e2e with `focus()` or `tap()`, never a bare
`click()`** — the component opens on hover AND focus while its `onClick`
**toggles**, so a mouse click on an already-open popover closes it, which is
that handler's intent and not a bug. **The click-swallow latch is armed from
`onPointerDown`, and the reason is the whole point of the component's history.**
It used to arm from `onFocus` behind `if (!open)`, which assumed focus arrives
before the wrapper's `mouseenter` has committed `setOpen(true)`. On a REAL touch
tap it does not: Chromium emits the compatibility mouse events first
(pointerdown → touchstart → mouseenter/mousedown → focus → click), so `open`
already read true, the latch never armed, and **every tap closed the popover it
had just opened** — with Admin → Usage telling the admin to "Hover or tap the
ⓘ". `pointerdown` lands before that compat `mouseenter`, so `open` is reliably
false there; `pointerType !== "mouse"` keeps a genuine mouse click toggling, and
the `!open` test is what lets a SECOND tap dismiss (arming on every touch
pointerdown would swallow that one too, and since it fires no new focus nothing
would ever clear the latch). Pinned by a `test.use({ hasTouch: true })` describe
in `csv-import.spec.js` whose assertions are **synchronous** past `closeSoon`'s
140 ms timer — an auto-retrying matcher would wait out a transiently-open
popover. **All eight earlier specs passed with this bug present**, including one
NAMED for the touch tap that staged `focus()` before `click()` and so armed the
latch cleanly; it has been replaced. Two lessons, both still live: **when a
flake has a candidate mechanism, construct the input that FORCES the bad branch
rather than sampling for it** (repetition proved nothing, while a throwaway
spec that hovered-then-clicked failed 5/5), and **a test named for a scenario it
cannot actually produce is worse than no test** — it reads as coverage.
**The axe gate (`frontend/e2e/a11y.spec.js`) fails on `critical` AND `serious`,
and now SCANS THE APP** — a rendered answer with its disclosures open, a
MID-STREAM answer, and all seven admin paths in **both themes** (19 scans).
It previously saw only Login and the EMPTY Chat state, i.e. the two
least-populated screens: every control the product is made of, and every
admin page, sat outside the gate. That is a COVERAGE hole, not a threshold
one — and it is how two whole classes of defect shipped past a green suite.
Widening it immediately found two `serious` violations on `main`: the hidden
PNG-export chart (`aria-hidden-focus` — recharts renders a focusable svg, so a
keyboard user could Tab into an invisible chart that announces nothing; fixed
with **`inert` AS WELL AS `aria-hidden`**, the pair being the point — one
removes it from the a11y tree, the other from the focus order), and an
`aria-label` on a **roleless `<span>`** in Admin → Skills
(`aria-prohibited-attr` — silently IGNORED, so the ▲/▼ vote counts reached a
screen reader as bare glyphs; replaced with `.sr-only` text rather than
`role="img"`, which prunes descendants). Two fixture rules the scans depend
on: mock admin lists with **CONTENT, not empty arrays** (an empty table
renders none of the elements whose contrast could be wrong — the WARNING log
level at 2.52:1 needed a WARNING record to exist), and the answer fixture must
carry a **CHART** (the shared table-only one left the chart defect outside the
gate even after the answer scan was added).
`critical`-only was not a strict threshold but a shaped blind spot: axe rates
colour-contrast, `aria-prohibited-attr`, `scrollable-region-focusable` and
`heading-order` as **`serious`**, i.e. the whole class this suite exists to catch
scored under the bar. Three scans: Login, Chat, and **Chat in the DARK theme as an
admin with an attention badge** — the light-theme non-admin scans structurally
could not render the elements whose contrast was broken. Two hard-won limits:
**(1)** the Login scan runs under `emulateMedia({reducedMotion:"reduce"})`, because
the door's figure gallery auto-advances every 5s through a .34s fade and axe
sampling mid-fade measures the *blended* colour — reporting 3.56:1 against text
that rests at 4.85:1. Scan resting pixels, not a transient frame. **(2)** axe files
a one-character element as **`incomplete`, not a violation** ("content is too short
to determine if it is actual text content"), so the count badge that sat at 2.43:1
on every admin's screen was invisible to the gate — and `incomplete` is not gatable
in general (it also holds the composer's deliberate 1:1 transparent-textarea
overlay). Contrast on such elements needs a **direct computed-style assertion**
(`contrastRatio()` in that spec measures resolved pixels, pinning readability
rather than a colour literal). `--on-fg` is the token for text on an `--accent`
fill; a hardcoded `#fff` there is the recurring bug.
**(3) axe only contrast-checks text INSIDE the viewport** —
`colorContrastEvaluate` opens with `if (!_isVisibleOnScreen(node)) return true`,
a PASS, not an incomplete. This app pins `html, body { overflow: hidden }` and
gives every screen its own inner scroller, so at Playwright's 1280×720 default
everything below the fold went unmeasured: **34% of text nodes on
`/admin/logs`**, and a real 4.44:1 violation sitting at y=767 that the scan
reported clean. The axe describe therefore sets **1280×2600**, and any new scan
needs it or it is theatre. Widening it also exposed a latent mid-animation flake
elsewhere, so **`reducedMotion: "reduce"` now applies to EVERY scan**, not just
Login's — that reasoning was never Login-specific, Login was just the only scan
close enough to the top of the page to be bitten.
**(4) `aria-prohibited-attr` returns `incomplete`, not a violation, whenever the
element has text content** — so `aria-label` on a roleless `<span>` (role
`generic`, where ARIA prohibits it) is never gated. Worse, Playwright's
`getByLabel` computes the name WITHOUT applying the role prohibition, so an e2e
assertion on it passes while screen readers ignore the attribute outright. Use
`.sr-only` text instead, as `Skills.jsx` does.

## The suites, and the lists that feed them

**One list, not two, for the backend suites:** `scripts/run_backend_suites.sh`
globs `backend/tests/test_*.py` and is called by BOTH `run_ci_local.sh` and CI's
backend job. It replaced a hand-kept array plus ~30 hand-written CI steps that had
drifted — `test_grounding.py` and `test_version.py` were in neither, running only
inside `coverage_check.sh`'s glob with output sent to `/dev/null`, so a grounding
failure read as "coverage gate failed". Adding a suite is now just adding the file;
`coverage_check.sh` replays a failing suite's output instead of discarding it.
Similarly `.env.example` is pinned against `config.Settings` in both directions by
`backend/tests/test_env_example.py`, and **`requirements.lock` is pinned against
`requirements.txt`** by `backend/tests/test_requirements_lock.py`: every direct
dependency must be locked, and the locked version must satisfy the declared floor.
Nothing installs `requirements.txt` — CI and the Dockerfile both install the
**lock** — so a raised floor with a stale lock is invisible drift that leaves every
check green while the suites exercise the version that did *not* change. Dependabot
does exactly this (it cannot run `pip-compile`): #253/#254 each raised a floor above
the pinned version and went fully green. Regenerate with
`pip-compile --generate-hashes --output-file=requirements.lock requirements.txt`
in the same PR that moves a floor.

**The npm side has no equivalent gate — `npm audit` is the check nothing runs.**
CI never invokes it, so a vulnerable transitive is invisible to a fully green
suite, and a dependabot title gives no hint: of three routine-looking npm PRs in
#276, **two were security fixes** (js-yaml 4.3.0→4.3.1 cleared a HIGH, postcss
8.5.19→8.5.26 a MODERATE) and a third HIGH (`brace-expansion`) was sitting
unreferenced by any of them. Run `npm audit` in `frontend/` on `main` **before
and after** any lockfile change and state the delta in the PR; the frontend
currently audits at **zero**, which is only a useful baseline if it is checked.
Two traps behind that: **`npm install` will NOT move a transitive that already
satisfies its parent's range** — js-yaml stayed on the vulnerable version
through a full `npm install --package-lock-only` and needed `npm update
js-yaml`, and a lockfile that still carries the advisory is byte-indistinguishable
from a fixed one at a glance — and **`ci.yml`'s Playwright container tag must
move with `@playwright/test`** (both at 1.62.1). #269 moved the package alone and
went green, because a PATCH pair happens to share a browser build; that is
exactly why the drift survives review until a bump where it does not.
**Prefer ONE PR when several dependabot PRs rewrite the same lockfile.** `main`
is `strict: true`, so each merge puts the rest behind and forces them to
regenerate that file against a moved base — #269/#273/#274 were combined into
#276 for the same reason #271 combined #267/#268.

## The local gate and static analysis

**Run the full gate before pushing.** `scripts/run_ci_local.sh` reproduces all of
CI (a **gitleaks** secret scan + a **semgrep** SAST pass, each when the binary is on
`PATH`; ruff over `backend/app backend/tests scripts` + ESLint; the `frontend/`
**vitest** unit tests; the `backend/tests/` backend suites against a fixture DB;
Playwright e2e — run against a **prebuilt static bundle**, `E2E_PREVIEW=1`, which
is 3.4× faster over a full run than the dev server that re-transforms modules per
`page.goto`; reuse is off in that mode or the suite runs against **stale source
and reports a false green**). A `.githooks/pre-push` hook runs it automatically
(bypass: `git push --no-verify`; skip e2e: `SKIP_E2E=1`). A **deletion-only push** skips the
gate — it ships no code — while a push mixing a deletion with commits still runs it
(`test_pre_push_hook.py`). It's a **fast pre-check** so failures
surface before CI — but since the repo went public the **authoritative gate is
GitHub CI**: `main` is **branch-protected** (a PR is required; the required checks
must be green AND up to date before merge; force pushes and direct pushes are
blocked). The **secrets** job runs gitleaks over full history as defense-in-depth
under GitHub's native secret-scanning + push-protection (both enabled). Admin
override is left enabled only as a safety valve for a flaky check.

**Static analysis — two layers, complementary.** **CodeQL** (`.github/workflows/codeql.yml`,
`security-extended`, scoped to non-test code) runs on every PR/push and is the
authority on **cross-file taint** (its py/log-injection caught a request `tz` param
logged in another module — CodeQL alerts surface in the Security tab; NB they don't
block a merge unless code-scanning *merge protection* is enabled in repo settings).
The three `github/codeql-action/*` steps are pinned to an **exact patch**
(`@v4.37.4`, was the floating `@v4`) — a reviewable diff for every CodeQL change
instead of silently riding whatever the major tag moves to, at the cost of a
dependabot PR per patch release.
**Semgrep** (the CI **SAST (semgrep)** job + the local gate) is the fast pattern
layer — `p/python` · `p/security-audit` · `p/javascript` plus repo-local rules in
**`.semgrep/`** (a CWE-117 log-injection rule). It runs `--error` (any finding fails
the job) over `backend/app` · `frontend/src` · `scripts`. It is **NOT** a CodeQL
substitute — semgrep OSS does INTRA-file taint only, so cross-file flows stay
CodeQL's job; the two overlap deliberately. Install semgrep isolated from the app
venv (`pipx install semgrep`) so it never enters the app's runtime deps.

## Test-env bleed

**Test-env gotcha.** A production `.env` (`COOKIE_SECURE=true`, real keys,
`EMAIL_DOMAIN=…`) bleeds into tests — run auth suites with `COOKIE_SECURE=false`,
and blank `LLM_API_KEY`/`RESEND_API_KEY`/`EMAIL_DOMAIN` to match CI's key-free
environment. **`scripts/ci_env.sh` is the single list of those blanks** — sourced
by both `run_ci_local.sh` and `coverage_check.sh`. **Any new setting that changes
behavior has to be blanked in `ci_env.sh`, in the PR that adds it.** CI has no
`.env`, so a bleed fails only on the developer's box, which is also the only
place the merge gate runs. (The list used to be duplicated per script and drifted
silently — `coverage_check.sh` was missing `EMAIL_DOMAIN`, which no gate could
catch, since `run_ci_local.sh` exported it before calling that script.)

## Reading the CodeQL queue

**A green PR is NOT an all-clear — check CodeQL separately, every time.**
Code-scanning alerts do **not** block a merge (merge protection is off), and the
`CodeQL` check going green means *the analysis ran*, not that it found nothing.
So a finding lands silently in the Security tab and stays there. After a merge:

```bash
gh api "repos/toddawhittaker/ipeds-oracle/code-scanning/alerts?state=open" \
  --jq '.[] | "\(.number)\t\(.rule.security_severity_level)\t\(.rule.id)\t\(.most_recent_instance.location.path):\(.most_recent_instance.location.start_line)"'
```

Todd had to point out alert #44 himself, several PRs after it appeared — the
whole point of the tool is that it catches what review doesn't, which is
worthless if nobody reads the queue. **Triage, don't just dismiss:** #44
(`py/url-redirection`) was genuinely not exploitable, and it was still worth
fixing rather than annotating away, because a queue with a permanent red item in
it trains you to stop looking. Probe an alert both ways before deciding — the
probe is what tells you whether you're patching a hole or hardening a
non-hole, and the answer belongs in the code comment.
