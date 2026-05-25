"""Pipeline conversion counter surfaced to helios-lidar.org.

Counts every successful pipeline run (one row per job that reaches
JobStatus.DONE) in a single SQLite file under the app's data
directory. The public endpoint just returns the all-time count so
the landing page can surface "X LiDAR conversions processed".

Everything in this module is best-effort: a failure to write the
DB is logged and swallowed so the job pipeline never blocks on a
stats hiccup. The DB itself sits under settings.jobs_dir's parent
so it persists across deploys but stays out of /tmp.

Schema is created on first access. Migrations are not needed yet:
the only state is the single table below.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


class StatsStore:
    """Thin wrapper around a single SQLite file. Owns its own
    connection per call (sqlite3 is per-connection thread-safe but
    not multi-thread on the same connection); a module-level lock
    serialises writes to avoid `database is locked` under burst
    traffic on the FastAPI worker.
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
                    CREATE TABLE IF NOT EXISTS conversions (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        completed_at INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_conversions_completed_at
                        ON conversions (completed_at);
                    CREATE TABLE IF NOT EXISTS download_snapshots (
                        ts          INTEGER PRIMARY KEY,
                        total_count INTEGER NOT NULL
                    );
                    """
                )

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

    def conversion_timestamps(self, since_unix: int) -> list[int]:
        """Every conversion completed_at >= since_unix, ascending. The
        dashboard buckets these into hourly / daily histograms."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    "SELECT completed_at FROM conversions WHERE completed_at >= ? ORDER BY completed_at",
                    (since_unix,),
                )
                return [int(r[0]) for r in cur.fetchall()]
        except sqlite3.Error as exc:
            log.warning("stats.conversion_timestamps failed: %s", exc)
            return []

    def record_download_snapshot(self, total_count: int) -> bool:
        """Append a (ts, total) snapshot of the cumulative download
        count fetched from the GitHub Releases API. Called once per
        successful refresh of helios_downloads.get_downloads_snapshot
        so we can compute per-period download deltas without GitHub
        exposing daily numbers directly.
        """
        if not isinstance(total_count, int) or total_count < 0:
            return False
        now = int(time.time())
        try:
            with self._lock:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO download_snapshots (ts, total_count) VALUES (?, ?)",
                        (now, total_count),
                    )
            return True
        except sqlite3.Error as exc:
            log.warning("stats.record_download_snapshot failed: %s", exc)
            return False

    def download_snapshots(self, since_unix: int) -> list[tuple[int, int]]:
        """Return ordered (ts, total_count) snapshots >= since_unix.
        Caller derives per-period download counts by diffing the
        first / last entry inside each bucket."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    "SELECT ts, total_count FROM download_snapshots WHERE ts >= ? ORDER BY ts",
                    (since_unix,),
                )
                return [(int(ts), int(c)) for ts, c in cur.fetchall()]
        except sqlite3.Error as exc:
            log.warning("stats.download_snapshots failed: %s", exc)
            return []

    def last_download_snapshot_before(self, before_unix: int) -> tuple[int, int] | None:
        """Snapshot immediately before `before_unix`, used as the
        zero-baseline when computing deltas inside a time window.
        Returns None when no snapshot exists prior."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    "SELECT ts, total_count FROM download_snapshots WHERE ts < ? ORDER BY ts DESC LIMIT 1",
                    (before_unix,),
                )
                row = cur.fetchone()
            return (int(row[0]), int(row[1])) if row else None
        except sqlite3.Error as exc:
            log.warning("stats.last_download_snapshot_before failed: %s", exc)
            return None
