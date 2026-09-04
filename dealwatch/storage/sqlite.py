"""SQLite connection helper, schema bootstrap, and the listing-history write
path (design.md §4).

WAL mode lets a future writer (the collector) and a future reader (the MCP
server) share the file without readers blocking on the writer's lock. The
schema is forward-only: each migration is a plain SQL script, applied once
and recorded in schema_version. No ORM, no Alembic.

This module does not normalize. record_sighting() takes already-mapped
values and never touches providers/ - see design.md §4.1's note that
raw_json lives on the observation precisely so re-normalization can happen
later without this module's involvement. store_spec() (V0.7b) is the one
exception to "never touches normalize/": it imports SpecResult purely as a
plain data carrier for its parameter type, and still doesn't call
normalize() or decide when to - that's entirely the caller's job (see
store_spec's own docstring).
"""

import json
import logging
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from dealwatch.config import Settings
from dealwatch.engine.baselines import Baseline
from dealwatch.normalize.engine import SpecResult

logger = logging.getLogger(__name__)

# design.md §4.2: "N consecutive sweep misses before gone_at." Setting
# gone_at on the first miss would manufacture short lifespans out of eBay's
# ordinary search-index inconsistency, and short lifespans are exactly what
# the survival baseline weighs most heavily.
MISS_THRESHOLD = 3

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
    (
        2,
        """
        -- Identity + current state. One row per item_id, updated in place.
        CREATE TABLE IF NOT EXISTS listings (
            item_id             TEXT PRIMARY KEY,
            profile_id          TEXT NOT NULL,
            title               TEXT NOT NULL,
            seller              TEXT,
            seller_feedback_pct REAL,
            seller_feedback_score INTEGER,
            condition_id        INTEGER,
            spec_json           TEXT,
            spec_status         TEXT NOT NULL,
            reject_rule_id      TEXT,
            bucket_key          TEXT,
            first_seen          INTEGER NOT NULL,
            last_seen           INTEGER NOT NULL,
            miss_count          INTEGER NOT NULL DEFAULT 0,
            gone_at             INTEGER,
            lifespan_mins       INTEGER
        );

        -- Append-only. One row on first sight, one per watched-field change.
        CREATE TABLE IF NOT EXISTS observations (
            id                INTEGER PRIMARY KEY,
            item_id           TEXT NOT NULL REFERENCES listings(item_id),
            observed_at       INTEGER NOT NULL,
            price_cents       INTEGER,
            shipping_cents    INTEGER,
            total_cents       INTEGER,
            buying_options    TEXT,
            current_bid_cents INTEGER,
            bid_count         INTEGER,
            raw_json          TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_observations_item_observed_at
            ON observations(item_id, observed_at);

        CREATE INDEX IF NOT EXISTS idx_listings_bucket_key_gone_at
            ON listings(bucket_key, gone_at);
        """,
    ),
    (
        3,
        """
        -- item_web_url exists today only inside raw_json - V0.9's alerts
        -- need it as a real column. SQLite has no ADD COLUMN IF NOT EXISTS,
        -- so this ALTER is only "idempotent" in the same sense migrations 1
        -- and 2 are: the schema_version gate above means this whole script
        -- runs exactly once per database file, ever, in normal operation.
        -- Re-running it against an already-migrated file (the scenario the
        -- CREATE TABLE IF NOT EXISTS statements below guard against) would
        -- fail on this one line - that's a real, deliberate gap, not an
        -- oversight, because guarding it would mean teaching the migration
        -- runner to check column existence in Python for one column that
        -- will only ever be added once.
        ALTER TABLE listings ADD COLUMN item_web_url TEXT;

        -- Survival-derived baselines (design.md §2.1, V0.8a). Fully
        -- recomputable from listings/observations - see
        -- scripts/recompute_baselines.py, which DELETEs and re-INSERTs
        -- every row for a profile rather than updating in place.
        CREATE TABLE IF NOT EXISTS baselines (
            profile_id        TEXT NOT NULL,
            bucket_key        TEXT NOT NULL,
            n                 INTEGER NOT NULL,
            n_price_only      INTEGER NOT NULL,
            p10_cents         INTEGER NOT NULL,
            p25_cents         INTEGER NOT NULL,
            p50_cents         INTEGER NOT NULL,
            fast_hours        INTEGER NOT NULL,
            computed_at       INTEGER NOT NULL,
            PRIMARY KEY (profile_id, bucket_key)
        );
        """,
    ),
]


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), check_same_thread=False)
    # Manual transaction control (BEGIN IMMEDIATE / COMMIT / ROLLBACK) is how
    # DailyBudget and record_sighting/record_sweep get an atomic
    # read-or-write unit; sqlite3's own implicit-transaction handling would
    # fight with that.
    conn.isolation_level = None
    # busy_timeout must be set before journal_mode=WAL, not after: switching
    # a fresh file into WAL mode itself needs a brief exclusive lock, and
    # with concurrent connections opening the same file that switch can
    # collide. Setting the timeout first makes it wait instead of raising
    # "database is locked" immediately.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    # Rows behave like dicts (row["title"]) as well as tuples, so read
    # helpers don't need to hand-map cursor.description everywhere.
    conn.row_factory = sqlite3.Row

    _apply_migrations(conn)
    return conn


def _split_statements(script: str) -> list[str]:
    # Every migration script is semicolon-terminated CREATE/ALTER statements
    # with only `--` line comments (no semicolons inside a comment or string
    # literal anywhere in _MIGRATIONS) - safe to split on literal `;`. A
    # comment line ends up prepended to the following fragment rather than
    # its own; that's fine, SQLite's parser skips comments in an execute()
    # string the same as inside a script.
    return [s.strip() for s in script.split(";") if s.strip()]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
    )

    # BEGIN IMMEDIATE around the whole read-current-version -> apply ->
    # write-new-version sequence, executing each migration statement with
    # plain execute() rather than executescript(), is load-bearing, not
    # decoration. DailyBudget (providers/ratelimit.py) opens a fresh
    # connection per call by design and is exercised from many threads at
    # once (tests/test_ratelimit.py's concurrent-reservation tests) - a
    # brand-new database file can genuinely get several concurrent
    # first-time connect() calls. executescript() unconditionally commits
    # any open transaction before it runs and then autocommits its own
    # statements one at a time, so wrapping executescript() calls in
    # BEGIN IMMEDIATE would silently do nothing - the transaction ends
    # before the script's DDL even starts, and two connections can both
    # read current=N and both attempt the same migration. CREATE TABLE IF
    # NOT EXISTS tolerated that (migrations 1 and 2, unchanged); ALTER
    # TABLE ADD COLUMN (migration 3) does not - SQLite has no ADD COLUMN
    # IF NOT EXISTS, so the loser got a real "duplicate column" error,
    # discovered via this exact concurrency test hanging (a thread dying
    # before it reaches the test's Barrier leaves the other 49 waiting
    # forever - not a timeout, an actual deadlock).
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        current = row[0] if row else 0

        for version, script in _MIGRATIONS:
            if version <= current:
                continue
            for statement in _split_statements(script):
                conn.execute(statement)
            if row is None:
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (version,)
                )
                row = (version,)
            else:
                conn.execute("UPDATE schema_version SET version = ?", (version,))
            current = version
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def default_db_path(settings: Settings) -> Path:
    return Path(settings.db_path)


# ---------------------------------------------------------------------------
# Write path (design.md §4.1, §4.2)
# ---------------------------------------------------------------------------


def record_sighting(
    conn: sqlite3.Connection,
    item_id: str,
    listing_fields: dict,
    observation_fields: dict,
    seen_at: int,
) -> None:
    """Record one item as seen, from a fast poll or a sweep.

    listing_fields keys: profile_id (str), title (str), seller (str|None),
    seller_feedback_pct (float|None), seller_feedback_score (int|None),
    condition_id (int|None), spec_status (str, optional - only consulted on
    insert; defaults to 'pending' if omitted, meaning "never normalized").

    observation_fields keys: price_cents, shipping_cents, total_cents,
    current_bid_cents, bid_count (all int|None), buying_options (list[str]),
    raw_json (str).

    seen_at is a Unix second, UTC. This function never writes last_seen -
    design.md §4.2 is explicit that only the sweep does, since a fast poll's
    absence proves nothing about a listing it didn't return.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT * FROM listings WHERE item_id = ?", (item_id,)
        ).fetchone()

        if existing is None:
            # 'pending' means "never normalized" - distinct from 'stale'
            # ("was normalized, title has since changed", design.md §4.1),
            # which is never true for a listing's very first sighting. A
            # caller that already knows better (e.g. a backfill) can pass
            # its own spec_status; this only supplies the default.
            # 'pending' means "never normalized" - distinct from 'stale'
            # ("was normalized, title has since changed", design.md §4.1),
            # which is never true for a listing's very first sighting. A
            # caller that already knows better (e.g. a backfill) can pass
            # its own spec_status; this only supplies the default.
            spec_status = listing_fields.get("spec_status", "pending")
            conn.execute(
                """
                INSERT INTO listings (
                    item_id, profile_id, title, seller, seller_feedback_pct,
                    seller_feedback_score, condition_id, spec_json,
                    spec_status, reject_rule_id, bucket_key,
                    first_seen, last_seen, miss_count, gone_at, lifespan_mins
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL,
                          ?, ?, 0, NULL, NULL)
                """,
                (
                    item_id,
                    listing_fields["profile_id"],
                    listing_fields["title"],
                    listing_fields.get("seller"),
                    listing_fields.get("seller_feedback_pct"),
                    listing_fields.get("seller_feedback_score"),
                    listing_fields.get("condition_id"),
                    spec_status,
                    seen_at,
                    seen_at,
                ),
            )
            _insert_observation(conn, item_id, observation_fields, seen_at)
            conn.execute("COMMIT")
            return

        # NULL != NULL in SQL, so this comparison has to happen in Python -
        # a WHERE-clause comparison would treat every unknown-shipping
        # listing as "changed" on every single poll.
        title_changed = listing_fields["title"] != existing["title"]

        last_obs = conn.execute(
            "SELECT * FROM observations WHERE item_id = ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        observation_changed = (
            title_changed
            or observation_fields.get("price_cents") != last_obs["price_cents"]
            or observation_fields.get("shipping_cents") != last_obs["shipping_cents"]
            or (observation_fields.get("buying_options") or [])
            != _load_buying_options(last_obs["buying_options"])
        )

        if observation_changed:
            _insert_observation(conn, item_id, observation_fields, seen_at)

        if title_changed:
            spec_json, bucket_key, spec_status = None, None, "stale"
        else:
            # Unchanged - preserve whatever V0.7 last computed here rather
            # than spuriously re-marking a settled spec as stale.
            spec_json = existing["spec_json"]
            bucket_key = existing["bucket_key"]
            spec_status = existing["spec_status"]

        if existing["gone_at"] is not None:
            logger.warning(
                "item %s resurrected after being marked gone; discarding "
                "lifespan_mins=%s",
                item_id,
                existing["lifespan_mins"],
            )

        conn.execute(
            """
            UPDATE listings
            SET title = ?, spec_json = ?, bucket_key = ?, spec_status = ?,
                miss_count = 0, gone_at = NULL, lifespan_mins = NULL
            WHERE item_id = ?
            """,
            (listing_fields["title"], spec_json, bucket_key, spec_status, item_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def store_spec(conn: sqlite3.Connection, item_id: str, result: SpecResult) -> None:
    """Write one listing's normalization result: spec_json, bucket_key,
    spec_status, reject_rule_id.

    This is the one function both the collector (inline, on every sighting)
    and scripts/backfill_normalize.py (over history) call to persist a
    SpecResult - one implementation so the two can't drift apart from each
    other by being edited at different times (design.md §5, V0.7b). Neither
    WHEN to normalize nor WHAT to normalize from is this function's
    decision: the collector already has mapped listing fields in hand: the
    backfill reads and maps raw_json itself. This only writes what it's
    handed.

    spec_json is `result.spec` verbatim, including the empty dict a
    rejected or not_target result carries - that's a real recorded fact
    ("normalize ran and stopped here"), not the same thing as 'pending'
    ("normalize has never run").

    One UPDATE, no explicit BEGIN/COMMIT - a single statement is already
    atomic under SQLite's own autocommit (see connect()'s isolation_level
    comment), and a caller doing this right after record_sighting (which
    does manage its own transaction) doesn't get a surprise nested one.
    """
    conn.execute(
        """
        UPDATE listings
        SET spec_json = ?, bucket_key = ?, spec_status = ?, reject_rule_id = ?
        WHERE item_id = ?
        """,
        (
            json.dumps(result.spec),
            result.bucket_key,
            result.spec_status,
            result.reject_rule_id,
            item_id,
        ),
    )


def store_baselines(
    conn: sqlite3.Connection,
    profile_id: str,
    baselines: list[Baseline],
    computed_at: int,
) -> None:
    """Replace every baselines row for profile_id in one transaction.

    Baselines are fully recomputable from listings/observations history
    (design.md §2.1, V0.8a) - there is no incremental-update case to
    support, so this always DELETEs the profile's existing rows before
    INSERTing the freshly computed set, the same "derived data, not
    observed data" treatment CLAUDE.md's Conventions section already
    applies elsewhere. Called by scripts/recompute_baselines.py; nothing
    else writes this table.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM baselines WHERE profile_id = ?", (profile_id,))
        conn.executemany(
            """
            INSERT INTO baselines (
                profile_id, bucket_key, n, n_price_only,
                p10_cents, p25_cents, p50_cents, fast_hours, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    profile_id,
                    b.bucket_key,
                    b.n,
                    b.n_price_only,
                    b.p10_cents,
                    b.p25_cents,
                    b.p50_cents,
                    b.fast_hours,
                    computed_at,
                )
                for b in baselines
            ],
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def record_sweep(
    conn: sqlite3.Connection,
    seen_item_ids: Iterable[str],
    profile_id: str,
    swept_at: int,
) -> None:
    """Bulk bookkeeping for one completed sweep of profile_id.

    seen_item_ids is every item_id the sweep's search results returned -
    record_sighting() should already have been called for each of them
    before this runs. swept_at is a Unix second, UTC.

    All of it happens in one transaction: 150 separate UPDATEs is 150
    fsyncs for what is conceptually a single event.
    """
    seen = list(seen_item_ids)

    conn.execute("BEGIN IMMEDIATE")
    try:
        if seen:
            placeholders = ",".join("?" * len(seen))
            conn.execute(
                f"UPDATE listings SET last_seen = ?, miss_count = 0 "
                f"WHERE item_id IN ({placeholders})",
                (swept_at, *seen),
            )
            conn.execute(
                f"""
                UPDATE listings
                SET miss_count = miss_count + 1
                WHERE profile_id = ? AND gone_at IS NULL
                  AND item_id NOT IN ({placeholders})
                """,
                (profile_id, *seen),
            )
        else:
            # Nothing came back at all - every active listing for this
            # profile was missed.
            conn.execute(
                "UPDATE listings SET miss_count = miss_count + 1 "
                "WHERE profile_id = ? AND gone_at IS NULL",
                (profile_id,),
            )

        conn.execute(
            """
            UPDATE listings
            SET gone_at = last_seen,
                lifespan_mins = (last_seen - first_seen) / 60
            WHERE profile_id = ? AND gone_at IS NULL AND miss_count >= ?
            """,
            (profile_id, MISS_THRESHOLD),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _insert_observation(
    conn: sqlite3.Connection, item_id: str, observation_fields: dict, observed_at: int
) -> None:
    conn.execute(
        """
        INSERT INTO observations (
            item_id, observed_at, price_cents, shipping_cents, total_cents,
            buying_options, current_bid_cents, bid_count, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            observed_at,
            observation_fields.get("price_cents"),
            observation_fields.get("shipping_cents"),
            observation_fields.get("total_cents"),
            json.dumps(observation_fields.get("buying_options") or []),
            observation_fields.get("current_bid_cents"),
            observation_fields.get("bid_count"),
            observation_fields["raw_json"],
        ),
    )


def _load_buying_options(value: str | None) -> list[str]:
    return json.loads(value) if value is not None else []


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def get_latest_observation(conn: sqlite3.Connection, item_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM observations WHERE item_id = ? ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    return _observation_row_to_dict(row) if row is not None else None


def get_observations(conn: sqlite3.Connection, item_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM observations WHERE item_id = ? ORDER BY id ASC",
        (item_id,),
    ).fetchall()
    return [_observation_row_to_dict(row) for row in rows]


def count_active_listings(conn: sqlite3.Connection, profile_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM listings WHERE profile_id = ? AND gone_at IS NULL",
        (profile_id,),
    ).fetchone()
    return row[0]


def _observation_row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["buying_options"] = _load_buying_options(data["buying_options"])
    return data
