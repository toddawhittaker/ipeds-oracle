#!/usr/bin/env bash
#
# run_backend_suites.sh — run every backend test suite, once, from one list.
#
# THE LIST IS A GLOB, DELIBERATELY. This used to be a hand-maintained array in
# scripts/run_ci_local.sh AND a hand-maintained set of ~30 named steps in
# .github/workflows/ci.yml, and the two drifted: test_grounding.py (the entire
# figure+table grounding contract) and test_version.py were in NEITHER. They ran
# only incidentally, inside coverage_check.sh's own glob with output sent to
# /dev/null — so a grounding regression surfaced as a bare non-zero exit from a
# step labelled "Coverage gate". Adding a suite is now just adding the file.
#
# Suites are dependency-light scripts: they print their own results and exit
# non-zero on failure. We stop at the first failure so the output ends on the
# thing that broke.
#
# Usage: scripts/run_backend_suites.sh          (expects the CI env already set)
# The caller owns IPEDS_DB_PATH / APP_DB_PATH and sourcing scripts/ci_env.sh.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY="${PY:-$REPO_ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="python3"

if [ -t 1 ]; then BOLD=$'\e[1m'; RED=$'\e[31m'; YEL=$'\e[33m'; RST=$'\e[0m'
else BOLD=""; RED=""; YEL=""; RST=""; fi

shopt -s nullglob
suites=(backend/tests/test_*.py)
shopt -u nullglob

if [ ${#suites[@]} -eq 0 ]; then
  # A glob that matches nothing would otherwise "pass" silently — the exact
  # failure mode this script exists to remove.
  printf '%s\n' "${BOLD}${RED}No backend suites found under backend/tests/ — refusing to report success.${RST}" >&2
  exit 1
fi

printf '%s\n' "${BOLD}${YEL}==> Backend: ${#suites[@]} suite(s)${RST}"
for suite in "${suites[@]}"; do
  printf '%s\n' "${BOLD}${YEL}--> $suite${RST}"
  if ! "$PY" "$suite"; then
    printf '%s\n' "${BOLD}${RED}BACKEND SUITE FAILED: $suite${RST}" >&2
    exit 1
  fi
done
printf '%s\n' "${BOLD}${YEL}==> Backend: all ${#suites[@]} suite(s) passed${RST}"
