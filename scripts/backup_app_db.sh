#!/usr/bin/env bash
# Back up app.db (users, skills, chats — the irreplaceable state) off-site.
# ipeds.db is intentionally NOT backed up: it is rebuildable from data/.
#
# Run from cron, e.g.:  0 3 * * *  /path/to/ipeds/scripts/backup_app_db.sh
# Requires: sqlite3, and — for the off-site copy — rclone with a remote
# configured (any S3-compatible object store works). Set BACKUP_REMOTE to the
# rclone "remote:path"; leave it unset to keep the local .gz only.
set -euo pipefail

APP_DB="${APP_DB_PATH:-./srv-data/app.db}"
BACKUP_REMOTE="${BACKUP_REMOTE:-}"
if [ -z "$BACKUP_REMOTE" ]; then
  echo "Set BACKUP_REMOTE to an rclone remote:path for the off-site copy." >&2
  echo "For local-only backups (with retention) use scripts/backup_app_db.py." >&2
  exit 1
fi
STAMP="$(date +%Y%m%d-%H%M%S)"
TMP="$(mktemp -d)"
OUT="$TMP/app-$STAMP.db"

# Consistent hot backup (safe while the app is running), then push off-site.
sqlite3 "$APP_DB" ".backup '$OUT'"
gzip "$OUT"
rclone copy "$OUT.gz" "$BACKUP_REMOTE/" --no-traverse
rm -rf "$TMP"
echo "backed up app.db -> $BACKUP_REMOTE/app-$STAMP.db.gz"
