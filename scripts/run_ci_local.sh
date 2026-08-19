#!/usr/bin/env bash
#
# run_ci_local.sh — run the full GitHub CI scope on this machine.
#
# This mirrors .github/workflows/ci.yml (the secrets · lint · unit · backend · e2e
# jobs) so a red check can be caught BEFORE it reaches GitHub. GitHub CI is now the
# authoritative server-side merge gate (main is branch-protected, all checks
# required); this stays as a FAST pre-check, wired up as a pre-push hook via
# .githooks/pre-push, so failures surface locally instead of after a push.
#
# Usage:
#   scripts/run_ci_local.sh            # run everything (lint, backend, e2e)
#   SKIP_E2E=1 scripts/run_ci_local.sh # skip the slow Playwright job
#
# It is intentionally faithful to CI: the backend suites run against a throwaway
# fixture ipeds.db (never the real 1.9 GB one) with COOKIE_SECURE=false, exactly
# as CI does. Bypass the pre-push hook entirely with `git push --no-verify`.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Prefer the project venv, fall back to whatever is on PATH.
PY="$REPO_ROOT/.venv/bin/python"
RUFF="$REPO_ROOT/.venv/bin/ruff"
[ -x "$PY" ] || PY="python3"
[ -x "$RUFF" ] || RUFF="ruff"

CI_DIR="$REPO_ROOT/.ci"
mkdir -p "$CI_DIR"

# --- ANSI helpers ---------------------------------------------------------
if [ -t 1 ]; then BOLD=$'\e[1m'; RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; RST=$'\e[0m'
else BOLD=""; RED=""; GRN=""; YEL=""; RST=""; fi

step() { printf '%s\n' "${BOLD}${YEL}==> $*${RST}"; }
fail() { printf '%s\n' "${BOLD}${RED}CI FAILED: $*${RST}" >&2; exit 1; }

# =========================================================================
# Job 0 — secret scan (gitleaks) — matches CI's "Secret scan (gitleaks)" job
# =========================================================================
# Runs only if gitleaks is on PATH; CI enforces it unconditionally, so a missing
# local binary downgrades to a warning rather than a false green. Install:
#   https://github.com/gitleaks/gitleaks (or `brew install gitleaks`).
if command -v gitleaks >/dev/null 2>&1; then
  step "Secrets: gitleaks (git history)"
  gitleaks git --no-banner --redact "$REPO_ROOT" || fail "gitleaks (secret detected)"
else
  printf '%s\n' "${YEL}Skipping secret scan — gitleaks not on PATH (CI still enforces it).${RST}"
fi

# =========================================================================
# Job 0b — SAST (semgrep) — matches CI's "SAST (semgrep)" job
# =========================================================================
# Fast pattern-based SAST complementing CodeQL. Runs only if semgrep is on PATH;
# CI enforces it unconditionally, so a missing local binary downgrades to a
# warning rather than a false green. Install (isolated from the app venv):
#   pipx install semgrep   # or: pip install --user semgrep
if command -v semgrep >/dev/null 2>&1; then
  step "SAST: semgrep (backend/app · frontend/src · scripts)"
  semgrep scan --error --quiet --metrics=off \
    --config=p/python --config=p/security-audit --config=p/javascript \
    --config="$REPO_ROOT/.semgrep" \
    "$REPO_ROOT/backend/app" "$REPO_ROOT/frontend/src" "$REPO_ROOT/scripts" \
    || fail "semgrep (SAST finding)"
else
  printf '%s\n' "${YEL}Skipping SAST — semgrep not on PATH (CI still enforces it). Install: pipx install semgrep${RST}"
fi

# =========================================================================
# Job 0c — dependency audit (pip-audit) — matches CI's "Dependency audit" job
# =========================================================================
# Audits backend/requirements.lock, the file the image and CI actually install
# and the one GitHub's dependency graph does not read (its pip parser ignores a
# .lock extension). Runs only if pip-audit is on PATH; CI enforces it
# unconditionally, so a missing local binary downgrades to a warning rather than
# a false green. Install (isolated from the app venv):
#   uv tool install pip-audit   # or: pipx install pip-audit
if command -v pip-audit >/dev/null 2>&1; then
  step "Dependency audit: pip-audit (backend/requirements.lock)"
  pip-audit --no-deps --strict --progress-spinner=off \
    -r "$REPO_ROOT/backend/requirements.lock" \
    || fail "pip-audit (vulnerable dependency)"
else
  printf '%s\n' "${YEL}Skipping dependency audit — pip-audit not on PATH (CI still enforces it). Install: uv tool install pip-audit${RST}"
fi

# =========================================================================
# Job 1 — lint (ruff · eslint)
# =========================================================================
step "Lint: ruff check backend/app backend/tests scripts"
"$RUFF" check --config backend/pyproject.toml backend/app backend/tests scripts || fail "ruff"

step "Lint: eslint (frontend)"
( cd frontend && npm run --silent lint ) || fail "eslint"

# Part of CI's Lint job, so it belongs here: frontend/types/ is COMMITTED, and
# `typecheck` re-emits it and diffs. Adding an export or renaming a prop without
# regenerating is a red CI check.
#
# This step was MISSING while CI had it (#220 added it to ci.yml only), which is
# the one failure mode this whole script exists to prevent: a green local gate
# and a red merge gate. Same "one list, not two" drift as the backend suite
# lists — whenever a step is added to ci.yml, add it here in the SAME PR.
step "Lint: types are in sync with the source (frontend)"
( cd frontend && npm run --silent typecheck ) || fail "typecheck (run 'npm --prefix frontend run types' and commit frontend/types/)"

# =========================================================================
# Job 1b — web unit tests (vitest: the fast pure-logic tier + JS coverage floor)
# =========================================================================
# vitest.config.js gates a per-file >=80% line floor over the pure-logic modules
# under test (the JS analogue of coverage_check.sh's per-module backend/app/ rule), so a
# failing unit test OR a coverage dip on those modules blocks the push.
step "Unit: vitest (frontend)"
( cd frontend && npm run --silent test:unit ) || fail "vitest unit tests"

# =========================================================================
# Job 2 — backend suites (against a throwaway fixture DB, like CI)
# =========================================================================
export IPEDS_DB_PATH="$CI_DIR/ipeds.db"
export APP_DB_PATH="$CI_DIR/app.db"
# Blank the settings a local prod .env would otherwise bleed into the tests.
# The list and the reasoning live in ci_env.sh — one copy, shared with
# coverage_check.sh, which CI also invokes on its own.
# shellcheck source=scripts/ci_env.sh
source "$REPO_ROOT/scripts/ci_env.sh"

# Fresh app.db each run so migration/backup suites start from a known state.
rm -f "$APP_DB_PATH"

step "Backend: build CI fixture database"
"$PY" scripts/make_ci_fixture_db.py "$IPEDS_DB_PATH" || fail "fixture db build"

# Ensure backend/app/main.py's SPA-serving block is active for the test run
# (no-op if a real frontend/dist build already exists).
"$PY" scripts/make_web_dist_stub.py

# Every backend suite, from ONE globbing list (scripts/run_backend_suites.sh).
# The old array here and the named steps in ci.yml were maintained separately
# and drifted -- two suites were in neither.
scripts/run_backend_suites.sh || fail "backend suites"

# Coverage gate — backend/app/ must stay >= 80% (re-runs suites under coverage.py).
step "Backend: coverage gate (backend/app/ >= 80%)"
"$REPO_ROOT/scripts/coverage_check.sh" || fail "coverage < 80%"

# =========================================================================
# Job 3 — Playwright e2e (network-mocked UI)
# =========================================================================
if [ "${SKIP_E2E:-0}" = "1" ]; then
  printf '%s\n' "${YEL}Skipping e2e (SKIP_E2E=1).${RST}"
else
  step "e2e: playwright (frontend)"
  # E2E_PREVIEW=1 runs the suite against a prebuilt static bundle instead of the
  # Vite dev server, matching CI. The dev server transforms modules on demand
  # PER ROUTE, and the suite pays that on every page.goto across 300+ tests --
  # measured as the bulk of a run whose fastest test is 2.8s. The one-off build
  # is amortised over the whole suite, so it only makes sense here (the full
  # gate), not for iterating on a single spec.
  #
  # It also turns OFF reuseExistingServer, so this can never run against a stale
  # preview server -- see the note in playwright.config.js.
  ( cd frontend && E2E_PREVIEW=1 npm run --silent test:e2e ) || fail "playwright e2e"
fi

printf '%s\n' "${BOLD}${GRN}All CI checks passed.${RST}"
