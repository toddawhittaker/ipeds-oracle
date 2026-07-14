#!/usr/bin/env bash
# Back up app.db (users, skills, chats — the irreplaceable state) to Cloudflare
# R2. ipeds.db is intentionally NOT backed up: it is rebuildable from data/.
#
# Run from cron, e.g.:  0 3 * * *  /srv/ipeds/scripts/backup_app_db.sh
# Requires: sqlite3, rclone configured with an R2 remote named "r2".
set -euo pipefail

APP_DB="${APP_DB_PATH:-./srv-data/app.db}"
R2_REMOTE="${R2_REMOTE:-r2:ipeds-backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
TMP="$(mktemp -d)"
OUT="$TMP/app-$STAMP.db"

# Consistent hot backup (safe while the app is running).
sqlite3 "$APP_DB" ".backup '$OUT'"
gzip "$OUT"
rclone copy "$OUT.gz" "$R2_REMOTE/" --no-traverse
rm -rf "$TMP"
echo "backed up app.db -> $R2_REMOTE/app-$STAMP.db.gz"
