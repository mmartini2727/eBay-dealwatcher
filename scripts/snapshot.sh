#!/usr/bin/env bash
# DealWatch DB snapshot. Runs on the LXC HOST (needs sqlite3; the container
# image doesn't have it). Safe while the collector is running.
# Snapshots live inside the LXC and are carried offsite by PBS.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="$REPO_ROOT/data/dealwatch.db"
SNAP_DIR="${DEALWATCH_SNAPSHOT_DIR:-/root/dealwatch-data}"
KEEP="${DEALWATCH_SNAPSHOT_KEEP:-7}"

TAG="${1:-}"                               # optional label, e.g. pre-v0.9
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$SNAP_DIR/dealwatch-${STAMP}${TAG:+-$TAG}.db"
TMP="$OUT.partial"

[ -f "$DB" ] || { echo "no database at $DB" >&2; exit 1; }
mkdir -p "$SNAP_DIR"
[ -e "$OUT" ] && { echo "refusing to overwrite $OUT" >&2; exit 1; }
rm -f "$TMP"

sqlite3 "$DB" "VACUUM INTO '$TMP';"

if [ "$(sqlite3 "$TMP" 'PRAGMA integrity_check;')" != "ok" ]; then
  echo "INTEGRITY CHECK FAILED — left at $TMP for inspection" >&2
  exit 1
fi

snap=$(sqlite3 "$TMP" 'SELECT COUNT(*) FROM observations;')
[ "$snap" -gt 0 ] || { echo "snapshot has zero observations" >&2; exit 1; }

# Atomic within the same filesystem: a PBS backup either sees no snapshot or
# a complete one, never a half-written file wearing a valid name.
mv "$TMP" "$OUT"

live=$(sqlite3 "$DB" 'SELECT COUNT(*) FROM observations;')
echo "ok  $OUT  $(du -h "$OUT" | cut -f1)  observations: snap=$snap live=$live"

ls -1t "$SNAP_DIR"/dealwatch-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --
