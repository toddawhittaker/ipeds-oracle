#!/usr/bin/env bash
# Create a git worktree that can run the app AND the full CI gate.
#
# Why: two sessions (e.g. two Claude Code terminals) in one clone share a single
# working tree, so a `git checkout` in one silently moves the other, and their dev
# servers fight over the same port. A worktree gives each session its own directory
# and branch backed by the same .git. The catch: a fresh worktree has none of the
# gitignored artifacts the app needs (.venv, node_modules, .env, the databases).
# This script wires them in — symlinking the big shared/read-only ones and copying
# the small stateful ones so each session's writes stay isolated.
#
# It refuses to leave behind any symlink that isn't gitignored: PR #48 clobbered
# real directories on `main` by committing a symlinked .venv/node_modules that
# slipped past a trailing-slash .gitignore pattern. Never `git add -A` in a
# worktree without checking what you're staging.
#
# Usage: scripts/worktree-add.sh <branch> [dir] [port]
#   <branch>  new (created off origin/main) or existing branch to check out
#   [dir]     worktree path         (default: ../ipeds-<branch-basename>)
#   [port]    dev-server port hint  (default: 8100 — MUST differ from other sessions)
set -euo pipefail

BRANCH="${1:?usage: scripts/worktree-add.sh <branch> [dir] [port]}"
# Always link from the PRIMARY checkout, even when run from another worktree
# (`--show-toplevel` would return the current worktree and chain the symlinks).
# `git worktree list` prints the main working tree first.
MAIN="$(git worktree list --porcelain | awk '/^worktree /{print $2; exit}')"
DIR="${2:-$(dirname "$MAIN")/ipeds-${BRANCH##*/}}"
PORT="${3:-8100}"

mkdir -p "$(dirname "$DIR")"
DIR="$(cd "$(dirname "$DIR")" && pwd)/$(basename "$DIR")"
[ -e "$DIR" ] && { echo "error: $DIR already exists" >&2; exit 1; }

# Create the worktree: reuse an existing branch, else branch off origin/main.
if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  git worktree add "$DIR" "$BRANCH"
else
  git fetch -q origin || true
  base="origin/main"; git rev-parse --verify -q "$base" >/dev/null || base="main"
  git worktree add "$DIR" -b "$BRANCH" "$base"
fi

# Symlink the big, shared, read-only-safe artifacts.
for link in .venv frontend/node_modules .env ipeds.db; do
  src="$MAIN/$link"
  [ -e "$src" ] || { echo "skip  $link (not in primary checkout)"; continue; }
  mkdir -p "$DIR/$(dirname "$link")"
  ln -sfn "$src" "$DIR/$link"
  if ! git -C "$DIR" check-ignore -q "$link"; then
    rm -f "$DIR/$link"
    echo "error: '$link' is NOT gitignored — removed the link to avoid a #48-style commit." >&2
    echo "       Fix .gitignore (no trailing slash) before re-running." >&2
    exit 1
  fi
  echo "link  $link"
done

# Copy the small, stateful DBs so each session's writes (migrations, sessions) are
# isolated. They're a point-in-time snapshot of the primary's state.
for db in app.db logs.db; do
  [ -e "$MAIN/$db" ] || continue
  cp "$MAIN/$db" "$DIR/$db"
  echo "copy  $db"
done

cat <<EOF

Worktree ready.
  dir:    $DIR
  branch: $BRANCH

Run its dev server on a DISTINCT port so it never fights another session:
  cd "$DIR" && COOKIE_SECURE=false .venv/bin/uvicorn --app-dir "$DIR/backend" \\
      app.main:app --host 0.0.0.0 --port $PORT

Full CI gate (uses the symlinked .venv / node_modules):
  cd "$DIR" && scripts/run_ci_local.sh

Remove when the branch is merged:
  git worktree remove "$DIR"
EOF
