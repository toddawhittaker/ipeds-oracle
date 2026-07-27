#!/usr/bin/env bash
#
# docs-shots.sh — regenerate every screenshot in docs/images/ for the guides.
#
# One command, because the last set went stale: the guides shipped with #114 on
# 2026-07-20 and were still showing that UI after the top bar was redesigned,
# the grounding marks landed, and the attention badges landed. Stale screenshots
# are invisible to every test and are the first thing a new user sees.
#
# Run it whenever a change is VISIBLE — layout, navigation, a new mark or badge
# — then eyeball the `git diff --stat` on docs/images/ and look at the ones that
# moved. Shots come from the e2e mock harness, so no backend, no LLM key, and no
# real user's email address ends up in a published PNG.
#
# Usage: scripts/docs-shots.sh
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
PAIRS="$ROOT/.docs-shots"

command -v convert >/dev/null 2>&1 || {
  echo "docs-shots: ImageMagick 'convert' not found (apt-get install imagemagick)" >&2
  exit 1
}

rm -rf "$PAIRS"
mkdir -p "$PAIRS" "$ROOT/docs/images"

echo "==> capturing"
( cd frontend && npx playwright test --config=playwright.docs.config.js )

# Stitch the light/dark pairs into one image each. `+append` puts them
# side-by-side; the 24px gutter keeps the two frames visually separate on
# GitHub's white AND dark page backgrounds.
echo "==> stitching light/dark pairs"
for name in answer admin-usage; do
  convert "$PAIRS/${name}-light.png" "$PAIRS/${name}-dark.png" \
    -background none -splice 24x0+0+0 +append -chop 24x0+0+0 \
    "$ROOT/docs/images/${name}-themes.png"
  echo "    docs/images/${name}-themes.png"
done

rm -rf "$PAIRS"

echo
echo "==> done. Review what actually changed:"
echo "    git status --short docs/images/"
