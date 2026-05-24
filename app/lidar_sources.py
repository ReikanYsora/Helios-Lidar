"""Expose the community-maintained `LIDAR_SOURCES.md` at the repo
root through the upload page's "Where do I download LiDAR data"
section.

The markdown file is the source of truth: contributors add new
countries by editing it and opening a pull request. The upload page
fetches the raw markdown on load via `GET /api/lidar-sources` and
displays it inside a code-block frame, so visitors immediately see
that what they're reading is the literal file from the repo (links
to portals stay readable as plain URLs).

The file is small (~1.5 KB) so we re-read on every request rather
than cache: keeps the contract trivial and the latency negligible.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("helios-lidar")

SOURCES_PATH = Path(__file__).resolve().parent.parent / "LIDAR_SOURCES.md"


def render_text() -> str:
    """Return the raw LIDAR_SOURCES.md text, or a placeholder if the
    file is unexpectedly missing on disk.
    """
    try:
        return SOURCES_PATH.read_text(encoding="utf-8")
    except OSError:
        log.exception("LIDAR_SOURCES.md is unreadable, returning empty placeholder")
        return "The data sources list is temporarily unavailable."
