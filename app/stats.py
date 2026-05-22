"""Public anonymous-stats counters surfaced to helios-lidar.org.

Two counters, both stored in a single SQLite file under the app's
data directory:

* `installs`     , one row per UUIDv4 install_id the Helios card
                  POSTs at most once per browser per 24 h. The
                  card endpoint validates the UUIDv4 shape before
                  insertion so an attacker can't poison the count
                  with arbitrary strings. We don't log the IP, we
                  don't log the User-Agent: only the install_id
                  and a last-seen unix timestamp. Active counts
                  use a 30-day rolling window.
* `conversions`  , monotonically incremented every time
                  app.main._process() flips a job to DONE.
                  Stored as one row per successful conversion so a
                  future "X conversions this week" stat is a
                  one-liner; the public endpoint just returns the
                  all-time count.

Everything in this module is best-effort: a failure to write the
DB is logged and swallowed so the heartbeat / job pipeline never
blocks on a stats hiccup. The DB itself sits under settings.jobs_dir's
parent so it persists across deploys but stays out of /tmp.

Schema is created on first access. Migrations are not needed yet:
the only state is the two tables below.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


#UUIDv4 byte regex: 8-4-4-4-12 hex, with the version nibble = 4 and
#the variant nibble in [8, 9, a, b]. Identical to the regex the card
#applies before sending. We re-validate at the server boundary so a
#malformed payload never reaches the DB.
_UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


#Rolling window for the "active users" count. Matches the card's
#own throttle (24 h) and gives a comfortable buffer for users who
#open the dashboard infrequently. Stale install_ids beyond this
#window are NOT auto-deleted (a future ping reactivates the row),
#but the public count only counts the recent ones.
ACTIVE_WINDOW_DAYS = 30


class StatsStore:
    """Thin wrapper around a single SQLite file. Owns its own
    connection per call (sqlite3 is per-connection thread-safe but
    not multi-thread on the same connection); a module-level lock
    serialises writes to avoid `database is locked` under burst
    heartbeat traffic on the FastAPI worker.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._lock:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS installs (
                        install_id TEXT PRIMARY KEY,
                        last_seen  INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_installs_last_seen
                        ON installs (last_seen);
                    CREATE TABLE IF NOT EXISTS conversions (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        completed_at INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_conversions_completed_at
                        ON conversions (completed_at);
                    """
                )

    def record_install(self, install_id: str) -> bool:
        """Upsert one install row. Returns False (silently) for any
        payload that doesn't match the UUIDv4 shape, so the public
        endpoint can call this directly without re-validating.
        """
        if not _UUID_V4_RE.match(install_id):
            return False
        now = int(time.time())
        try:
            with self._lock:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        """
                        INSERT INTO installs (install_id, last_seen)
                        VALUES (?, ?)
                        ON CONFLICT(install_id) DO UPDATE
                            SET last_seen = excluded.last_seen
                        """,
                        (install_id.lower(), now),
                    )
            return True
        except sqlite3.Error as exc:
            log.warning("stats.record_install failed: %s", exc)
            return False

    def record_conversion(self) -> bool:
        """Append one row to the conversions table. Called from
        app.main._process() right after a job hits DONE.
        """
        now = int(time.time())
        try:
            with self._lock:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        "INSERT INTO conversions (completed_at) VALUES (?)",
                        (now,),
                    )
            return True
        except sqlite3.Error as exc:
            log.warning("stats.record_conversion failed: %s", exc)
            return False

    def active_installs(self, window_days: int = ACTIVE_WINDOW_DAYS) -> int:
        """Number of distinct install_ids seen in the last window."""
        cutoff = int(time.time()) - window_days * 24 * 60 * 60
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM installs WHERE last_seen >= ?",
                    (cutoff,),
                ).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error as exc:
            log.warning("stats.active_installs failed: %s", exc)
            return 0

    def total_conversions(self) -> int:
        """All-time successful conversion count."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM conversions"
                ).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error as exc:
            log.warning("stats.total_conversions failed: %s", exc)
            return 0
