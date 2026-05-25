"""Buy Me a Coffee analytics, opt-in via the BMAC_TOKEN env var.

Without the token, every public function returns empty data so the
dashboard's "Donations" section renders an empty histogram + a
"connect your account" hint rather than blowing up.

With the token, we pull /v1/supporters + /v1/extras + /v1/subscriptions
from the BMaC API and aggregate into per-day amount buckets matching
the rest of the dashboard.

The BMaC token has full read access to your supporter list. Anyone
who can read the token can read every donor name + email + message
that has ever supported the account. Keep it scoped to the systemd
unit's environment + never log it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import NamedTuple

log = logging.getLogger(__name__)

ENV_TOKEN = "BMAC_TOKEN"
BMAC_API = "https://developers.buymeacoffee.com/api/v1"
PAGE_SIZE = 100
HTTP_TIMEOUT = 15
CACHE_TTL_SECONDS = 5 * 60


class Donation(NamedTuple):
    ts_unix: int
    amount: float
    currency: str
    kind: str  # "supporter" | "extra" | "subscription"


_cache: list[Donation] | None = None
_cache_at: float = 0.0
_lock = threading.Lock()


def _token() -> str | None:
    tok = os.environ.get(ENV_TOKEN, "").strip()
    return tok or None


def _http_get(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "helios-lidar/1.0 (+https://helios-lidar.org)",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _paginate(endpoint: str, token: str, kind: str) -> list[Donation]:
    out: list[Donation] = []
    url = f"{BMAC_API}/{endpoint}"
    while url:
        try:
            payload = _http_get(url, token)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            log.warning("BMaC %s fetch failed: %s", endpoint, exc)
            return out
        data = payload.get("data") or []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            #BMaC mixes date field names across endpoints.
            ts_raw = (
                entry.get("supporter_created_at")
                or entry.get("created_at")
                or entry.get("subscription_created_on")
            )
            if not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone(timezone.utc)
            except (TypeError, ValueError):
                continue
            #Amount fields: support_coffee_price * support_coffees, or
            #subscription_coffee_price * subscription_coffee_num, etc.
            amt = (
                entry.get("support_coffee_price")
                or entry.get("subscription_coffee_price")
                or entry.get("amount")
                or 0
            )
            try:
                amt = float(amt)
            except (TypeError, ValueError):
                amt = 0.0
            mult = (
                entry.get("support_coffees")
                or entry.get("subscription_coffee_num")
                or 1
            )
            try:
                mult = int(mult)
            except (TypeError, ValueError):
                mult = 1
            currency = entry.get("transfer_currency_type") or entry.get("subscription_currency") or "USD"
            total = amt * max(1, mult)
            if total <= 0:
                continue
            out.append(Donation(
                ts_unix=int(ts.timestamp()),
                amount=total,
                currency=str(currency),
                kind=kind,
            ))
        url = payload.get("next_page_url")
    return out


def fetch_all() -> list[Donation]:
    """Return every donation visible to the configured BMaC token,
    cached in-process for 5 minutes. Returns empty list when the
    token isn't configured or the API call fails on a cold cache.
    """
    global _cache, _cache_at
    with _lock:
        now = time.time()
        if _cache is not None and (now - _cache_at) < CACHE_TTL_SECONDS:
            return _cache
        token = _token()
        if not token:
            _cache = []
            _cache_at = now
            return _cache
        rows: list[Donation] = []
        for endpoint, kind in (("supporters", "supporter"),
                                ("extras", "extra"),
                                ("subscriptions", "subscription")):
            rows.extend(_paginate(endpoint, token, kind))
        rows.sort(key=lambda d: d.ts_unix)
        _cache = rows
        _cache_at = now
        return _cache


def is_configured() -> bool:
    return _token() is not None
