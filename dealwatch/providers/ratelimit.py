"""Persisted daily call-budget for the eBay Browse API.

An in-memory counter resets on every restart. A crash-restart loop would
silently burn the entire day's 5,000-call allocation with nothing to show
for it - that failure mode is the whole reason this milestone exists (see
design.md §7 and CLAUDE.md's rate-limit trap). Persisting to SQLite means
the budget survives restarts, and doing the reservation as a single guarded
UPDATE makes it atomic: two callers racing at the ceiling can't both read
"there's room" and then both write.
"""

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from dealwatch.config import Settings
from dealwatch.storage.sqlite import connect, default_db_path

# eBay's daily reset is midnight Pacific, not midnight UTC. Using zoneinfo
# (not a hardcoded UTC offset) is what keeps this correct across the two
# DST transitions a year - a fixed offset would silently drift by an hour.
PACIFIC = ZoneInfo("America/Los_Angeles")


class BudgetExhausted(Exception):
    """Raised when reserve() is refused: no room left in today's budget."""


def _today_la() -> str:
    return datetime.now(PACIFIC).date().isoformat()


class DailyBudget:
    """Tracks (period, used) against a SQLite-backed daily ceiling.

    Opens a fresh connection per operation rather than holding one for the
    instance's lifetime. A single DailyBudget is lru_cache'd as a FastAPI
    dependency and shared across uvicorn's threadpool and the
    asyncio.to_thread call in EbayBrowseProvider._attempt() - a shared
    sqlite3.Connection used from multiple threads is either a
    ProgrammingError (check_same_thread's default) or a path to
    nested-transaction corruption (without it). The atomicity guarantee is
    supposed to live in the guarded SQL, not in a Python-level lock, so a
    short-lived per-call connection is the fix rather than a threading.Lock
    wrapped around a shared one.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ceiling = settings.daily_call_limit - settings.daily_reserve_calls
        self._ensure_row()

    def _connect(self) -> sqlite3.Connection:
        return connect(default_db_path(self._settings))

    def _ensure_row(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO budget (id, period, used) VALUES (1, ?, 0)",
                (_today_la(),),
            )
        finally:
            conn.close()

    def _rollover_if_needed(self, conn: sqlite3.Connection, today: str) -> None:
        # Lazy rollover: nothing runs at midnight to reset this. Instead,
        # every operation checks "is the stored period still today?" and
        # resets on the first one that notices it isn't. `today` is passed
        # in rather than recomputed here so a single reserve()/status() call
        # uses one clock reading throughout - reading it twice risked
        # straddling midnight and comparing a stale row against a new date.
        conn.execute(
            "UPDATE budget SET period = ?, used = 0 WHERE id = 1 AND period != ?",
            (today, today),
        )

    def reserve(self, n: int = 1) -> bool:
        today = _today_la()
        conn = self._connect()
        try:
            self._rollover_if_needed(conn, today)

            # BEGIN IMMEDIATE takes the write lock up front (rather than on
            # the first write inside the transaction), so a second
            # connection attempting this at the same instant blocks here
            # instead of racing past the WHERE check below. The guarded
            # UPDATE itself is atomic without this - SQLite serializes
            # writers regardless - but it's kept because it documents the
            # intent (reserve-or-refuse as one unit) even though it isn't
            # load-bearing here.
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "UPDATE budget SET used = used + ? "
                    "WHERE id = 1 AND period = ? AND used + ? <= ?",
                    (n, today, n, self._ceiling),
                )
                conn.execute("COMMIT")
                return cur.rowcount == 1
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def status(self) -> dict:
        today = _today_la()
        conn = self._connect()
        try:
            self._rollover_if_needed(conn, today)
            period, used = conn.execute(
                "SELECT period, used FROM budget WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()

        return {
            "period": period,
            "used": used,
            "ceiling": self._ceiling,
            "remaining": max(self._ceiling - used, 0),
        }
