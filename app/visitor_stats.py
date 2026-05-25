"""Visitor analytics derived from nginx access logs.

Parses /var/log/nginx/access.log + rotated siblings, builds:

  * hourly histograms over the last 24 hours, 7 days and 30 days;
  * top-N breakdowns of country, browser, OS and device class
    (mobile vs desktop), each as a list of {label, count} pairs the
    front-end renders as doughnut charts.

Heuristics:

  * Bot user-agents are filtered out (the usual googlebot / bingbot
    / ahrefs / curl / monitoring crawl). The list is intentionally
    conservative; an unknown UA is treated as "other / desktop"
    rather than dropped, so the totals match what the operator
    sees in nginx logs minus the obvious robots.
  * GeoIP lookup uses ip-api.com's free batch endpoint. Each IP
    is cached forever in a small SQLite table (no expiry: the
    country of an IP doesn't change in any way that matters to
    aggregate stats). Uncached IPs queue up; up to 100 are
    resolved per stats refresh so we stay inside the public rate
    limit (15 batch requests / minute).
  * The whole aggregate snapshot is cached in-process for 5
    minutes; the dashboard can refresh as often as the user wants
    without re-parsing the log files.

Reads only: never writes to the nginx logs themselves.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple

log = logging.getLogger(__name__)

NGINX_LOG_DIR = Path("/var/log/nginx")
NGINX_LOG_NAME = "access.log"

#Combined log format: $remote_addr - $remote_user [$time_local]
#"$request" $status $body_bytes_sent "$http_referer" "$http_user_agent"
_LOG_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ '
    r'\[(?P<ts>[^\]]+)\] '
    r'"(?P<method>[A-Z]+) (?P<path>[^"]*?) HTTP/[0-9.]+" '
    r'(?P<status>\d+) (?P<bytes>\S+) '
    r'"(?P<referrer>[^"]*)" '
    r'"(?P<ua>[^"]*)"'
)

#nginx timestamp inside [...]: "24/May/2026:18:12:45 +0000"
_TS_FMT = "%d/%b/%Y:%H:%M:%S %z"

_BOT_RE = re.compile(
    r"bot|crawl|spider|google|bing|yandex|baidu|facebookexternalhit|"
    r"whatsapp|petalbot|ahrefs|semrush|monitor|uptimerobot|"
    r"curl|wget|python-requests|nodejs|axios|libwww|httpclient",
    re.IGNORECASE,
)

#Browser detection ordered by specificity: Edg before Chrome (Edge
#advertises both), Chrome before Safari (Chrome embeds AppleWebKit),
#Firefox last because its UA is straightforward.
_BROWSER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Edge",    re.compile(r"Edg/")),
    ("Opera",   re.compile(r"OPR/|Opera/")),
    ("Samsung", re.compile(r"SamsungBrowser/")),
    ("Vivaldi", re.compile(r"Vivaldi/")),
    ("Chrome",  re.compile(r"Chrome/")),
    ("Firefox", re.compile(r"Firefox/")),
    ("Safari",  re.compile(r"Safari/")),
]

_OS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("iOS",      re.compile(r"iPhone|iPad|iPod")),
    ("Android",  re.compile(r"Android")),
    ("Windows",  re.compile(r"Windows NT")),
    ("macOS",    re.compile(r"Mac OS X|Macintosh")),
    ("Linux",    re.compile(r"Linux")),
    ("ChromeOS", re.compile(r"CrOS")),
]


def _ua_browser(ua: str) -> str:
    for name, pat in _BROWSER_PATTERNS:
        if pat.search(ua):
            return name
    return "Other"


def _ua_os(ua: str) -> str:
    for name, pat in _OS_PATTERNS:
        if pat.search(ua):
            return name
    return "Other"


def _ua_is_mobile(ua: str) -> bool:
    #Conservative: "Mobile" token is the standard mobile marker;
    #Android tablets sometimes omit it, but those count as mobile
    #for our purposes anyway. iPad reports as Mac but with iPad in
    #UA, hence the explicit string match.
    return ("Mobile" in ua) or ("iPad" in ua) or ("iPhone" in ua) or ("Android" in ua)


class LogRow(NamedTuple):
    ts: datetime
    ip: str
    path: str
    status: int
    ua: str


def _iter_log_files(since: datetime) -> Iterator[Path]:
    """Yield the nginx access logs that could contain entries newer
    than `since`, newest first. Logs older than `since`'s day are
    skipped, both by filename order and by lazy mtime check.
    """
    if not NGINX_LOG_DIR.is_dir():
        return
    candidates = sorted(NGINX_LOG_DIR.glob(f"{NGINX_LOG_NAME}*"))
    #Reverse so we visit access.log.30.gz before .log.1 etc. We
    #actually want the OLDEST file first so the parser sees rows
    #in chronological order; reverse-sorted by suffix gives that.
    def _sort_key(p: Path) -> int:
        name = p.name
        if name == NGINX_LOG_NAME:
            return 0
        m = re.match(rf"{re.escape(NGINX_LOG_NAME)}\.(\d+)", name)
        if not m:
            return 999
        return -int(m.group(1))
    candidates.sort(key=_sort_key)
    for p in candidates:
        try:
            if datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) < since - timedelta(days=1):
                continue
        except OSError:
            continue
        yield p


def _open_log(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def parse_rows(since: datetime, max_rows: int = 500_000) -> list[LogRow]:
    """Read every nginx access log file potentially newer than
    `since` and return matching rows. `max_rows` caps memory in
    pathological cases (large bursts).
    """
    rows: list[LogRow] = []
    for path in _iter_log_files(since):
        try:
            with _open_log(path) as fh:
                for line in fh:
                    m = _LOG_RE.match(line)
                    if not m:
                        continue
                    try:
                        ts = datetime.strptime(m["ts"], _TS_FMT)
                    except ValueError:
                        continue
                    if ts < since:
                        continue
                    try:
                        status = int(m["status"])
                    except (TypeError, ValueError):
                        continue
                    rows.append(LogRow(
                        ts=ts.astimezone(timezone.utc),
                        ip=m["ip"],
                        path=m["path"] or "",
                        status=status,
                        ua=m["ua"] or "",
                    ))
                    if len(rows) >= max_rows:
                        return rows
        except OSError as exc:
            log.warning("nginx log %s unreadable: %s", path, exc)
    return rows


def _filter_human_visits(rows: Iterable[LogRow]) -> list[LogRow]:
    """Keep only one row per (ip, hour) for GET / hits with 2xx/3xx,
    bots filtered out. That collapses page reloads + asset fetches
    into a single "visit" event per hour per visitor.
    """
    seen: set[tuple[str, str]] = set()
    out: list[LogRow] = []
    for r in rows:
        if r.path != "/" or r.status >= 400:
            continue
        if _BOT_RE.search(r.ua):
            continue
        key = (r.ip, r.ts.strftime("%Y-%m-%dT%H"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


#---- Geo cache ---------------------------------------------------

class GeoCache:
    """SQLite-backed cache of IP -> ISO country code. ip-api.com
    free batch endpoint resolves up to 100 IPs / call and grants 15
    calls / minute. We stay well inside that by capping each stats
    refresh at one batch (100 unknowns) and only ever caching once
    per IP.
    """

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS geo_cache (
            ip           TEXT PRIMARY KEY,
            country_code TEXT,
            country_name TEXT,
            cached_at    INTEGER NOT NULL
        );
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(self.SCHEMA)

    def lookup_all(self, ips: Iterable[str]) -> dict[str, tuple[str | None, str | None]]:
        """Return {ip: (country_code, country_name)} for every cached
        IP in `ips`; uncached IPs are absent from the result.
        """
        result: dict[str, tuple[str | None, str | None]] = {}
        unique = list({ip for ip in ips if ip})
        if not unique:
            return result
        placeholders = ",".join("?" * len(unique))
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                f"SELECT ip, country_code, country_name FROM geo_cache WHERE ip IN ({placeholders})",
                unique,
            )
            for ip, cc, name in cur.fetchall():
                result[ip] = (cc, name)
        return result

    def store(self, batch: dict[str, tuple[str | None, str | None]]) -> None:
        if not batch:
            return
        now = int(time.time())
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO geo_cache (ip, country_code, country_name, cached_at) "
                    "VALUES (?, ?, ?, ?)",
                    [(ip, cc, name, now) for ip, (cc, name) in batch.items()],
                )

    def resolve_missing(self, ips: Iterable[str], cap: int = 100) -> dict[str, tuple[str | None, str | None]]:
        """Fetch up to `cap` uncached IPs from ip-api.com and persist
        the result. Returns the freshly-resolved subset (caller can
        merge with the prior lookup_all result).
        """
        cached = self.lookup_all(ips)
        unknown = [ip for ip in {x for x in ips if x} if ip not in cached]
        if not unknown:
            return {}
        unknown = unknown[:cap]
        try:
            req = urllib.request.Request(
                "http://ip-api.com/batch?fields=status,country,countryCode,query",
                data=json.dumps(unknown).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            log.warning("ip-api batch failed: %s", exc)
            return {}
        out: dict[str, tuple[str | None, str | None]] = {}
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            ip = entry.get("query")
            if not ip:
                continue
            if entry.get("status") == "success":
                out[ip] = (entry.get("countryCode"), entry.get("country"))
            else:
                #Cache the failure as a null entry to avoid retrying
                #on every refresh; many failed lookups are private
                #IPs (CGNAT, 10.x, etc.) that will never resolve.
                out[ip] = (None, None)
        self.store(out)
        return out


#---- Aggregation -------------------------------------------------

def _top_n(counter: dict[str, int], n: int = 10) -> list[dict]:
    items = sorted(counter.items(), key=lambda x: x[1], reverse=True)
    return [{"label": k, "count": v} for k, v in items[:n]]


def _hourly_buckets(rows: list[LogRow], now: datetime, window_hours: int) -> list[dict]:
    """Return a list of {label, count} buckets, one per hour over the
    last `window_hours`, oldest first. `label` is an ISO-formatted
    "YYYY-MM-DD HH:00" so the front-end can format it for display.
    """
    buckets: dict[str, int] = {}
    start = (now - timedelta(hours=window_hours - 1)).replace(minute=0, second=0, microsecond=0)
    for i in range(window_hours):
        key = (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H")
        buckets[key] = 0
    for r in rows:
        key = r.ts.strftime("%Y-%m-%dT%H")
        if key in buckets:
            buckets[key] += 1
    return [{"label": k, "count": v} for k, v in buckets.items()]


def _daily_buckets(rows: list[LogRow], now: datetime, window_days: int) -> list[dict]:
    buckets: dict[str, int] = {}
    start = (now - timedelta(days=window_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(window_days):
        key = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        buckets[key] = 0
    for r in rows:
        key = r.ts.strftime("%Y-%m-%d")
        if key in buckets:
            buckets[key] += 1
    return [{"label": k, "count": v} for k, v in buckets.items()]


class StatsSnapshot(NamedTuple):
    fetched_at_unix: float
    total_visits_24h: int
    total_visits_7d: int
    total_visits_30d: int
    unique_visitors_24h: int
    unique_visitors_7d: int
    unique_visitors_30d: int
    hourly_24h: list[dict]
    hourly_7d: list[dict]
    daily_30d: list[dict]
    countries: list[dict]
    browsers: list[dict]
    operating_systems: list[dict]
    devices: list[dict]


CACHE_TTL_SECONDS = 5 * 60
_cache: StatsSnapshot | None = None
_cache_lock = threading.Lock()
_geo: GeoCache | None = None


def get_snapshot(geo_db_path: Path) -> StatsSnapshot | None:
    """Return the cached snapshot if fresh, otherwise rebuild it
    from the current log files. Returns None only on a complete
    failure (nginx logs unreadable AND no cache yet).
    """
    global _cache, _geo
    if _geo is None:
        _geo = GeoCache(geo_db_path)

    with _cache_lock:
        now_unix = time.time()
        if _cache is not None and (now_unix - _cache.fetched_at_unix) < CACHE_TTL_SECONDS:
            return _cache

        now = datetime.now(timezone.utc)
        since_30d = now - timedelta(days=30)
        rows_30d = parse_rows(since_30d)
        visits_30d = _filter_human_visits(rows_30d)
        if not rows_30d:
            log.warning("no nginx log rows found in the last 30 days")
            return _cache

        since_7d = now - timedelta(days=7)
        since_24h = now - timedelta(hours=24)
        visits_7d  = [v for v in visits_30d if v.ts >= since_7d]
        visits_24h = [v for v in visits_30d if v.ts >= since_24h]

        #UA breakdowns: use the last-30-days window so the pie
        #charts read as "where do my visitors come from / what do
        #they use" rather than "what did one IP do in one hour".
        browsers: dict[str, int]  = defaultdict(int)
        oses:     dict[str, int]  = defaultdict(int)
        devices:  dict[str, int]  = defaultdict(int)
        for v in visits_30d:
            browsers[_ua_browser(v.ua)] += 1
            oses[_ua_os(v.ua)] += 1
            devices["Mobile" if _ua_is_mobile(v.ua) else "Desktop"] += 1

        #Geo: best-effort. Resolve up to 100 new IPs this call;
        #the rest stay "Unknown" until they get resolved on a
        #later refresh.
        ips_30d = {v.ip for v in visits_30d}
        cached_geo = _geo.lookup_all(ips_30d)
        fresh_geo  = _geo.resolve_missing(ips_30d - set(cached_geo))
        cached_geo.update(fresh_geo)
        country_counts: dict[str, int] = defaultdict(int)
        for v in visits_30d:
            cc, name = cached_geo.get(v.ip, (None, None))
            country_counts[name or "Unknown"] += 1

        snapshot = StatsSnapshot(
            fetched_at_unix=now_unix,
            total_visits_24h=len(visits_24h),
            total_visits_7d=len(visits_7d),
            total_visits_30d=len(visits_30d),
            unique_visitors_24h=len({v.ip for v in visits_24h}),
            unique_visitors_7d =len({v.ip for v in visits_7d}),
            unique_visitors_30d=len({v.ip for v in visits_30d}),
            hourly_24h=_hourly_buckets(visits_24h, now, 24),
            hourly_7d =_hourly_buckets(visits_7d,  now, 24 * 7),
            daily_30d =_daily_buckets(visits_30d, now, 30),
            countries=_top_n(country_counts, n=12),
            browsers =_top_n(browsers,        n=10),
            operating_systems=_top_n(oses,    n=10),
            devices  =_top_n(devices,         n=4),
        )
        _cache = snapshot
        return snapshot


def snapshot_to_dict(snap: StatsSnapshot) -> dict:
    return {
        "fetched_at_unix": snap.fetched_at_unix,
        "total_visits_24h": snap.total_visits_24h,
        "total_visits_7d":  snap.total_visits_7d,
        "total_visits_30d": snap.total_visits_30d,
        "unique_visitors_24h": snap.unique_visitors_24h,
        "unique_visitors_7d":  snap.unique_visitors_7d,
        "unique_visitors_30d": snap.unique_visitors_30d,
        "hourly_24h": snap.hourly_24h,
        "hourly_7d":  snap.hourly_7d,
        "daily_30d":  snap.daily_30d,
        "countries":  snap.countries,
        "browsers":   snap.browsers,
        "operating_systems": snap.operating_systems,
        "devices":    snap.devices,
    }
