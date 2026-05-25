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
                    CREATE TABLE IF NOT EXISTS download_per_tag_snapshots (
                        ts          INTEGER NOT NULL,
                        tag         TEXT    NOT NULL,
                        total_count INTEGER NOT NULL,
                        PRIMARY KEY (ts, tag)
                    );
                    CREATE INDEX IF NOT EXISTS idx_dl_per_tag_ts
                        ON download_per_tag_snapshots (ts);
                    """
                )
                #Idempotent column add: client_ip on conversions
                #for the per-country breakdown. Older rows stay NULL
                #until the nginx-log backfill assigns one.
                try:
                    conn.execute("ALTER TABLE conversions ADD COLUMN client_ip TEXT")
                except sqlite3.OperationalError:
                    pass  #column already exists

    def record_conversion(self, client_ip: str | None = None) -> bool:
        """Append one row to the conversions table. Called from
        app.main._process() right after a job hits DONE. `client_ip`
        is the address that POSTed the job, captured upstream from
        the FastAPI Request, so we can resolve the country later
        for the dashboard.
        """
        now = int(time.time())
        try:
            with self._lock:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        "INSERT INTO conversions (completed_at, client_ip) VALUES (?, ?)",
                        (now, client_ip),
                    )
            return True
        except sqlite3.Error as exc:
            log.warning("stats.record_conversion failed: %s", exc)
            return False

    def conversion_rows_since(self, since_unix: int) -> list[tuple[int, str | None]]:
        """Return `(completed_at, client_ip)` for every conversion at
        or after `since_unix`, ascending. The dashboard uses this to
        resolve country breakdowns; rows with NULL ip stay grouped
        under "Unknown" until the backfill catches them."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    "SELECT completed_at, client_ip FROM conversions "
                    "WHERE completed_at >= ? ORDER BY completed_at",
                    (since_unix,),
                )
                return [(int(ts), ip if ip else None) for ts, ip in cur.fetchall()]
        except sqlite3.Error as exc:
            log.warning("stats.conversion_rows_since failed: %s", exc)
            return []

    def conversions_missing_ip(self) -> list[tuple[int, int]]:
        """`(id, completed_at)` for every conversion row whose
        client_ip is NULL, ascending, used by the nginx-log backfill
        to pair completion timestamps with the request IPs."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    "SELECT id, completed_at FROM conversions "
                    "WHERE client_ip IS NULL ORDER BY completed_at"
                )
                return [(int(i), int(t)) for i, t in cur.fetchall()]
        except sqlite3.Error as exc:
            log.warning("stats.conversions_missing_ip failed: %s", exc)
            return []

    def set_conversion_ip(self, conversion_id: int, client_ip: str) -> bool:
        try:
            with self._lock:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        "UPDATE conversions SET client_ip = ? WHERE id = ? AND client_ip IS NULL",
                        (client_ip, conversion_id),
                    )
            return True
        except sqlite3.Error as exc:
            log.warning("stats.set_conversion_ip failed: %s", exc)
            return False

    def backfill_conversion_ips(self, post_entries: list[tuple[int, str]],
                                lookback_seconds: int = 20 * 60) -> int:
        """Walk every conversion with NULL `client_ip` and assign the
        most recent POST /jobs IP from `post_entries` that landed at
        most `lookback_seconds` before the conversion's completed_at.

        `post_entries` is a chronologically-ordered list of
        `(unix_ts, ip)` tuples (see visitor_stats.parse_post_jobs_entries).
        Returns the number of rows updated.

        Heuristic: the upload POST is always strictly before the
        completion time (the job runs synchronously then writes the
        DONE row). Typical end-to-end is well under 5 min for the
        rasters / under 15 min for a heavy LAZ; 20 min is the
        permissive cap so we don't miss anything reasonable. If two
        uploads land inside the same window, we take the latest one,
        which is the most likely match in practice for a single
        user. Multi-user collisions inside a 20 min window are rare
        on this site's traffic.
        """
        missing = self.conversions_missing_ip()
        if not missing or not post_entries:
            return 0
        #Two-pointer sweep, both sorted ascending by ts.
        updates: list[tuple[int, str]] = []
        i = 0
        for cid, ts in missing:
            window_start = ts - lookback_seconds
            #Advance i to the first entry that's >= window_start.
            while i < len(post_entries) and post_entries[i][0] < window_start:
                i += 1
            #Look for the latest entry <= ts (but >= window_start).
            j = i
            best: str | None = None
            while j < len(post_entries) and post_entries[j][0] <= ts:
                best = post_entries[j][1]
                j += 1
            if best:
                updates.append((cid, best))
        if not updates:
            return 0
        try:
            with self._lock:
                with sqlite3.connect(self.db_path) as conn:
                    conn.executemany(
                        "UPDATE conversions SET client_ip = ? "
                        "WHERE id = ? AND client_ip IS NULL",
                        [(ip, cid) for cid, ip in updates],
                    )
            return len(updates)
        except sqlite3.Error as exc:
            log.warning("stats.backfill_conversion_ips failed: %s", exc)
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

    def record_download_per_tag(self, per_tag: list[tuple[str, int]]) -> bool:
        """Append one row per (release_tag, cumulative_count) at the
        current wall-clock time. Used to derive per-version download
        deltas inside each bucket of the dashboard histograms."""
        if not per_tag:
            return False
        now = int(time.time())
        try:
            rows = []
            for tag, c in per_tag:
                if not isinstance(c, int) or c < 0 or not isinstance(tag, str) or not tag:
                    continue
                rows.append((now, tag, c))
            if not rows:
                return False
            with self._lock:
                with sqlite3.connect(self.db_path) as conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO download_per_tag_snapshots (ts, tag, total_count) "
                        "VALUES (?, ?, ?)",
                        rows,
                    )
            return True
        except sqlite3.Error as exc:
            log.warning("stats.record_download_per_tag failed: %s", exc)
            return False

    def download_per_tag_snapshots_since(self, since_unix: int) -> list[tuple[int, str, int]]:
        """Ordered (ts, tag, count) rows >= since_unix."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    "SELECT ts, tag, total_count FROM download_per_tag_snapshots "
                    "WHERE ts >= ? ORDER BY ts, tag",
                    (since_unix,),
                )
                return [(int(ts), str(tag), int(c)) for ts, tag, c in cur.fetchall()]
        except sqlite3.Error as exc:
            log.warning("stats.download_per_tag_snapshots_since failed: %s", exc)
            return []

    def last_download_per_tag_before(self, before_unix: int) -> dict[str, int]:
        """For each tag, the highest snapshot total_count strictly
        before `before_unix`. Used as the per-tag baseline so the
        first bucket inside a window doesn't double-count downloads
        that landed before the window started."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    "SELECT tag, MAX(total_count) FROM download_per_tag_snapshots "
                    "WHERE ts < ? GROUP BY tag",
                    (before_unix,),
                )
                return {str(tag): int(c) for tag, c in cur.fetchall() if tag is not None}
        except sqlite3.Error as exc:
            log.warning("stats.last_download_per_tag_before failed: %s", exc)
            return {}
