# IPEDS Oracle — process

This file holds process only: how work is done in this repo. It does not hold
architecture, feature behavior, rationale, or history.

- What the system does → `docs/ARCHITECTURE.md`, `docs/AGENT_LOOP.md`,
  `docs/AUTH_AND_SECURITY.md`, `docs/ADMIN.md`, `docs/MCP.md`
- The data model, and how to query it → `docs/SCHEMA.md`, `docs/DATASET.md`
- How to run, lint, and test it locally → `CONTRIBUTING.md`
- What each gate runs, and what has slipped past one → `docs/TESTING.md`
- How a release is published and deployed → `docs/RELEASING.md`, README → **Self-hosting**
- What the app looks like to its users → `docs/USER_GUIDE.md`, `docs/ADMIN_GUIDE.md`

Before adding a line here, ask whether it would still be true if the process
changed. If yes, it belongs in one of those. Do not append feature decisions,
measurements, incident write-ups, or status to this file. Under 150 lines; if it
needs to grow, something in it belongs somewhere else.

## Read before you start

Read the document before touching the code it governs. Do not work from a
summary of it, including this one.

| Before touching | Read |
|---|---|
| any SQL, or any number in an answer | `docs/SCHEMA.md`, then `docs/DATASET.md` — the CIP/`awlevel` rollups and the result-size caps |
| the agent loop, the prompt, grounding, the critic, lessons, the cache | `docs/AGENT_LOOP.md` |
| auth, sessions, CSRF, rate limits, security headers | `docs/AUTH_AND_SECURITY.md` |
| imports, the dataset swap, usage or spend | `docs/ADMIN.md` |
| a persisted message field, or the chat / admin frontend | `docs/ARCHITECTURE.md` |
| a test, a gate, or a coverage floor | `docs/TESTING.md` |
| anything an operator sees — image, compose, ports, uid | `docs/RELEASING.md` |
| the MCP endpoint, an API key, or the `ask` tool | `docs/MCP.md` |

## Choosing the path

The routing test is **design uncertainty or large blast radius**, not "touches
multiple files." Route through the `.claude/agents/` team — `project-manager`
orchestrates `architect` → `test-engineer` → `implementer` → the reviewers —
only when the design is genuinely uncertain or the change reaches far.

Everything else goes **inline with a review pass at the end**, even across a few
files. Follow-on fixes to a shipped feature default to inline. **Say which path
you are taking** before you start.

`test-engineer` owns test files, and `implementer` may not edit them, **on the
team path only**. Inline, whoever writes the code writes its tests.

## Testing

Test-first for behavior that can realistically regress: ownership and authz
scoping, persistence invariants, security contracts, aggregation correctness.
Fix presentation trivia — strings, labels, singular/plural, cosmetic shape —
directly.

**Every new test names the specific regression it catches.** One that only
re-echoes a constant or a UI string a function away is noise and does not ship.

**Pick the lowest tier that actually catches the regression.** Pure logic is
vitest; genuine browser truth — routing, focus, aria-live, back/forward,
SSE-driven DOM — is Playwright. Do not boot a browser to check a pure function,
and do not unit-test a navigation truth jsdom will fake and get wrong. When a
pure function is pinned only through an e2e assertion, move it down and thin the
redundant e2e check.

**Every `backend/app/` module stays ≥ 80% line coverage, per module, not just in
total**, and any `src/foo.js` with a co-located `src/foo.test.js` stays ≥ 80%
per file. Meet the floor with tests that guard real behavior; never pad it with
assertions on constants. New low-coverage code is not done until it is tested.

`docs/TESTING.md` has the tiers, the gates, and the traps that have made a test
pass while the code was broken. Read it before writing a test that involves
focus, a popover, an axe scan, or a fixture.

## Gates

**Run `scripts/run_ci_local.sh` before pushing.** It reproduces CI; the
`.githooks/pre-push` hook runs it for you (`--no-verify` to bypass, `SKIP_E2E=1`
to skip e2e). It is a fast pre-check — **the authoritative gate is GitHub CI**,
and `main` is branch-protected.

**Never merge with a check red, stale, or still running.** Not "it is
unrelated," not "it passes locally."

**Never skip, xfail, or delete a failing test to get a gate green.** A failing
test is finding a real defect or is itself wrong. If the test is wrong, fix it
in its own commit and say in the PR why the old assertion was incorrect.

**Never lower a floor or narrow a gate to get a change through.** A floor moves
only in a PR whose subject is moving it and whose body says why the new number
is right.

**Any new setting that changes behavior gets blanked in `scripts/ci_env.sh` in
the PR that adds it.** CI has no `.env`, so a bleed fails only on the developer's
box — the one place the merge gate runs.

**Dependencies:** run `npm audit` in `frontend/` before and after any lockfile
change and state the delta in the PR; move the Playwright container tag in
`ci.yml` with `@playwright/test`; regenerate `requirements.lock` in the same PR
that moves a floor in `requirements.txt`; combine dependabot PRs that rewrite
the same lockfile into one.

## Branch and pull request discipline

You cannot commit to `main` — branch protection blocks it, and that is the
intent, not an obstacle to route around.

1. Branch (`feat/…`, `fix/…`, `chore/…`, `docs/…`).
2. Keep the PR focused on one item.
3. Open the PR, then **watch CI without blocking**: run `gh pr checks <n>
   --watch` as a background task and keep working.
4. Merge only when lint · unit · backend · e2e · image are all green.

End commit messages with the `Co-Authored-By:` trailer.

## After a merge

**A green PR is not an all-clear.** Code-scanning findings never block a merge
and sit silently in the Security tab, so check the queue every time:

```bash
gh api "repos/toddawhittaker/ipeds-oracle/code-scanning/alerts?state=open" \
  --jq '.[] | "\(.number)\t\(.rule.security_severity_level)\t\(.rule.id)\t\(.most_recent_instance.location.path):\(.most_recent_instance.location.start_line)"'
```

**Triage, don't dismiss.** Probe an alert both ways before deciding, and put the
answer in a code comment. A queue with a permanent red item trains you to stop
looking.

## Two sessions at once

Two sessions in one clone share a working tree: a `git checkout` in one moves
the other's branch mid-edit. Give each its own worktree
(`scripts/worktree-add.sh <branch>`; see `CONTRIBUTING.md` → *Running two
sessions at once*). Check `git branch --show-current` and `git status` before
any git write, and **never `git add -A` in a worktree**.

## Keeping the docs true

Update the right document in the **same PR** as the change. The split is by
kind, and writing to the wrong file is how both halves rot: process rules here,
how a subsystem works in its `docs/` file, contributor mechanics in
`CONTRIBUTING.md`, operator facts in the README's **Self-hosting** section.
This file is loaded into every session — add the pointer, not the paragraph.

**A major architecture or workflow change also sweeps `.claude/agents/`.** The
specialist definitions reference the tiers, features, and rules, and they go
stale silently. Fold the sweep into the same PR when it is small; otherwise ship
it as an immediate focused follow-up.
