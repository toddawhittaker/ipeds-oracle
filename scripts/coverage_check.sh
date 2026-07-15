#!/usr/bin/env bash
#
# coverage_check.sh — enforce the project's >=80% app/ coverage standard.
#
# Runs every eval/test_*.py suite under coverage.py against a throwaway fixture
# ipeds.db (key-free, like CI) and fails if combined app/ line coverage drops
# below the threshold. Auto-discovers suites by glob, so a new eval/test_*.py is
# included automatically. eval_nl2sql.py is excluded (needs a real DB + API key).
#
# Used by CI (backend job) and by scripts/run_ci_local.sh. Override the bar with
# COVERAGE_MIN (default 80).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY="$REPO_ROOT/.venv/bin/python"
COV="$REPO_ROOT/.venv/bin/coverage"
[ -x "$PY" ] || PY="python3"
[ -x "$COV" ] || COV="coverage"

COVERAGE_MIN="${COVERAGE_MIN:-80}"

# Match CI's key-free environment (a real .env would send suites down live paths).
export COOKIE_SECURE=false OPENROUTER_API_KEY="" RESEND_API_KEY=""

CI_DIR="$REPO_ROOT/.ci"
mkdir -p "$CI_DIR"
export IPEDS_DB_PATH="$CI_DIR/ipeds.db"
"$PY" scripts/make_ci_fixture_db.py "$IPEDS_DB_PATH" >/dev/null

rm -f "$REPO_ROOT/.coverage"
for suite in eval/test_*.py; do
  # Each suite gets its own fresh app.db so migration/backup state can't leak.
  APP_DB_PATH="$(mktemp -d)/app.db" "$COV" run --source=app --append "$suite" >/dev/null
done

echo "=== app/ coverage (min ${COVERAGE_MIN}%) ==="
"$COV" report --sort=cover --fail-under="$COVERAGE_MIN"
