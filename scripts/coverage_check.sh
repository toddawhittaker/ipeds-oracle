#!/usr/bin/env bash
#
# coverage_check.sh — enforce the project's >=80% backend/app/ coverage standard,
# PER MODULE (not just the total): the build fails if ANY backend/app/ file drops below
# the threshold.
#
# Runs every backend/tests/test_*.py suite under coverage.py against a throwaway
# fixture ipeds.db (key-free, like CI). Auto-discovers suites by glob, so a new
# backend/tests/test_*.py is included automatically. eval_nl2sql.py is excluded
# (needs a real DB + API key).
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
# shellcheck source=scripts/ci_env.sh
source "$REPO_ROOT/scripts/ci_env.sh"

CI_DIR="$REPO_ROOT/.ci"
mkdir -p "$CI_DIR"
export IPEDS_DB_PATH="$CI_DIR/ipeds.db"
"$PY" scripts/make_ci_fixture_db.py "$IPEDS_DB_PATH" >/dev/null
# Ensure the SPA-serving block in backend/app/main.py is active (see the script's docstring).
"$PY" scripts/make_web_dist_stub.py

rm -f "$REPO_ROOT/.coverage"
for suite in backend/tests/test_*.py; do
  # Each suite gets its own fresh app.db so migration/backup state can't leak.
  APP_DB_PATH="$(mktemp -d)/app.db" "$COV" run --source=app --append "$suite" >/dev/null
done

echo "=== backend/app/ coverage (per-module floor ${COVERAGE_MIN}%) ==="
"$COV" report --sort=cover
"$COV" json -q -o "$CI_DIR/coverage.json"
# Fail if ANY module with statements is below the floor (report --fail-under only
# checks the grand total, which can hide one weak module behind strong ones).
COVERAGE_MIN="$COVERAGE_MIN" "$PY" - "$CI_DIR/coverage.json" <<'PY'
import json, os, sys
floor = float(os.environ["COVERAGE_MIN"])
data = json.load(open(sys.argv[1]))
bad = sorted(
    ((f, m["summary"]["percent_covered"]) for f, m in data["files"].items()
     if m["summary"]["num_statements"] > 0
     and m["summary"]["percent_covered"] < floor),
    key=lambda x: x[1])
if bad:
    print(f"\nFAIL: {len(bad)} module(s) below {floor:.0f}% coverage:")
    for f, pct in bad:
        print(f"  {pct:5.1f}%  {f}")
    sys.exit(1)
print(f"\nOK: every backend/app/ module >= {floor:.0f}% "
      f"(total {data['totals']['percent_covered']:.0f}%).")
PY
