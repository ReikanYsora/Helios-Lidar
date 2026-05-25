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


#Own-host substrings; referrers matching any of these are
#considered internal navigation and stripped from the external-
#referrer chart so the chart shows only "where the visitor came
#from" (search engines, forums, social, etc.).
_OWN_HOSTS = ("helios-lidar.org", "37.59.122.223", "localhost")


def _referrer_host(ref: str) -> str | None:
    """Return the bare hostname of a referrer URL, or None if the
    referrer is empty / "-" / one of our own hosts. Strips leading
    "www.".
    """
    if not ref or ref == "-":
        return None
    #Skip our own URLs (internal navigation).
    for h in _OWN_HOSTS:
        if h in ref:
            return None
    try:
        from urllib.parse import urlparse
        host = urlparse(ref).hostname
    except Exception:
        return None
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host


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
    referrer: str


def _iter_log_files(since: datetime) -> Iterator[Path]:
    """Yield the nginx access logs that could contain entries newer
    than `since`, oldest file first so rows accumulate chronologically.
    Files whose mtime predates the window are skipped.
    """
    if not NGINX_LOG_DIR.is_dir():
        return
    candidates = sorted(NGINX_LOG_DIR.glob(f"{NGINX_LOG_NAME}*"))
    #access.log -> 0, access.log.1 -> -1, access.log.10.gz -> -10, etc.
    #Sort so the highest suffix (oldest archive) comes first, then
    #down to access.log (newest) last; chronological order.
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


def parse_rows(since: datetime, max_rows: int = 2_000_000) -> list[LogRow]:
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
                        referrer=m["referrer"] or "",
                    ))
                    if len(rows) >= max_rows:
                        return rows
        except OSError as exc:
            log.warning("nginx log %s unreadable: %s", path, exc)
    return rows


def parse_post_jobs_entries(since: datetime, max_rows: int = 200_000) -> list[tuple[int, str]]:
    """Scan the nginx access logs and return `(unix_ts, ip)` for
    every POST /jobs request at or after `since`, oldest first.

    Used by the retroactive backfill of `conversions.client_ip`:
    a conversion's completion timestamp is correlated against the
    nearest POST /jobs entry preceding it to recover the originating
    IP for legacy rows that pre-date the column being added.
    """
    out: list[tuple[int, str]] = []
    for path in _iter_log_files(since):
        try:
            with _open_log(path) as fh:
                for line in fh:
                    m = _LOG_RE.match(line)
                    if not m:
                        continue
                    if (m["method"] or "") != "POST":
                        continue
                    p = m["path"] or ""
                    if not (p == "/jobs" or p.startswith("/jobs?")):
                        continue
                    try:
                        ts = datetime.strptime(m["ts"], _TS_FMT).astimezone(timezone.utc)
                    except ValueError:
                        continue
                    if ts < since:
                        continue
                    out.append((int(ts.timestamp()), m["ip"]))
                    if len(out) >= max_rows:
                        return out
        except OSError as exc:
            log.warning("nginx log %s unreadable: %s", path, exc)
    return out


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

    def resolve_missing(self, ips: Iterable[str], cap: int = 300) -> dict[str, tuple[str | None, str | None]]:
        """Fetch up to `cap` uncached IPs from ip-api.com and persist
        the result. Splits into 100-IP batches (ip-api batch endpoint
        cap) and stays well inside the 15-batch/min rate limit so a
        single refresh can warm up several hundred IPs in one go.
        """
        cached = self.lookup_all(ips)
        unknown = [ip for ip in {x for x in ips if x} if ip not in cached]
        if not unknown:
            return {}
        unknown = unknown[:cap]
        out: dict[str, tuple[str | None, str | None]] = {}
        for chunk_start in range(0, len(unknown), 100):
            chunk = unknown[chunk_start:chunk_start + 100]
            try:
                req = urllib.request.Request(
                    "http://ip-api.com/batch?fields=status,country,countryCode,query",
                    data=json.dumps(chunk).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                log.warning("ip-api batch failed: %s", exc)
                continue
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                ip = entry.get("query")
                if not ip:
                    continue
                if entry.get("status") == "success":
                    out[ip] = (entry.get("countryCode"), entry.get("country"))
                else:
                    #Cache the failure as a null entry to avoid
                    #retrying on every refresh; many failed lookups
                    #are private IPs (CGNAT, 10.x, etc.).
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


def _growth_index_daily(visits_1y: list,
                        conv_ts_1y: list[int],
                        dl_daily_1y: list[dict],
                        now: datetime,
                        window_days: int = 365) -> dict:
    """Build a composite "growth of the app" daily index from the
    last `window_days` of telemetry.

    Formula (per day):
        score = unique_visitors + 5 * conversions + 10 * card_downloads

    The weights reflect the funnel cost: a visitor is cheap, a
    conversion proves real usage of the LiDAR pipeline, a download
    is a long-term commitment to the card. Weights are intentionally
    small integers, not tuned, so the curve stays interpretable
    (10 means "one download today = ten visits today" in score
    space).

    On top of the raw daily score we layer:
        - 7-day EMA (exponentially-weighted moving average,
          alpha = 2/(N+1) with N=7) to absorb day-of-week noise
        - linear least-squares trend fit on the EMA, which gives
          the average score-points / day slope we report as
          `slope_per_day`
        - week-over-week growth %: last-7-day avg vs prior-7-day avg
        - month-over-month growth %: last-30-day avg vs prior-30-day avg

    Returns a Chart.js-friendly payload plus the scalar growth
    metrics for KPI display.
    """
    start_day = (now - timedelta(days=window_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    labels: list[str] = [(start_day + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(window_days)]
    key_to_index = {k: i for i, k in enumerate(labels)}

    unique_ips_per_day: list[set] = [set() for _ in range(window_days)]
    for v in visits_1y:
        k = v.ts.strftime("%Y-%m-%d")
        i = key_to_index.get(k)
        if i is not None:
            unique_ips_per_day[i].add(v.ip)
    uniques = [len(s) for s in unique_ips_per_day]

    conv = [0] * window_days
    for ts in conv_ts_1y:
        k = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        i = key_to_index.get(k)
        if i is not None:
            conv[i] += 1

    dl = [0] * window_days
    for b in dl_daily_1y:
        i = key_to_index.get(b.get("label"))
        if i is not None:
            dl[i] = int(b.get("count", 0))

    W_VISIT, W_CONV, W_DL = 1, 5, 10
    raw = [uniques[i] * W_VISIT + conv[i] * W_CONV + dl[i] * W_DL for i in range(window_days)]

    #7-day EMA: smooth out weekly seasonality. Seed with the raw
    #value at index 0 so the very first day isn't pulled toward
    #zero before any history has accumulated.
    alpha = 2.0 / (7 + 1)
    ema: list[float] = []
    prev = float(raw[0]) if raw else 0.0
    for v in raw:
        prev = alpha * float(v) + (1.0 - alpha) * prev
        ema.append(round(prev, 2))

    #Linear regression on the EMA (least squares, closed form).
    #Slope is "score points per day" averaged across the window;
    #the trend line lets the eye see whether we're decelerating
    #even when the EMA still climbs.
    n = len(ema)
    slope = 0.0
    intercept = ema[0] if ema else 0.0
    if n >= 2:
        xs = list(range(n))
        mean_x = sum(xs) / n
        mean_y = sum(ema) / n
        num = sum((xs[i] - mean_x) * (ema[i] - mean_y) for i in range(n))
        den = sum((xs[i] - mean_x) ** 2 for i in range(n))
        if den > 0:
            slope = num / den
            intercept = mean_y - slope * mean_x
    trend = [round(slope * x + intercept, 2) for x in range(n)]

    def _safe_mean(xs: list[float]) -> float:
        return (sum(xs) / len(xs)) if xs else 0.0

    def _growth_pct(window: int) -> float | None:
        if n < window * 2:
            return None
        recent = _safe_mean(raw[-window:])
        prior  = _safe_mean(raw[-window * 2:-window])
        if prior <= 0:
            return None
        return round(100.0 * (recent - prior) / prior, 1)

    return {
        "labels":         labels,
        "raw":            raw,
        "ema":            ema,
        "trend":          trend,
        "slope_per_day":  round(slope, 3),
        "growth_pct_wow": _growth_pct(7),
        "growth_pct_mom": _growth_pct(30),
        "weights":        {"visitor": W_VISIT, "conversion": W_CONV, "download": W_DL},
    }


def _conversion_country_rows(conv_rows: list[tuple[int, str | None]],
                             geo: dict) -> list[dict]:
    """Aggregate `(completed_at, client_ip)` conversion records into
    a `{code, name, count}` table sorted descending. IPs not in the
    geo cache (private, unresolved, or pre-backfill) fall into the
    "Unknown" bucket so we still see how many uncategorised
    conversions are in the window."""
    counts: dict[tuple[str | None, str], int] = defaultdict(int)
    for _ts, ip in conv_rows:
        if ip:
            cc, name = geo.get(ip, (None, None))
        else:
            cc, name = (None, None)
        counts[(cc, name or "Unknown")] += 1
    rows = [{"code": cc, "name": name, "count": n}
            for (cc, name), n in counts.items()]
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows


def _country_table_rows(visits: list, geo: dict) -> list[dict]:
    """Return a list of `{code, name, count}` rows, one per country
    seen in `visits`, sorted by visit count descending. `code` is
    the ISO 3166-1 alpha-2 string (used by the front-end to emit
    the flag emoji); None when the IP isn't in `geo` (private /
    unresolved). Unlimited length on purpose, the table scrolls
    rather than truncates."""
    counts: dict[tuple[str | None, str], int] = defaultdict(int)
    for v in visits:
        cc, name = geo.get(v.ip, (None, None))
        counts[(cc, name or "Unknown")] += 1
    rows = [{"code": cc, "name": name, "count": n}
            for (cc, name), n in counts.items()]
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows


def _referrer_table_rows(visits: list) -> list[dict]:
    """Return a list of `{host, count}` rows for the external
    referrer of every visit, "Direct" when the request had no
    referrer, sorted by count descending. Same unlimited-length
    contract as `_country_table_rows`."""
    counts: dict[str, int] = defaultdict(int)
    for v in visits:
        host = _referrer_host(v.referrer)
        counts[host or "Direct"] += 1
    rows = [{"host": h, "count": n} for h, n in counts.items()]
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows


def _ts_hourly_buckets(timestamps: list[int], now: datetime, window_hours: int) -> list[dict]:
    """Same as _hourly_buckets but takes raw unix timestamps."""
    buckets: dict[str, int] = {}
    start = (now - timedelta(hours=window_hours - 1)).replace(minute=0, second=0, microsecond=0)
    for i in range(window_hours):
        key = (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H")
        buckets[key] = 0
    for ts in timestamps:
        d = datetime.fromtimestamp(ts, tz=timezone.utc)
        key = d.strftime("%Y-%m-%dT%H")
        if key in buckets:
            buckets[key] += 1
    return [{"label": k, "count": v} for k, v in buckets.items()]


def _per_tag_deltas(per_tag_snapshots: list[tuple[int, str, int]],
                    baselines: dict[str, int],
                    now: datetime,
                    window_hours: int | None = None,
                    window_days: int | None = None) -> dict:
    """Build a per-version stacked histogram from per-tag snapshots.

    Input rows are `(ts, tag, cumulative_count)` ordered by ts. For
    each tag we bucket the cumulative counts by time window and
    compute per-bucket deltas vs the highest seen so far for that
    tag (starting from `baselines[tag]` when set, else the bucket's
    own min so the first bucket isn't a flat zero on cold-start).

    Returns Chart.js-friendly:
        {
            "labels":   [bucket_keys...],   //chronological
            "datasets": [
                {"tag": "<release>", "data": [count_per_bucket, ...]},
                ...
            ],
        }
    """
    assert (window_hours is None) ^ (window_days is None)
    if window_hours is not None:
        start = (now - timedelta(hours=window_hours - 1)).replace(minute=0, second=0, microsecond=0)
        step  = timedelta(hours=1)
        count = window_hours
        fmt   = "%Y-%m-%dT%H"
    else:
        start = (now - timedelta(days=window_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        step  = timedelta(days=1)
        count = window_days
        fmt   = "%Y-%m-%d"

    labels: list[str] = [(start + i * step).strftime(fmt) for i in range(count)]
    key_to_index = {k: i for i, k in enumerate(labels)}

    #Per-tag: bucket_max + bucket_min, each indexed by tag then bucket index.
    tag_max: dict[str, list[int]] = {}
    tag_min: dict[str, list[int]] = {}
    for ts, tag, total in per_tag_snapshots:
        d = datetime.fromtimestamp(ts, tz=timezone.utc)
        k = d.strftime(fmt)
        i = key_to_index.get(k)
        if i is None:
            continue
        if tag not in tag_max:
            tag_max[tag] = [-1] * count
            tag_min[tag] = [-1] * count
        if tag_max[tag][i] < 0 or total > tag_max[tag][i]:
            tag_max[tag][i] = total
        if tag_min[tag][i] < 0 or total < tag_min[tag][i]:
            tag_min[tag][i] = total

    #Also seed baselines for tags that appear only in the window
    #(without a pre-window snapshot). Their first-bucket delta uses
    #the bucket's own min as implicit baseline.
    all_tags = set(tag_max) | set(baselines)

    #Sort tags by version order: parse "vX.Y.Z" descending so the
    #newest release sits at the top of the stack legend.
    def _ver_key(t: str) -> tuple[int, ...]:
        t = t.lstrip("vV").split("-", 1)[0]
        try:
            return tuple(int(x) for x in t.split("."))
        except ValueError:
            return (0,)
    sorted_tags = sorted(all_tags, key=_ver_key, reverse=True)

    datasets: list[dict] = []
    for tag in sorted_tags:
        prev = baselines.get(tag)
        bucket_max = tag_max.get(tag, [-1] * count)
        bucket_min = tag_min.get(tag, [-1] * count)
        data: list[int] = []
        for i in range(count):
            cmax = bucket_max[i]
            if cmax < 0:
                data.append(0)
            else:
                if prev is None:
                    data.append(max(0, cmax - bucket_min[i]))
                else:
                    data.append(max(0, cmax - prev))
                prev = cmax
        #Skip tags whose window is entirely zeros to keep the chart
        #legend short on long ranges.
        if any(v > 0 for v in data):
            datasets.append({"tag": tag, "data": data})

    return {"labels": labels, "datasets": datasets}


def _bmac_daily_amounts(donations, now: datetime, window_days: int) -> list[dict]:
    """Sum donation amounts per day over the last `window_days`.
    Returns one bucket per day even when empty so the chart canvas
    keeps a stable width."""
    buckets: dict[str, float] = {}
    start = (now - timedelta(days=window_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(window_days):
        key = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        buckets[key] = 0.0
    for d in donations:
        key = datetime.fromtimestamp(d.ts_unix, tz=timezone.utc).strftime("%Y-%m-%d")
        if key in buckets:
            buckets[key] += float(d.amount)
    return [{"label": k, "count": round(v, 2)} for k, v in buckets.items()]


def _ts_daily_buckets(timestamps: list[int], now: datetime, window_days: int) -> list[dict]:
    buckets: dict[str, int] = {}
    start = (now - timedelta(days=window_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(window_days):
        key = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        buckets[key] = 0
    for ts in timestamps:
        key = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if key in buckets:
            buckets[key] += 1
    return [{"label": k, "count": v} for k, v in buckets.items()]


def _snapshots_to_deltas(snapshots: list[tuple[int, int]],
                         baseline: tuple[int, int] | None,
                         now: datetime,
                         window_hours: int | None = None,
                         window_days: int | None = None) -> list[dict]:
    """Convert a list of (ts, cumulative_count) snapshots into a
    histogram of new downloads per bucket.

    For each bucket, the bucket count is `max_in_bucket - prev_max`
    where prev_max is the highest cumulative count we've seen at or
    before the start of the bucket. `baseline` is the snapshot just
    before the window starts; without it the first bucket's delta
    would falsely contain every download ever made up to that point.
    """
    assert (window_hours is None) ^ (window_days is None), "exactly one window"
    bucket_keys: list[str] = []
    if window_hours is not None:
        start = (now - timedelta(hours=window_hours - 1)).replace(minute=0, second=0, microsecond=0)
        for i in range(window_hours):
            bucket_keys.append((start + timedelta(hours=i)).strftime("%Y-%m-%dT%H"))
        fmt = "%Y-%m-%dT%H"
    else:
        start = (now - timedelta(days=window_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        for i in range(window_days):
            bucket_keys.append((start + timedelta(days=i)).strftime("%Y-%m-%d"))
        fmt = "%Y-%m-%d"

    #Largest + smallest cumulative count seen in each bucket; -1
    #marks an empty bucket. Tracking both lets us compute within-
    #bucket activity when no pre-window baseline is available.
    bucket_max: dict[str, int] = {k: -1 for k in bucket_keys}
    bucket_min: dict[str, int] = {k: -1 for k in bucket_keys}
    for ts, total in snapshots:
        d = datetime.fromtimestamp(ts, tz=timezone.utc)
        key = d.strftime(fmt)
        if key not in bucket_max:
            continue
        if bucket_max[key] < 0 or total > bucket_max[key]:
            bucket_max[key] = total
        if bucket_min[key] < 0 or total < bucket_min[key]:
            bucket_min[key] = total

    prev = baseline[1] if baseline is not None else None
    out: list[dict] = []
    for k in bucket_keys:
        cur_max = bucket_max[k]
        if cur_max < 0:
            #No snapshot in this bucket: it counts as 0 new downloads.
            out.append({"label": k, "count": 0})
        else:
            if prev is None:
                #First bucket without any pre-window baseline: use the
                #bucket's own min as the implicit baseline so we can
                #still show the within-bucket activity instead of a
                #flat zero. Subsequent buckets inherit prev=cur_max.
                delta = max(0, cur_max - bucket_min[k])
            else:
                delta = max(0, cur_max - prev)
            out.append({"label": k, "count": delta})
            prev = cur_max
    return out


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
    daily_1y: list[dict]
    total_visits_1y: int
    unique_visitors_1y: int
    browsers: list[dict]
    operating_systems: list[dict]
    devices: list[dict]
    #Conversion histograms (rows from the stats.db conversions table).
    conversions_total: int
    conversions_24h: int
    conversions_7d: int
    conversions_30d: int
    conversions_1y: int
    conversions_hourly_24h: list[dict]
    conversions_hourly_7d: list[dict]
    conversions_daily_30d: list[dict]
    conversions_daily_1y: list[dict]
    #Download histograms (deltas between download_snapshots rows).
    #Sparse until enough snapshots have accumulated; the dashboard
    #renders the zero-padded buckets either way so the canvas size
    #stays stable.
    downloads_total: int
    downloads_24h: int
    downloads_7d: int
    downloads_30d: int
    downloads_1y: int
    downloads_hourly_24h: list[dict]
    downloads_hourly_7d: list[dict]
    downloads_daily_30d: list[dict]
    downloads_daily_1y: list[dict]
    #BMaC donations (empty unless the BMAC_TOKEN env var is set).
    donations_configured: bool
    donations_total_amount: float
    donations_24h_amount: float
    donations_7d_amount: float
    donations_30d_amount: float
    donations_1y_amount: float
    donations_daily_30d: list[dict]
    donations_daily_1y: list[dict]
    #Server-load samples (load_1m / mem_used_pct / disk_used_pct
    #averaged per bucket). Hourly over 24 h; daily for 7d / 30d / 1y.
    server_hourly_24h: dict
    server_daily_7d:   dict
    server_daily_30d:  dict
    server_daily_1y:   dict
    #Per-version downloads: stacked-bar-ready {labels, datasets[]}
    #where each dataset is {tag, data[]}. Same four windows as the
    #other histograms.
    downloads_pv_hourly_24h: dict
    downloads_pv_hourly_7d:  dict
    downloads_pv_daily_30d:  dict
    downloads_pv_daily_1y:   dict
    #Per-country tables (one per range). Each is an ordered list
    #of `{code, name, count}` rows sorted by count descending. The
    #front-end emits the flag emoji from `code`. No size cap, the
    #table is scrollable.
    countries_table_24h: list[dict]
    countries_table_7d:  list[dict]
    countries_table_30d: list[dict]
    countries_table_1y:  list[dict]
    #Per-range external referrers as a `{host, count}` table sorted
    #descending. "Direct" is the bucket for visits with no Referer.
    referrers_table_24h: list[dict]
    referrers_table_7d:  list[dict]
    referrers_table_30d: list[dict]
    referrers_table_1y:  list[dict]
    #Per-range conversions by originating country. Source IP is the
    #x-forwarded-for of the POST /jobs request, captured at job
    #creation. Pre-backfill rows surface as "Unknown" until the
    #nginx-log correlation catches them.
    conversions_country_24h: list[dict]
    conversions_country_7d:  list[dict]
    conversions_country_30d: list[dict]
    conversions_country_1y:  list[dict]
    #Composite "growth of the app" daily index over the last 1 y,
    #weighted sum of unique visitors + conversions + downloads, with
    #a 7-day EMA + linear trend overlay. See _growth_index_daily().
    growth_index_1y: dict


CACHE_TTL_SECONDS = 5 * 60
_cache: StatsSnapshot | None = None
_cache_lock = threading.Lock()
_geo: GeoCache | None = None


def get_snapshot(geo_db_path: Path, stats_store=None, downloads_module=None,
                 server_sampler=None) -> StatsSnapshot | None:
    """Return the cached snapshot if fresh, otherwise rebuild it
    from the current log files. Returns None only on a complete
    failure (nginx logs unreadable AND no cache yet).

    `stats_store` is the StatsStore instance (for the conversions
    table + the download_snapshots table). `downloads_module` is
    the helios_downloads module (we call get_downloads_snapshot
    with a callback so the cumulative-downloads history feeds the
    download_snapshots table).
    """
    global _cache, _geo
    if _geo is None:
        _geo = GeoCache(geo_db_path)

    with _cache_lock:
        now_unix = time.time()
        if _cache is not None and (now_unix - _cache.fetched_at_unix) < CACHE_TTL_SECONDS:
            return _cache

        now = datetime.now(timezone.utc)
        since_1y  = now - timedelta(days=365)
        since_30d = now - timedelta(days=30)
        rows_1y = parse_rows(since_1y)
        visits_1y = _filter_human_visits(rows_1y)
        if not rows_1y:
            log.warning("no nginx log rows found in the last year")
            return _cache
        visits_30d = [v for v in visits_1y if v.ts >= since_30d]

        since_7d = now - timedelta(days=7)
        since_24h = now - timedelta(hours=24)
        visits_7d  = [v for v in visits_30d if v.ts >= since_7d]
        visits_24h = [v for v in visits_30d if v.ts >= since_24h]

        #UA breakdowns: use the last-30-days window so the pie
        #charts read as "where do my visitors come from / what do
        #they use" rather than "what did one IP do in one hour".
        browsers:  dict[str, int]  = defaultdict(int)
        oses:      dict[str, int]  = defaultdict(int)
        devices:   dict[str, int]  = defaultdict(int)
        for v in visits_30d:
            browsers[_ua_browser(v.ua)] += 1
            oses[_ua_os(v.ua)] += 1
            devices["Mobile" if _ua_is_mobile(v.ua) else "Desktop"] += 1

        #Geo: best-effort. Resolve up to 100 new IPs this call;
        #the rest stay "Unknown" until they get resolved on a
        #later refresh. The 1y window only pulls already-cached
        #entries since we don't want a year of unknown IPs to all
        #queue up for resolution at once.
        ips_30d = {v.ip for v in visits_30d}
        cached_geo = _geo.lookup_all(ips_30d)
        fresh_geo  = _geo.resolve_missing(ips_30d - set(cached_geo))
        cached_geo.update(fresh_geo)
        ips_1y = {v.ip for v in visits_1y}
        geo_1y = _geo.lookup_all(ips_1y)
        geo_1y.update(cached_geo)

        #Conversion histograms. Read from the stats.db conversions
        #table; cheap (single SELECT bounded by since_30d).
        conv_total = 0
        conv_24h = conv_7d = conv_30d = 0
        conv_hourly_24h: list[dict] = _ts_hourly_buckets([], now, 24)
        conv_hourly_7d:  list[dict] = _ts_hourly_buckets([], now, 24 * 7)
        conv_daily_30d:  list[dict] = _ts_daily_buckets([],  now, 30)
        conv_rows_1y: list[tuple[int, str | None]] = []
        if stats_store is not None:
            try:
                conv_total = stats_store.total_conversions()
                conv_rows_1y = stats_store.conversion_rows_since(int(since_1y.timestamp()))
                conv_ts_30d = [ts for ts, _ in conv_rows_1y if ts >= int(since_30d.timestamp())]
                conv_24h = sum(1 for t in conv_ts_30d if t >= int(since_24h.timestamp()))
                conv_7d  = sum(1 for t in conv_ts_30d if t >= int(since_7d.timestamp()))
                conv_30d = len(conv_ts_30d)
                conv_hourly_24h = _ts_hourly_buckets(conv_ts_30d, now, 24)
                conv_hourly_7d  = _ts_hourly_buckets(conv_ts_30d, now, 24 * 7)
                conv_daily_30d  = _ts_daily_buckets(conv_ts_30d, now, 30)
            except Exception:
                log.exception("conversion histogram build failed")

        #Resolve any conversion IPs we don't have in cache yet; the
        #count is small (one POST /jobs per conversion) so we can
        #afford to push them through the ip-api batch in this call.
        conv_ips_unresolved = {ip for _ts, ip in conv_rows_1y
                               if ip and ip not in geo_1y}
        if conv_ips_unresolved:
            extra_geo = _geo.lookup_all(conv_ips_unresolved)
            still_missing = conv_ips_unresolved - set(extra_geo)
            if still_missing:
                extra_geo.update(_geo.resolve_missing(still_missing))
            geo_1y.update(extra_geo)

        def _conv_window(ts_min: int) -> list[tuple[int, str | None]]:
            return [r for r in conv_rows_1y if r[0] >= ts_min]
        conv_country_24h = _conversion_country_rows(_conv_window(int(since_24h.timestamp())), geo_1y)
        conv_country_7d  = _conversion_country_rows(_conv_window(int(since_7d.timestamp())),  geo_1y)
        conv_country_30d = _conversion_country_rows(_conv_window(int(since_30d.timestamp())), geo_1y)
        conv_country_1y  = _conversion_country_rows(conv_rows_1y, geo_1y)

        #Download snapshots: refresh GitHub now (which records a new
        #row in download_snapshots via the on_fresh callback), then
        #read the recent history + diff into per-bucket new-downloads.
        dl_total = 0
        dl_24h = dl_7d = dl_30d = 0
        dl_hourly_24h: list[dict] = _ts_hourly_buckets([], now, 24)
        dl_hourly_7d:  list[dict] = _ts_hourly_buckets([], now, 24 * 7)
        dl_daily_30d:  list[dict] = _ts_daily_buckets([],  now, 30)
        dl_1y = 0
        dl_daily_1y: list[dict]  = _ts_daily_buckets([],  now, 365)
        dl_pv_24h = {"labels": [], "datasets": []}
        dl_pv_7d  = {"labels": [], "datasets": []}
        dl_pv_30d = {"labels": [], "datasets": []}
        dl_pv_1y  = {"labels": [], "datasets": []}
        if downloads_module is not None and stats_store is not None:
            try:
                dl_snap = downloads_module.get_downloads_snapshot(
                    on_fresh=stats_store.record_download_snapshot,
                )
                if dl_snap is not None:
                    dl_total = dl_snap.total_downloads
                hist_24h_unix = int(since_24h.timestamp())
                hist_7d_unix  = int(since_7d.timestamp())
                hist_30d_unix = int(since_30d.timestamp())
                hist_1y_unix  = int(since_1y.timestamp())
                snaps_1y  = stats_store.download_snapshots(hist_1y_unix)
                baseline_1y  = stats_store.last_download_snapshot_before(hist_1y_unix)
                baseline_30d = stats_store.last_download_snapshot_before(hist_30d_unix)
                baseline_7d  = stats_store.last_download_snapshot_before(hist_7d_unix)
                baseline_24h = stats_store.last_download_snapshot_before(hist_24h_unix)
                #Window-scoped slices: re-use snaps_1y filtered by ts.
                snaps_30d = [s for s in snaps_1y if s[0] >= hist_30d_unix]
                snaps_7d  = [s for s in snaps_1y if s[0] >= hist_7d_unix]
                snaps_24h = [s for s in snaps_1y if s[0] >= hist_24h_unix]
                dl_daily_1y   = _snapshots_to_deltas(snaps_1y,  baseline_1y,  now, window_days=365)
                dl_daily_30d  = _snapshots_to_deltas(snaps_30d, baseline_30d, now, window_days=30)
                dl_hourly_7d  = _snapshots_to_deltas(snaps_7d,  baseline_7d,  now, window_hours=24 * 7)
                dl_hourly_24h = _snapshots_to_deltas(snaps_24h, baseline_24h, now, window_hours=24)
                dl_24h = sum(b["count"] for b in dl_hourly_24h)
                dl_7d  = sum(b["count"] for b in dl_hourly_7d)
                dl_30d = sum(b["count"] for b in dl_daily_30d)
                dl_1y  = sum(b["count"] for b in dl_daily_1y)

                #Per-version time series: read all per-tag snapshots
                #in the 1y window once, slice by window for the
                #shorter ranges, and run the per-tag delta builder
                #with the right baseline per window.
                pv_snaps_1y = stats_store.download_per_tag_snapshots_since(hist_1y_unix)
                pv_snaps_30d = [r for r in pv_snaps_1y if r[0] >= hist_30d_unix]
                pv_snaps_7d  = [r for r in pv_snaps_1y if r[0] >= hist_7d_unix]
                pv_snaps_24h = [r for r in pv_snaps_1y if r[0] >= hist_24h_unix]
                base_24h = stats_store.last_download_per_tag_before(hist_24h_unix)
                base_7d  = stats_store.last_download_per_tag_before(hist_7d_unix)
                base_30d = stats_store.last_download_per_tag_before(hist_30d_unix)
                base_1y  = stats_store.last_download_per_tag_before(hist_1y_unix)
                dl_pv_24h = _per_tag_deltas(pv_snaps_24h, base_24h, now, window_hours=24)
                dl_pv_7d  = _per_tag_deltas(pv_snaps_7d,  base_7d,  now, window_hours=24 * 7)
                dl_pv_30d = _per_tag_deltas(pv_snaps_30d, base_30d, now, window_days=30)
                dl_pv_1y  = _per_tag_deltas(pv_snaps_1y,  base_1y,  now, window_days=365)
            except Exception:
                log.exception("download histogram build failed")

        #Conversions 1y
        conv_1y = 0
        conv_ts_1y: list[int] = []
        conv_daily_1y: list[dict] = _ts_daily_buckets([], now, 365)
        if stats_store is not None:
            try:
                conv_ts_1y = stats_store.conversion_timestamps(int(since_1y.timestamp()))
                conv_1y = len(conv_ts_1y)
                conv_daily_1y = _ts_daily_buckets(conv_ts_1y, now, 365)
            except Exception:
                log.exception("conversion 1y build failed")

        #Server load samples: per-minute rows averaged per bucket.
        server_hourly_24h = {"load_1m": [], "mem_used_pct": [], "disk_used_pct": []}
        server_daily_7d   = {"load_1m": [], "mem_used_pct": [], "disk_used_pct": []}
        server_daily_30d  = {"load_1m": [], "mem_used_pct": [], "disk_used_pct": []}
        server_daily_1y   = {"load_1m": [], "mem_used_pct": [], "disk_used_pct": []}
        if server_sampler is not None:
            try:
                from app import server_stats as _ss
                rows_24h = server_sampler.samples_since(int(since_24h.timestamp()))
                rows_7d  = server_sampler.samples_since(int(since_7d.timestamp()))
                rows_30d = server_sampler.samples_since(int(since_30d.timestamp()))
                rows_1y  = server_sampler.samples_since(int(since_1y.timestamp()))
                server_hourly_24h = _ss.averaged_buckets(rows_24h, now, window_hours=24)
                server_daily_7d   = _ss.averaged_buckets(rows_7d,  now, window_days=7)
                server_daily_30d  = _ss.averaged_buckets(rows_30d, now, window_days=30)
                server_daily_1y   = _ss.averaged_buckets(rows_1y,  now, window_days=365)
            except Exception:
                log.exception("server stats build failed")

        #Donations (Buy Me a Coffee). Empty unless BMAC_TOKEN is set
        #in the systemd env; see app/bmac_stats.py for the module.
        donations_configured = False
        donations_total = donations_24h = donations_7d = donations_30d = donations_1y = 0.0
        donations_daily_30d: list[dict] = []
        donations_daily_1y:  list[dict] = []
        try:
            from app import bmac_stats
            donations_configured = bmac_stats.is_configured()
            if donations_configured:
                bmac_rows = bmac_stats.fetch_all()
                donations_total = sum(d.amount for d in bmac_rows)
                donations_24h = sum(d.amount for d in bmac_rows if d.ts_unix >= int(since_24h.timestamp()))
                donations_7d  = sum(d.amount for d in bmac_rows if d.ts_unix >= int(since_7d.timestamp()))
                donations_30d = sum(d.amount for d in bmac_rows if d.ts_unix >= int(since_30d.timestamp()))
                donations_1y  = sum(d.amount for d in bmac_rows if d.ts_unix >= int(since_1y.timestamp()))
                donations_daily_30d = _bmac_daily_amounts(bmac_rows, now, 30)
                donations_daily_1y  = _bmac_daily_amounts(bmac_rows, now, 365)
            else:
                donations_daily_30d = _ts_daily_buckets([], now, 30)
                donations_daily_1y  = _ts_daily_buckets([], now, 365)
        except Exception:
            log.exception("donations histogram build failed")

        snapshot = StatsSnapshot(
            fetched_at_unix=now_unix,
            total_visits_24h=len(visits_24h),
            total_visits_7d=len(visits_7d),
            total_visits_30d=len(visits_30d),
            total_visits_1y =len(visits_1y),
            unique_visitors_24h=len({v.ip for v in visits_24h}),
            unique_visitors_7d =len({v.ip for v in visits_7d}),
            unique_visitors_30d=len({v.ip for v in visits_30d}),
            unique_visitors_1y =len({v.ip for v in visits_1y}),
            hourly_24h=_hourly_buckets(visits_24h, now, 24),
            hourly_7d =_hourly_buckets(visits_7d,  now, 24 * 7),
            daily_30d =_daily_buckets(visits_30d, now, 30),
            daily_1y  =_daily_buckets(visits_1y,  now, 365),
            browsers =_top_n(browsers,        n=10),
            operating_systems=_top_n(oses,    n=10),
            devices  =_top_n(devices,         n=4),
            conversions_total=conv_total,
            conversions_24h=conv_24h,
            conversions_7d =conv_7d,
            conversions_30d=conv_30d,
            conversions_1y =conv_1y,
            conversions_hourly_24h=conv_hourly_24h,
            conversions_hourly_7d =conv_hourly_7d,
            conversions_daily_30d =conv_daily_30d,
            conversions_daily_1y  =conv_daily_1y,
            downloads_total=dl_total,
            downloads_24h=dl_24h,
            downloads_7d =dl_7d,
            downloads_30d=dl_30d,
            downloads_1y =dl_1y,
            downloads_hourly_24h=dl_hourly_24h,
            downloads_hourly_7d =dl_hourly_7d,
            downloads_daily_30d =dl_daily_30d,
            downloads_daily_1y  =dl_daily_1y,
            donations_configured=donations_configured,
            donations_total_amount=round(donations_total, 2),
            donations_24h_amount =round(donations_24h, 2),
            donations_7d_amount  =round(donations_7d, 2),
            donations_30d_amount =round(donations_30d, 2),
            donations_1y_amount  =round(donations_1y, 2),
            donations_daily_30d  =donations_daily_30d,
            donations_daily_1y   =donations_daily_1y,
            server_hourly_24h=server_hourly_24h,
            server_daily_7d  =server_daily_7d,
            server_daily_30d =server_daily_30d,
            server_daily_1y  =server_daily_1y,
            downloads_pv_hourly_24h=dl_pv_24h,
            downloads_pv_hourly_7d =dl_pv_7d,
            downloads_pv_daily_30d =dl_pv_30d,
            downloads_pv_daily_1y  =dl_pv_1y,
            countries_table_24h=_country_table_rows(visits_24h, cached_geo),
            countries_table_7d =_country_table_rows(visits_7d,  cached_geo),
            countries_table_30d=_country_table_rows(visits_30d, cached_geo),
            countries_table_1y =_country_table_rows(visits_1y,  geo_1y),
            referrers_table_24h=_referrer_table_rows(visits_24h),
            referrers_table_7d =_referrer_table_rows(visits_7d),
            referrers_table_30d=_referrer_table_rows(visits_30d),
            referrers_table_1y =_referrer_table_rows(visits_1y),
            conversions_country_24h=conv_country_24h,
            conversions_country_7d =conv_country_7d,
            conversions_country_30d=conv_country_30d,
            conversions_country_1y =conv_country_1y,
            growth_index_1y=_growth_index_daily(visits_1y, conv_ts_1y, dl_daily_1y, now, window_days=365),
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
        "total_visits_1y": snap.total_visits_1y,
        "unique_visitors_1y": snap.unique_visitors_1y,
        "hourly_24h": snap.hourly_24h,
        "hourly_7d":  snap.hourly_7d,
        "daily_30d":  snap.daily_30d,
        "daily_1y":   snap.daily_1y,
        "browsers":   snap.browsers,
        "operating_systems": snap.operating_systems,
        "devices":    snap.devices,
        "conversions_total": snap.conversions_total,
        "conversions_24h":   snap.conversions_24h,
        "conversions_7d":    snap.conversions_7d,
        "conversions_30d":   snap.conversions_30d,
        "conversions_1y":    snap.conversions_1y,
        "conversions_hourly_24h": snap.conversions_hourly_24h,
        "conversions_hourly_7d":  snap.conversions_hourly_7d,
        "conversions_daily_30d":  snap.conversions_daily_30d,
        "conversions_daily_1y":   snap.conversions_daily_1y,
        "downloads_total": snap.downloads_total,
        "downloads_24h":   snap.downloads_24h,
        "downloads_7d":    snap.downloads_7d,
        "downloads_30d":   snap.downloads_30d,
        "downloads_1y":    snap.downloads_1y,
        "downloads_hourly_24h": snap.downloads_hourly_24h,
        "downloads_hourly_7d":  snap.downloads_hourly_7d,
        "downloads_daily_30d":  snap.downloads_daily_30d,
        "downloads_daily_1y":   snap.downloads_daily_1y,
        "donations_configured":    snap.donations_configured,
        "donations_total_amount":  snap.donations_total_amount,
        "donations_24h_amount":    snap.donations_24h_amount,
        "donations_7d_amount":     snap.donations_7d_amount,
        "donations_30d_amount":    snap.donations_30d_amount,
        "donations_1y_amount":     snap.donations_1y_amount,
        "donations_daily_30d":     snap.donations_daily_30d,
        "donations_daily_1y":      snap.donations_daily_1y,
        "server_hourly_24h": snap.server_hourly_24h,
        "server_daily_7d":   snap.server_daily_7d,
        "server_daily_30d":  snap.server_daily_30d,
        "server_daily_1y":   snap.server_daily_1y,
        "downloads_pv_hourly_24h": snap.downloads_pv_hourly_24h,
        "downloads_pv_hourly_7d":  snap.downloads_pv_hourly_7d,
        "downloads_pv_daily_30d":  snap.downloads_pv_daily_30d,
        "downloads_pv_daily_1y":   snap.downloads_pv_daily_1y,
        "countries_table_24h":     snap.countries_table_24h,
        "countries_table_7d":      snap.countries_table_7d,
        "countries_table_30d":     snap.countries_table_30d,
        "countries_table_1y":      snap.countries_table_1y,
        "referrers_table_24h":     snap.referrers_table_24h,
        "referrers_table_7d":      snap.referrers_table_7d,
        "referrers_table_30d":     snap.referrers_table_30d,
        "referrers_table_1y":      snap.referrers_table_1y,
        "conversions_country_24h": snap.conversions_country_24h,
        "conversions_country_7d":  snap.conversions_country_7d,
        "conversions_country_30d": snap.conversions_country_30d,
        "conversions_country_1y":  snap.conversions_country_1y,
        "growth_index_1y":         snap.growth_index_1y,
    }
