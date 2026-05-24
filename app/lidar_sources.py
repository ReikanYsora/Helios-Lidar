"""Render the community-maintained `LIDAR_SOURCES.md` at the repo
root into HTML for the upload page's "Where do I download LiDAR
data" section.

The markdown file is the source of truth: contributors add new
countries by editing it and opening a pull request. The upload
page fetches the rendered HTML on load via `GET /api/lidar-sources`
and injects it inside a bordered scrollable frame, so visitors
read a polished bullet list AND see at a glance that it's "this
file" being mirrored.

The file is small (~1.5 KB) so we re-render on every request rather
than cache: keeps the contract trivial and the latency negligible.
"""

from __future__ import annotations

import logging
from pathlib import Path

import markdown

log = logging.getLogger("helios-lidar")

SOURCES_PATH = Path(__file__).resolve().parent.parent / "LIDAR_SOURCES.md"


def render_html() -> str:
    """Return the rendered LIDAR_SOURCES.md as HTML, or a placeholder
    if the file is unexpectedly missing on disk.
    """
    try:
        text = SOURCES_PATH.read_text(encoding="utf-8")
    except OSError:
        log.exception("LIDAR_SOURCES.md is unreadable, returning empty placeholder")
        return "<p>The data sources list is temporarily unavailable.</p>"

    return markdown.markdown(
        text,
        extensions=["fenced_code", "tables", "sane_lists"],
        output_format="html5",
    )
