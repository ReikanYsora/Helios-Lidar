"""CORS-relay for upstream LiDAR endpoints the Helios card cannot
reach directly from a browser.

Why this exists: some national LiDAR providers serve their data over
public WCS endpoints that return correct payloads but no
`Access-Control-Allow-Origin` header. A browser fetch then rejects
the response even though the server happily delivered the bytes.
GUGiK (Poland) is the first case we hit, more will follow as we add
providers.

This module exposes a single GET endpoint, `/api/lidar-proxy`, that
takes the full upstream URL as a query parameter, validates the
hostname against a server-side allowlist (so we are not an open
proxy), refetches the bytes over plain HTTP from this VPS and
relays them back to the browser with `Access-Control-Allow-Origin: *`
courtesy of the FastAPI CORSMiddleware that already wraps every
endpoint in main.py.

Adding a new provider is one allowlist entry away. No per-provider
parsing, no per-provider URL templating. The card knows its own
upstream URL, the server just trusts it after the hostname check.
"""

from __future__ import annotations

import logging
import socket
from urllib.parse import urlparse

import urllib.error
import urllib.request

from fastapi import HTTPException
from fastapi.responses import Response

log = logging.getLogger("helios-lidar")

#Hostnames the proxy is allowed to relay to. Anything else returns
#403 so a third party cannot use helios-lidar.org as an open HTTP
#fetcher. Add new entries here as we onboard providers that need the
#relay.
ALLOWED_UPSTREAM_HOSTS: frozenset[str] = frozenset({
    "mapy.geoportal.gov.pl",  # Poland, GUGiK NMPT (DSM_PL-EVRF2007-NH)
})

#Hard ceiling on what we forward to the browser. A 1024 x 1024
#Float32 GeoTIFF is ~4 MB; we leave generous headroom for the
#largest raster the pipeline could ever request and still cap below
#anything that would be abusive to relay through our bandwidth.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

#Connection + read timeout for the upstream fetch. Polish geoportal
#typically answers in under a second; 15 s is the worst case we
#tolerate before declaring the upstream dead and surfacing a 504
#to the card so its backoff kicks in.
UPSTREAM_TIMEOUT_SECONDS = 15

#User-Agent we identify as. Some national geoportals reject empty or
#python-urllib UAs out of habit; a real-looking string keeps us off
#the dumb-blocklist tier without pretending to be Chrome.
USER_AGENT = "helios-lidar/1.0 (+https://helios-lidar.org)"


def fetch_upstream(upstream_url: str) -> Response:
    """Validate the upstream hostname, fetch the URL server-side,
    return a FastAPI Response that streams the bytes back with the
    upstream Content-Type preserved. Raises HTTPException with a
    meaningful status code on any failure path so the card can
    distinguish "we are blocked at the proxy layer" from "the
    upstream itself is down".
    """
    if not upstream_url:
        raise HTTPException(status_code=400, detail="missing upstream URL")

    try:
        parsed = urlparse(upstream_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"malformed upstream URL: {exc}") from exc

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="upstream scheme must be http or https")

    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_UPSTREAM_HOSTS:
        log.warning("lidar_proxy: rejected upstream host %r (not in allowlist)", host)
        raise HTTPException(status_code=403, detail=f"upstream host not allowed: {host}")

    req = urllib.request.Request(
        upstream_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept":     "image/tiff, application/octet-stream, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_SECONDS) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            content_type = resp.headers.get("Content-Type", "application/octet-stream")
    except urllib.error.HTTPError as exc:
        log.warning("lidar_proxy: upstream %s returned HTTP %d", host, exc.code)
        raise HTTPException(status_code=502, detail=f"upstream returned {exc.code}") from exc
    except (TimeoutError, socket.timeout) as exc:
        #Python 3.10+ raises the builtin TimeoutError on socket read
        #timeout instead of wrapping it inside urllib.error.URLError,
        #so the catch below misses it and the request bubbles up as a
        #500 with the full Starlette stack in the journal. Surface a
        #clean 504 so the card's backoff treats the timeout the same
        #as any other upstream-unreachable case.
        log.warning("lidar_proxy: upstream %s read timed out after %ds", host, UPSTREAM_TIMEOUT_SECONDS)
        raise HTTPException(status_code=504, detail="upstream timed out") from exc
    except urllib.error.URLError as exc:
        log.warning("lidar_proxy: upstream %s unreachable: %s", host, exc)
        raise HTTPException(status_code=504, detail="upstream unreachable") from exc

    if len(raw) > MAX_RESPONSE_BYTES:
        log.warning("lidar_proxy: upstream %s payload exceeds %d bytes", host, MAX_RESPONSE_BYTES)
        raise HTTPException(status_code=502, detail="upstream payload too large")

    return Response(
        content=raw,
        media_type=content_type,
        headers={
            #Short cache on the browser side, the same home position fetches the same upstream bytes for the lifetime of a session.
            #Server-side caching could come later if bandwidth becomes a concern.
            "Cache-Control": "public, max-age=3600",
        },
    )
