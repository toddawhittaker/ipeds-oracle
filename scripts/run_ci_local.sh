#!/usr/bin/env bash
#
# run_ci_local.sh — run the full GitHub CI scope on this machine.
#
# This mirrors .github/workflows/ci.yml (the lint · backend · e2e jobs) so a red
# check can be caught BEFORE it reaches GitHub. It exists because branch
# protection / required status checks are not available on this private repo's
# plan, so CI is not a server-side merge gate — this is the client-side gate
# (wired up as a pre-push hook via .githooks/pre-push).
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
# Job 1 — lint (ruff · eslint)
# =========================================================================
step "Lint: ruff check app scripts eval"
"$RUFF" check app scripts eval || fail "ruff"

step "Lint: eslint (web)"
( cd web && npm run --silent lint ) || fail "eslint"

# =========================================================================
# Job 2 — backend suites (against a throwaway fixture DB, like CI)
# =========================================================================
export IPEDS_DB_PATH="$CI_DIR/ipeds.db"
export APP_DB_PATH="$CI_DIR/app.db"
# CI runs with NO .env; a local prod .env (loaded by app/config.py via an
# absolute path, so CWD tricks don't help) bleeds real settings into the tests.
# OS env vars take precedence over the .env file in pydantic-settings, so blank
# the ones that change behavior to reproduce CI's key-free environment:
#   * COOKIE_SECURE=false — a prod true drops the Secure cookie under the http
#     TestClient -> spurious "Not signed in".
#   * OPENROUTER_API_KEY blank — with a key present the chat/agent suites take
#     the LIVE path and hit the fixture's absent tables (e.g. _family_map);
#     CI (no key) takes the deterministic short path.
#   * RESEND_API_KEY blank — so the mailer logs the link instead of sending
#     REAL email through Resend during tests.
export COOKIE_SECURE=false
export OPENROUTER_API_KEY=""
export RESEND_API_KEY=""

# Fresh app.db each run so migration/backup suites start from a known state.
rm -f "$APP_DB_PATH"

step "Backend: build CI fixture database"
"$PY" scripts/make_ci_fixture_db.py "$IPEDS_DB_PATH" || fail "fixture db build"

# Ensure app/main.py's SPA-serving block is active for the test run (no-op if a
# real web/dist build already exists).
"$PY" scripts/make_web_dist_stub.py

BACKEND_SUITES=(
  test_sql_guards.py
  test_sql_guards_hardening.py
  test_backend.py
  test_security.py
  test_rate_limit.py
  test_migrations.py
  test_result_isolation.py
  test_backup.py
  test_agent_loop.py
  test_logbuffer.py
  test_mailer.py
  test_guard.py
  test_importer.py
  test_schema_tool.py
  test_registry.py
  test_chat_router.py
  test_admin_router.py
)
for suite in "${BACKEND_SUITES[@]}"; do
  step "Backend: eval/$suite"
  "$PY" "eval/$suite" || fail "eval/$suite"
done

# Coverage gate — app/ must stay >= 80% (re-runs suites under coverage.py).
step "Backend: coverage gate (app/ >= 80%)"
"$REPO_ROOT/scripts/coverage_check.sh" || fail "coverage < 80%"

# =========================================================================
# Job 3 — Playwright e2e (network-mocked UI)
# =========================================================================
if [ "${SKIP_E2E:-0}" = "1" ]; then
  printf '%s\n' "${YEL}Skipping e2e (SKIP_E2E=1).${RST}"
else
  step "e2e: playwright (web)"
  ( cd web && npm run --silent test:e2e ) || fail "playwright e2e"
fi

printf '%s\n' "${BOLD}${GRN}All CI checks passed.${RST}"
