"""SQLite connection helper and schema bootstrap.

WAL mode lets a future writer (the collector) and a future reader (the MCP
server) share the file without readers blocking on the writer's lock. The
schema is forward-only: each migration is a plain SQL script, applied once
and recorded in schema_version. No ORM, no Alembic - at this size a
migration framework would be pure overhead, and design.md is explicit that
this file starts as connection-layer-plus-budget-table only. Listings,
baselines, and alerts tables are V0.5.
"""

import sqlite3
from pathlib import Path

from dealwatch.config import Settings

# Each entry is (version, script). Scripts must be idempotent (CREATE TABLE
# IF NOT EXISTS) so re-running migrations against an already-migrated file
# is harmless - that's what makes "forward-only, no rollback" safe.
_MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS budget (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            period TEXT NOT NULL,
            used INTEGER NOT NULL
        );
        """,
    ),
]


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), check_same_thread=False)
    # Manual transaction control (BEGIN IMMEDIATE / COMMIT / ROLLBACK) is how
    # DailyBudget gets an atomic reserve-or-refuse check; sqlite3's own
    # implicit-transaction handling would fight with that.
    conn.isolation_level = None
    # busy_timeout must be set before journal_mode=WAL, not after: switching
    # a fresh file into WAL mode itself needs a brief exclusive lock, and
    # with concurrent connections opening the same file that switch can
    # collide. Setting the timeout first makes it wait instead of raising
    # "database is locked" immediately.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")

    _apply_migrations(conn)
    return conn


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
    )
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    current = row[0] if row else 0

    for version, script in _MIGRATIONS:
        if version <= current:
            continue
        conn.executescript(script)
        if row is None:
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (version,)
            )
            row = (version,)
        else:
            conn.execute("UPDATE schema_version SET version = ?", (version,))
        current = version


def default_db_path(settings: Settings) -> Path:
    return Path(settings.db_path)
