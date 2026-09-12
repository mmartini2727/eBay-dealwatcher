#!/usr/bin/env python3
"""Print dealwatch.reporting.status.collect_status()'s payload (V0.10,
design.md §12).

    python scripts/status.py [--db data/dealwatch.db] [--profile thinkpad-t14] [--json]

Read-only, via the same file:...?mode=ro convention as
scripts/baseline_report.py, scripts/normalize_report.py, and
scripts/bucket_key_dryrun.py - a fourth copy of open_readonly(), not an
import, since it is copy-pasted in each of those three already and this
milestone must not touch any of them to introduce a shared one.

WAL caveat: a mode=ro connection cannot create the -shm/-wal sidecar
files. Inside the running container a writer (the collector) already has
them open, so this works. Against a stopped container, or a snapshot
copied without its sidecar files, the open fails outright rather than
silently reading a stale view - see README's Database snapshots section
on why a VACUUM INTO snapshot has no sidecars to begin with.
"""

import argparse
import sqlite3
import time

from dealwatch.config import Settings
from dealwatch.reporting.status import collect_status


def open_readonly(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


_ALIVE_LINES = [
    ("last_sweep_started_age_mins", "last sweep started (mins ago)"),
    ("last_sweep_attempt_age_mins", "last sweep attempt (mins ago)"),
    ("last_price_change_age_mins", "last price change (mins ago)"),
    ("sweeps_today_total", "sweeps today: total"),
    ("sweeps_today_recorded", "sweeps today: recorded"),
    ("sweeps_today_truncated", "sweeps today: truncated"),
    ("listings_last_seen_max", "listings.last_seen (max, epoch)"),
    ("sweep_bookkeeping_consistent", "sweep bookkeeping consistent"),
]

_COLLECTING_LINES = [
    ("active_listings", "active listings"),
    ("new_listings_today", "new listings today"),
    ("deaths_today", "deaths today"),
    ("deaths_today_unmeasured", "deaths today (unmeasured lifespan)"),
    ("last_sweep_coverage_pct", "last sweep coverage"),
    ("last_sweep_page_drift", "last sweep page drift"),
    ("last_sweep_active_count_before", "last sweep active_count_before"),
]

_FINDING_LINES = [
    ("alert_events_today_live", "alert events today (live)"),
    ("alert_events_today_dry", "alert events today (dry_run)"),
    ("alert_rows_today", "alert rows today"),
    ("alert_events_7d", "alert events (7d)"),
    ("distinct_items_alerted_7d", "distinct items alerted (7d)"),
    ("best_ratio_24h", "best ratio_to_p25 (24h)"),
]

_BASELINE_LINES = [
    ("baseline_buckets_total", "baseline buckets"),
    ("baselines_computed_age_mins", "baselines computed (mins ago)"),
    ("dead_spec_ok_count", "dead spec_status=ok count"),
    ("dead_spec_ok_deaths_7d", "dead spec_status=ok deaths (7d)"),
]

_LABEL_WIDTH = max(
    len(label)
    for _, label in _ALIVE_LINES + _COLLECTING_LINES + _FINDING_LINES + _BASELINE_LINES
)


def _fmt(value) -> str:
    return "unknown" if value is None else str(value)


def _render_lines(group: dict, lines: list[tuple[str, str]]) -> list[str]:
    return [f"  {label:<{_LABEL_WIDTH}}  {_fmt(group[key])}" for key, label in lines]


def render_status(status: dict) -> str:
    """Pure formatting - grouped plain text, one metric per line, aligned.
    No color, no box drawing, no external dependency. Kept separate from
    main() so it's directly testable without a database."""
    lines: list[str] = []

    lines.append("ALIVE")
    lines.extend(_render_lines(status["alive"], _ALIVE_LINES))
    budget = status["alive"]["budget"]
    lines.append(
        f"  {'budget':<{_LABEL_WIDTH}}  period={_fmt(budget['period'])} "
        f"used={_fmt(budget['used'])} period_is_today={_fmt(budget['period_is_today'])} "
        f"ceiling={_fmt(budget['ceiling'])} remaining={_fmt(budget['remaining'])}"
    )

    lines.append("")
    lines.append("COLLECTING")
    lines.extend(_render_lines(status["collecting"], _COLLECTING_LINES))
    spec_counts = status["collecting"]["spec_status_counts_active"]
    lines.append(
        f"  {'spec_status (active)':<{_LABEL_WIDTH}}  "
        + ", ".join(f"{k}={v}" for k, v in spec_counts.items())
    )

    lines.append("")
    lines.append("FINDING")
    lines.extend(_render_lines(status["finding"], _FINDING_LINES))
    delivery_counts = status["finding"]["delivery_status_row_counts_today"]
    lines.append(
        f"  {'delivery_status rows (today)':<{_LABEL_WIDTH}}  "
        + (", ".join(f"{k}={v}" for k, v in delivery_counts.items()) or "none")
    )

    lines.append("")
    lines.append("BASELINE")
    lines.extend(_render_lines(status["baseline"], _BASELINE_LINES))
    layer_counts = status["baseline"]["baseline_layer_counts_7d"]
    lines.append(
        f"  {'baseline_layer events (7d)':<{_LABEL_WIDTH}}  "
        + (", ".join(f"{k}={v}" for k, v in layer_counts.items()) or "none")
    )

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/dealwatch.db")
    parser.add_argument("--profile", default="thinkpad-t14")
    parser.add_argument("--json", action="store_true", help="emit the raw dict")
    args = parser.parse_args(argv)

    settings = Settings()
    ceiling = settings.daily_call_limit - settings.daily_reserve_calls

    conn = open_readonly(args.db)
    try:
        status = collect_status(conn, args.profile, ceiling=ceiling, now=int(time.time()))
    finally:
        conn.close()

    if args.json:
        import json

        print(json.dumps(status, indent=2))
    else:
        print(render_status(status))


if __name__ == "__main__":
    main()
