#!/usr/bin/env bash
# DealWatch baseline recompute. Runs on the LXC HOST, after the nightly
# snapshot, so a bad recompute is always recoverable from a file minutes old.
# The recompute runs in the container (the package lives there); the host
# only needs sqlite3, which snapshot.sh already requires.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="$REPO_ROOT/data/dealwatch.db"
KUMA_URL="${DEALWATCH_KUMA_PUSH_URL:-}"

[ -f "$DB" ] || { echo "no database at $DB" >&2; exit 1; }

shopt -s nullglob
paths=("$REPO_ROOT"/profiles/*.yaml)
[ ${#paths[@]} -gt 0 ] || { echo "no profiles in $REPO_ROOT/profiles" >&2; exit 1; }

rc=0
for path in "${paths[@]}"; do
  rel="profiles/$(basename "$path")"
  id="$(awk '/^id:[[:space:]]/ {print $2; exit}' "$path")"
  [ -n "$id" ] || { echo "FAIL $rel: no top-level id:" >&2; rc=1; continue; }

  enabled="$(awk '/^enabled:[[:space:]]/ {print $2; exit}' "$path")"
  if [ "$enabled" != "true" ]; then
    echo "skip $id (enabled: ${enabled:-unset})"
    continue
  fi

  before=$(sqlite3 -readonly "$DB" \
    "SELECT COUNT(*) FROM baselines WHERE profile_id = '$id';")

  if ! out=$(docker exec dealwatch python scripts/recompute_baselines.py \
               --profile "$rel" 2>&1); then
    echo "FAIL $id: recompute exited non-zero" >&2
    echo "$out" >&2
    rc=1
    continue
  fi

  after=$(sqlite3 -readonly "$DB" \
    "SELECT COUNT(*) FROM baselines WHERE profile_id = '$id';")

  # DELETE-then-INSERT: a run deriving zero buckets wipes the profile's
  # baselines outright. Scoring goes blind and baselines_age stays quiet,
  # because there are no rows left to be stale.
  if [ "$before" -gt 0 ] && [ "$after" -eq 0 ]; then
    echo "FAIL $id: had $before baseline rows, now 0 — scoring is blind" >&2
    echo "$out" >&2
    rc=1
    continue
  fi

  echo "ok  $id  baselines $before -> $after  |  $(echo "$out" | tail -n1)"
done

if [ "$rc" -eq 0 ] && [ -n "$KUMA_URL" ]; then
  curl -fsS -m 10 "$KUMA_URL" >/dev/null || echo "kuma push failed" >&2
fi

exit "$rc"