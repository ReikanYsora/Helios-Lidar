"""Background sampler for the Helios HACS submission PR position.

When you submit a custom plugin to HACS (the Home Assistant Community
Store), the PR lands at the back of a FIFO queue and waits to be
reviewed. Helios's PR is hacs/default#7520. This module polls the
GitHub Search API once per hour to count how many open PRs with the
"New default repository" label are OLDER than #7520, persists the
samples to a JSONL file, and exposes the full time-series for the
stats page chart.

Samples persist across worker restarts so the chart keeps continuity
even when the service has been OOM-killed + respawned. The file is
append-only, never re-written. Each line is one
`{"ts": <unix>, "ahead": <int>, "pr_state": <"open"|"merged"|...>}`
record; the consumer is the in-process snapshot loader which keeps
the parsed list cached.

Polling cadence: hourly. GitHub Search anonymous rate limit is
10 req/min, so a 1 req/h thread is comfortably inside the budget
even on a worker that bounces a few times.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger("helios-lidar")

#The PR being tracked. Hard-coded because there's exactly one: the
#Helios submission to HACS. Drop this whole module the day the PR
#gets merged.
TARGET_PR_NUMBER  = 7520
TARGET_PR_OWNER   = "hacs"
TARGET_PR_REPO    = "default"
TARGET_PR_LABEL   = "New default repository"
#PR creation timestamp (UTC). Captured once at submission, hard-coded
#here so the queue-ahead count doesn't need a second API call to
#discover it. If you ever resubmit, update this with the new PR's
#created_at.
TARGET_PR_CREATED_AT = "2026-05-08T23:18:31Z"

#Hourly samples. Hours align with the wall clock (we don't enforce
#exact alignment, just a target cadence). With one record per hour
#a one-year window holds 8 760 rows / ~1 MB of JSONL.
SAMPLE_INTERVAL_SECONDS = 60 * 60

#Hard ceiling on the response body. The GitHub search payload for
#one query is well under 100 KB.
MAX_RESPONSE_BYTES = 1024 * 1024
HTTP_TIMEOUT_SECONDS = 10


class Sample(NamedTuple):
    ts_unix:  int
    ahead:    int
    pr_state: str


class HacsPrSnapshot(NamedTuple):
    target_pr_number: int
    samples:          list[Sample]
    fetched_at_unix:  float


_cache_lock = threading.Lock()
_cache: HacsPrSnapshot | None = None
_log_path: Path | None = None


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
        raise ValueError("github response too large")
    return json.loads(raw.decode("utf-8"))


def _count_prs_ahead() -> int:
    """One Search API call. `created:<TARGET_PR_CREATED_AT` filters
    to PRs strictly older than ours, the open + label filters keep us
    on the same FIFO queue. Returns the total count GitHub reports.

    Query terms are separated by spaces in the GitHub Search syntax,
    `urllib.parse.urlencode` then turns the whole string into the
    URL-safe form (spaces → +, quotes → %22, < → %3C, etc.) so the
    Request doesn't choke.
    """
    q = (
        f"repo:{TARGET_PR_OWNER}/{TARGET_PR_REPO} "
        f"is:pr is:open "
        f'label:"{TARGET_PR_LABEL}" '
        f"created:<{TARGET_PR_CREATED_AT}"
    )
    params = urllib.parse.urlencode({"q": q, "per_page": "1"})
    url = f"https://api.github.com/search/issues?{params}"
    data = _http_get_json(url)
    if not isinstance(data, dict):
        raise ValueError("unexpected search payload")
    n = data.get("total_count")
    if not isinstance(n, int):
        raise ValueError("missing total_count")
    return n


def _fetch_pr_state() -> str:
    """Single PR-detail call, returns 'open' / 'closed' / 'merged'."""
    url = f"https://api.github.com/repos/{TARGET_PR_OWNER}/{TARGET_PR_REPO}/pulls/{TARGET_PR_NUMBER}"
    data = _http_get_json(url)
    if not isinstance(data, dict):
        return "unknown"
    if data.get("merged"):
        return "merged"
    state = data.get("state")
    return state if isinstance(state, str) else "unknown"


def _load_samples(path: Path) -> list[Sample]:
    """Read the on-disk JSONL, drop malformed lines silently."""
    samples: list[Sample] = []
    if not path.is_file():
        return samples
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    samples.append(Sample(
                        ts_unix=int(row["ts"]),
                        ahead=int(row["ahead"]),
                        pr_state=str(row.get("pr_state", "unknown")),
                    ))
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        return samples
    samples.sort(key=lambda s: s.ts_unix)
    return samples


def _append_sample(path: Path, sample: Sample) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts":       sample.ts_unix,
            "ahead":    sample.ahead,
            "pr_state": sample.pr_state,
        }) + "\n")


def _sampler_loop(path: Path) -> None:
    """Daemon thread. Once on startup (so a worker restart records a
    fresh sample even if the timer hasn't fired yet), then every
    SAMPLE_INTERVAL_SECONDS. Best-effort: a network failure logs a
    warning and the loop continues, no crash, no exponential backoff
    needed because the next attempt is already an hour away.
    """
    global _cache
    try:
        ahead = _count_prs_ahead()
        state = _fetch_pr_state()
        sample = Sample(ts_unix=int(time.time()), ahead=ahead, pr_state=state)
        _append_sample(path, sample)
        with _cache_lock:
            if _cache is not None:
                _cache.samples.append(sample)
                _cache = HacsPrSnapshot(
                    target_pr_number=TARGET_PR_NUMBER,
                    samples=_cache.samples,
                    fetched_at_unix=time.time(),
                )
        log.info("hacs_pr_status: startup sample, ahead=%d state=%s", ahead, state)
    except (urllib.error.URLError, ValueError) as exc:
        log.warning("hacs_pr_status: startup sample failed: %s", exc)

    while True:
        time.sleep(SAMPLE_INTERVAL_SECONDS)
        try:
            ahead = _count_prs_ahead()
            state = _fetch_pr_state()
            sample = Sample(ts_unix=int(time.time()), ahead=ahead, pr_state=state)
            _append_sample(path, sample)
            with _cache_lock:
                if _cache is not None:
                    _cache.samples.append(sample)
                    _cache = HacsPrSnapshot(
                        target_pr_number=TARGET_PR_NUMBER,
                        samples=_cache.samples,
                        fetched_at_unix=time.time(),
                    )
        except Exception as exc:  # noqa: BLE001
            log.warning("hacs_pr_status: hourly sample failed: %s", exc)


def start_sampler(jsonl_path: Path) -> None:
    """Wire the on-disk path and spawn the daemon. Idempotent: a
    second call is a no-op so the import-side init code can fire
    safely from anywhere."""
    global _log_path, _cache
    if _log_path is not None:
        return
    _log_path = jsonl_path
    with _cache_lock:
        _cache = HacsPrSnapshot(
            target_pr_number=TARGET_PR_NUMBER,
            samples=_load_samples(jsonl_path),
            fetched_at_unix=time.time(),
        )
    t = threading.Thread(
        target=_sampler_loop,
        args=(jsonl_path,),
        name="hacs-pr-sampler",
        daemon=True,
    )
    t.start()


def get_snapshot() -> HacsPrSnapshot:
    """Return the current in-process snapshot. The sampler keeps it
    refreshed each hour; consumers read it at any cadence."""
    with _cache_lock:
        if _cache is None:
            return HacsPrSnapshot(
                target_pr_number=TARGET_PR_NUMBER,
                samples=[],
                fetched_at_unix=time.time(),
            )
        return _cache
