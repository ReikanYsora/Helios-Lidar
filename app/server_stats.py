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


def _read_net_bytes() -> tuple[int, int] | None:
    """Sum rx + tx bytes across all non-loopback interfaces from
    /proc/net/dev. Returns (rx_total, tx_total) or None if the file
    can't be read (non-Linux dev box, etc.)."""
    try:
        rx_total = 0
        tx_total = 0
        with open("/proc/net/dev", "r", encoding="utf-8") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                iface, rest = line.split(":", 1)
                iface = iface.strip()
                if iface == "lo":
                    continue
                cols = rest.split()
                if len(cols) < 9:
                    continue
                rx_total += int(cols[0])
                tx_total += int(cols[8])
        return (rx_total, tx_total)
    except (OSError, ValueError):
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
            #Idempotent column adds for the network-bandwidth columns
            #(net_rx_mbps + net_tx_mbps). Older rows stay NULL, which
            #the chart layer treats as "no data for this bucket".
            for col in ("net_rx_mbps REAL", "net_tx_mbps REAL"):
                try:
                    conn.execute(f"ALTER TABLE server_samples ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass  #column already exists
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        #Cumulative-counter baseline for the network-rate computation:
        #(unix_ts, rx_bytes, tx_bytes) from the previous tick. None
        #after a restart, which makes the first sample record nulls
        #for the rates rather than a huge spike from uptime-since-boot.
        self._last_net: tuple[int, int, int] | None = None

    def _record_one(self) -> None:
        now = int(time.time())
        load = _load_1m()
        mem  = _read_meminfo_pct()
        disk = _disk_used_pct()

        #Network bandwidth: (bytes delta) / (seconds delta) -> Mbps.
        #/proc/net/dev counters are cumulative since boot, so we keep
        #the previous tick in memory and emit the rate per sample.
        rx_mbps = tx_mbps = None
        net = _read_net_bytes()
        if net is not None:
            rx_bytes, tx_bytes = net
            if self._last_net is not None:
                last_ts, last_rx, last_tx = self._last_net
                dt = now - last_ts
                if dt > 0:
                    drx = max(0, rx_bytes - last_rx)
                    dtx = max(0, tx_bytes - last_tx)
                    rx_mbps = round(drx * 8 / dt / 1_000_000, 3)
                    tx_mbps = round(dtx * 8 / dt / 1_000_000, 3)
            self._last_net = (now, rx_bytes, tx_bytes)

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO server_samples "
                    "(ts, load_1m, mem_used_pct, disk_used_pct, net_rx_mbps, net_tx_mbps) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (now, load, mem, disk, rx_mbps, tx_mbps),
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

    def samples_since(self, since_unix: int) -> list[tuple]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    "SELECT ts, load_1m, mem_used_pct, disk_used_pct, "
                    "       net_rx_mbps, net_tx_mbps "
                    "FROM server_samples WHERE ts >= ? ORDER BY ts",
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

    #Per-bucket running sums + counts for each metric. Five tracked
    #series: load, mem%, disk%, network rx Mbps, network tx Mbps.
    sums  = {k: [0.0, 0.0, 0.0, 0.0, 0.0] for k in bucket_keys}
    cnts  = {k: [0,   0,   0,   0,   0  ] for k in bucket_keys}
    for row in samples:
        ts = row[0]
        load = row[1] if len(row) > 1 else None
        mem  = row[2] if len(row) > 2 else None
        disk = row[3] if len(row) > 3 else None
        nrx  = row[4] if len(row) > 4 else None
        ntx  = row[5] if len(row) > 5 else None
        d = datetime.fromtimestamp(ts, tz=timezone.utc)
        k = d.strftime(fmt)
        if k not in sums:
            continue
        if load is not None: sums[k][0] += load; cnts[k][0] += 1
        if mem  is not None: sums[k][1] += mem;  cnts[k][1] += 1
        if disk is not None: sums[k][2] += disk; cnts[k][2] += 1
        if nrx  is not None: sums[k][3] += nrx;  cnts[k][3] += 1
        if ntx  is not None: sums[k][4] += ntx;  cnts[k][4] += 1

    def _series(idx: int, decimals: int = 2) -> list[dict]:
        out: list[dict] = []
        for k in bucket_keys:
            n = cnts[k][idx]
            v = round(sums[k][idx] / n, decimals) if n > 0 else None
            out.append({"label": k, "value": v})
        return out

    return {
        "load_1m":       _series(0),
        "mem_used_pct":  _series(1),
        "disk_used_pct": _series(2),
        "net_rx_mbps":   _series(3, decimals=3),
        "net_tx_mbps":   _series(4, decimals=3),
    }
