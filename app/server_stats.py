"""Lightweight server-load sampler.

A background daemon thread records (timestamp, 1-min load average,
memory-used %, disk-used %) every 60 seconds into a server_samples
SQLite table. Zero deps: load via os.getloadavg(), memory via
/proc/meminfo, disk via os.statvfs("/").

CPU is exposed as the 1-minute load average (number of processes
in the run queue averaged over 60 s) rather than instantaneous %,
because % requires sampling /proc/stat twice and tracking deltas
per-cpu, which is heavier than what this dashboard needs. On a
1-core or 2-core VPS the load average is the actually meaningful
"how busy is the box" signal anyway.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_INTERVAL_SECONDS = 60


def _read_meminfo_pct() -> float | None:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            total = avail = None
            for line in fh:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1])
                if total is not None and avail is not None:
                    break
            if not total:
                return None
            used = max(0, total - (avail or 0))
            return round(100.0 * used / total, 2)
    except OSError:
        return None


def _disk_used_pct(path: str = "/") -> float | None:
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free  = st.f_bavail * st.f_frsize
        if total <= 0:
            return None
        return round(100.0 * (total - free) / total, 2)
    except OSError:
        return None


def _load_1m() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        return None


class ServerSampler:
    """Owns its own SQLite connection per write + a tiny daemon
    thread that wakes up every minute to record one sample. Safe
    to start once on app boot."""

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS server_samples (
            ts            INTEGER PRIMARY KEY,
            load_1m       REAL,
            mem_used_pct  REAL,
            disk_used_pct REAL
        );
        CREATE INDEX IF NOT EXISTS idx_server_samples_ts ON server_samples (ts);
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(self.SCHEMA)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _record_one(self) -> None:
        now = int(time.time())
        load = _load_1m()
        mem  = _read_meminfo_pct()
        disk = _disk_used_pct()
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO server_samples (ts, load_1m, mem_used_pct, disk_used_pct) "
                    "VALUES (?, ?, ?, ?)",
                    (now, load, mem, disk),
                )
        except sqlite3.Error as exc:
            log.warning("server_samples insert failed: %s", exc)

    def _run(self) -> None:
        #Record one immediately so the dashboard isn't empty for a
        #full minute after a restart.
        self._record_one()
        while not self._stop.is_set():
            if self._stop.wait(SAMPLE_INTERVAL_SECONDS):
                return
            self._record_one()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="server-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    #---- Read-side helpers used by the /api/stats aggregation ----

    def samples_since(self, since_unix: int) -> list[tuple[int, float | None, float | None, float | None]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    "SELECT ts, load_1m, mem_used_pct, disk_used_pct FROM server_samples "
                    "WHERE ts >= ? ORDER BY ts",
                    (since_unix,),
                )
                return cur.fetchall()
        except sqlite3.Error as exc:
            log.warning("server_samples read failed: %s", exc)
            return []


def averaged_buckets(samples, now: datetime,
                     window_hours: int | None = None,
                     window_days: int | None = None) -> dict[str, list[dict]]:
    """Average load / mem / disk per bucket. Returns a dict with
    three series keyed `load_1m`, `mem_used_pct`, `disk_used_pct`,
    each a list of `{label, value}` (value is None when no sample
    landed in that bucket, so the chart can render gaps).
    """
    assert (window_hours is None) ^ (window_days is None)
    if window_hours is not None:
        start = (now - timedelta(hours=window_hours - 1)).replace(minute=0, second=0, microsecond=0)
        step = timedelta(hours=1)
        count = window_hours
        fmt = "%Y-%m-%dT%H"
    else:
        start = (now - timedelta(days=window_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        step = timedelta(days=1)
        count = window_days
        fmt = "%Y-%m-%d"

    bucket_keys: list[str] = []
    for i in range(count):
        bucket_keys.append((start + i * step).strftime(fmt))

    #Per-bucket running sums + counts for each metric.
    sums  = {k: [0.0, 0.0, 0.0] for k in bucket_keys}
    cnts  = {k: [0,   0,   0  ] for k in bucket_keys}
    for ts, load, mem, disk in samples:
        d = datetime.fromtimestamp(ts, tz=timezone.utc)
        k = d.strftime(fmt)
        if k not in sums:
            continue
        if load is not None:
            sums[k][0] += load; cnts[k][0] += 1
        if mem  is not None:
            sums[k][1] += mem;  cnts[k][1] += 1
        if disk is not None:
            sums[k][2] += disk; cnts[k][2] += 1

    def _series(idx: int) -> list[dict]:
        out: list[dict] = []
        for k in bucket_keys:
            n = cnts[k][idx]
            v = round(sums[k][idx] / n, 2) if n > 0 else None
            out.append({"label": k, "value": v})
        return out

    return {
        "load_1m":       _series(0),
        "mem_used_pct":  _series(1),
        "disk_used_pct": _series(2),
    }
