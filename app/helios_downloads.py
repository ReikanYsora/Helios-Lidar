"""Server-side proxy for the Helios card's GitHub release download
counts. Replaces the previous anonymous heartbeat path: instead of
asking each user's browser to ping us, we ask GitHub how many times
the `helios.js` bundle has been downloaded per release and surface
the totals on the landing page.

The fetch path:

  1. GET https://api.github.com/repos/ReikanYsora/Helios/releases
     (one page is enough; the project has well under 100 releases
     and we explicitly only want the non-prerelease, non-draft set)
  2. Sum `assets[].download_count` per release, ignore drafts +
     prereleases (beta tags would pollute the user-facing counter)
  3. Sort by tag descending so the freshest release is index 0

We cache the result in-process for an hour to stay well under the
60 req/h anonymous GitHub quota even if many visitors hit the page
in the same window. On any GitHub error we serve the last known
good payload; on a cold-start failure we surface a small placeholder
that the landing page treats as "stat unavailable".
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import NamedTuple

log = logging.getLogger("helios-lidar")

GITHUB_OWNER = "ReikanYsora"
GITHUB_REPO = "Helios"

#Five minutes TTL. Short enough that a new download shows up on
#the page near-real-time, long enough that the worker fires at
#most 12 GitHub calls per hour, well below the 60 req/h anonymous
#quota even if every cached fetch happens to expire under load.
CACHE_TTL_SECONDS = 5 * 60

#Hard cap on the response size so a hostile redirect or malformed
#response can't pin the worker. A page of 100 releases with full
#asset metadata is well under 200 KB in practice.
MAX_RESPONSE_BYTES = 1024 * 1024

HTTP_TIMEOUT_SECONDS = 10

#One-time correction baselines added on top of whatever GitHub
#reports for a given tag. Lets us preserve a historical download
#total when the release-asset workflow had to re-upload the bundle
#and GitHub reset that asset's download_count to zero (an asset
#counter is reset on every delete + re-upload, with no API to seed
#it back). Each entry is a "+N" baseline: the real GitHub count is
#summed on top so a tag that keeps getting downloaded after the
#correction still increments past the baseline naturally.
MANUAL_BASELINES: dict[str, int] = {
    #v1.6.3 had 151 downloads before the bundle was re-uploaded as
    #part of a same-tag content refresh; counter reset to 0 then.
    "v1.6.3": 151,
}


class ReleaseDownload(NamedTuple):
    tag: str
    downloads: int


class HeliosDownloadsSnapshot(NamedTuple):
    latest_tag: str | None
    latest_downloads: int
    total_downloads: int
    #Newest release first. Drafts + prereleases are filtered out.
    by_version: list[ReleaseDownload]
    fetched_at_unix: float


_cache: HeliosDownloadsSnapshot | None = None
_lock = threading.Lock()


def _http_get_json(url: str) -> object:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "helios-lidar/1.0 (+https://helios-lidar.org)",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError(f"Response from {url} exceeds {MAX_RESPONSE_BYTES} bytes")
    return json.loads(raw.decode("utf-8", errors="replace"))


def _fetch_fresh() -> HeliosDownloadsSnapshot:
    payload = _http_get_json(
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases?per_page=100"
    )
    if not isinstance(payload, list):
        raise ValueError("GitHub releases endpoint returned a non-list payload")

    rows: list[ReleaseDownload] = []
    for release in payload:
        if not isinstance(release, dict):
            continue
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag:
            continue
        assets = release.get("assets")
        if not isinstance(assets, list):
            continue
        downloads = 0
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            count = asset.get("download_count")
            if isinstance(count, int) and count >= 0:
                downloads += count
        downloads += MANUAL_BASELINES.get(tag, 0)
        rows.append(ReleaseDownload(tag=tag, downloads=downloads))

    #GitHub returns releases in published-at descending order already,
    #but we don't trust that contract: sort by a parsed version key
    #so v1.10 sits above v1.9 instead of below.
    rows.sort(key=_version_sort_key, reverse=True)

    latest = rows[0] if rows else None
    return HeliosDownloadsSnapshot(
        latest_tag=latest.tag if latest else None,
        latest_downloads=latest.downloads if latest else 0,
        total_downloads=sum(r.downloads for r in rows),
        by_version=rows,
        fetched_at_unix=time.time(),
    )


def _version_sort_key(row: ReleaseDownload) -> tuple[int, ...]:
    """Parse a `vX.Y.Z[-suffix]` tag into a tuple suitable for sort.
    Falls back to (0,) for anything that doesn't match the shape so
    junk tags sink to the bottom without raising.
    """
    tag = row.tag.lstrip("vV")
    head = tag.split("-", 1)[0]
    parts: list[int] = []
    for chunk in head.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            return (0,)
    return tuple(parts) if parts else (0,)


def get_downloads_snapshot() -> HeliosDownloadsSnapshot | None:
    """Return the cached snapshot if fresh, otherwise refresh in-place.

    Returns the stale cache on fetch errors. Returns None only if we
    have never successfully fetched the data in this process.
    """
    global _cache
    with _lock:
        now = time.time()
        if _cache is not None and (now - _cache.fetched_at_unix) < CACHE_TTL_SECONDS:
            return _cache
        try:
            _cache = _fetch_fresh()
            return _cache
        except (urllib.error.URLError, ValueError, json.JSONDecodeError, TimeoutError):
            log.exception("Helios downloads fetch failed; serving stale cache if any")
            return _cache
