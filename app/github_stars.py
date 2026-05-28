"""Server-side fetch + cache of GitHub star history for the Helios
projects. Surfaced on the stats page as a two-line chart so the
maintainer can watch organic interest grow alongside downloads and
conversions.

The fetch path:

  1. GET https://api.github.com/repos/{owner}/{repo}/stargazers
     with `Accept: application/vnd.github.v3.star+json`, which makes
     GitHub return each star as `{"starred_at": "ISO8601", "user": {...}}`
     instead of just the user object.
  2. Paginate (per_page=100, follow Link: rel="next") until the
     repo is exhausted. With a couple of thousand stars total this
     is one or two requests per repo per refresh.
  3. Keep only the `starred_at` epochs, sorted ascending.

Two repos are tracked: ReikanYsora/Helios (the card) and
ReikanYsora/Helios-Lidar (this companion site). Both cached
together because the refresh is cheap and the chart renders them
side-by-side.

Cache TTL: one hour. GitHub anonymous rate limit is 60 req/h per IP;
two repos × ~ N pages per refresh stays well under the cap even if
the cache flaps repeatedly. On any GitHub error we serve the last
known good payload; on a cold-start failure we surface an empty
snapshot the stats endpoint treats as "stat unavailable".

NB: the stargazers endpoint returns paginated star timestamps for
PUBLIC repos only, no authentication needed. Once a repo crosses
~ 40 000 stars the API caps at 400 pages (40 000 stars worth) and
deeper history is truncated. We are nowhere near that limit, the
issue is documented here for the future.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import NamedTuple

log = logging.getLogger("helios-lidar")

#Repos tracked, ordered. The label is what shows up in the chart
#legend; keep it short, the legend sits in a tight header strip.
TRACKED_REPOS: list[tuple[str, str, str]] = [
    ("ReikanYsora", "Helios",       "Helios (the card)"),
    ("ReikanYsora", "Helios-Lidar", "Helios-Lidar (the site)"),
]

#One-hour TTL. Same trade-off as helios_downloads: short enough to
#feel near-real-time on the stats page, long enough that two
#concurrent refreshes can't blow through the 60 req/h anonymous
#GitHub quota even under repeated cache flaps.
CACHE_TTL_SECONDS = 60 * 60

#Hard ceiling on response size per page. A page of 100 stars with
#full user metadata is well under 200 KB in practice.
MAX_RESPONSE_BYTES = 1024 * 1024
HTTP_TIMEOUT_SECONDS = 10

#GitHub caps stargazers pagination at 400 pages = 40 000 stars. We
#stop earlier as a safety net so a runaway redirect or an unexpected
#schema change can't pin the worker indefinitely.
MAX_PAGES = 500

_RE_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


class RepoStarsSnapshot(NamedTuple):
    owner: str
    repo:  str
    label: str
    #Unix epochs (UTC) of every star, ascending.
    starred_at_unix: list[int]


class StarsSnapshot(NamedTuple):
    repos: list[RepoStarsSnapshot]
    fetched_at_unix: float


_cache: StarsSnapshot | None = None
_lock = threading.Lock()


def _http_get(url: str) -> tuple[bytes, dict]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github.v3.star+json",
            "User-Agent": "helios-lidar/1.0 (+https://helios-lidar.org)",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
        headers = {k.lower(): v for k, v in resp.headers.items()}
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError(f"GitHub response too large at {url}")
    return raw, headers


def _next_page_url(link_header: str | None) -> str | None:
    """Parse the GitHub Link header and return the URL for rel="next",
    or None when we're on the last page (no next link)."""
    if not link_header:
        return None
    m = _RE_NEXT_LINK.search(link_header)
    return m.group(1) if m else None


def _fetch_repo_stars(owner: str, repo: str, label: str) -> RepoStarsSnapshot:
    """Paginate through every star, collect the starred_at timestamps."""
    url: str | None = (
        f"https://api.github.com/repos/{owner}/{repo}/stargazers"
        "?per_page=100"
    )
    timestamps: list[int] = []
    pages = 0
    while url and pages < MAX_PAGES:
        try:
            raw, headers = _http_get(url)
        except (urllib.error.URLError, ValueError) as e:
            log.warning("github_stars: fetch failed at %s: %s", url, e)
            break
        try:
            page = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.warning("github_stars: parse failed at %s: %s", url, e)
            break
        if not isinstance(page, list):
            log.warning("github_stars: unexpected payload shape at %s", url)
            break
        for item in page:
            sa = item.get("starred_at") if isinstance(item, dict) else None
            if not isinstance(sa, str):
                continue
            try:
                #GitHub returns "2025-06-04T18:23:11Z", fromisoformat
                #handles it in 3.11+, fall back to manual parse on older.
                ts = datetime.fromisoformat(sa.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            timestamps.append(int(ts))
        url = _next_page_url(headers.get("link"))
        pages += 1
    timestamps.sort()
    return RepoStarsSnapshot(
        owner=owner,
        repo=repo,
        label=label,
        starred_at_unix=timestamps,
    )


def _refresh_blocking() -> StarsSnapshot:
    repos = [_fetch_repo_stars(o, r, lbl) for (o, r, lbl) in TRACKED_REPOS]
    return StarsSnapshot(repos=repos, fetched_at_unix=time.time())


def get_stars_snapshot() -> StarsSnapshot:
    """Returns the cached stars snapshot, refreshing it when stale."""
    global _cache
    with _lock:
        now = time.time()
        if _cache is not None and (now - _cache.fetched_at_unix) < CACHE_TTL_SECONDS:
            return _cache
        try:
            fresh = _refresh_blocking()
        except Exception as e:
            log.warning("github_stars: refresh failed, keeping last cache: %s", e)
            if _cache is not None:
                return _cache
            #Cold start, no cache yet, return an empty snapshot so
            #the stats endpoint still answers cleanly. Subsequent
            #refreshes will retry.
            return StarsSnapshot(
                repos=[
                    RepoStarsSnapshot(o, r, lbl, [])
                    for (o, r, lbl) in TRACKED_REPOS
                ],
                fetched_at_unix=now,
            )
        _cache = fresh
        return fresh
