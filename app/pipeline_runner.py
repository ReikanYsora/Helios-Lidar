"""Subprocess runner for the LAZ -> nDSM pipeline.

Spawned by main.py as a fresh `python -m app.pipeline_runner <job_id>`
process so the heavy point-cloud allocations live and die inside a
child process. When the child exits, the kernel reclaims every byte
the pipeline ever touched, the parent worker stays near its baseline
80 MB and the next job starts on a clean slate.

Without this isolation, FastAPI's BackgroundTask scheduler ran the
pipeline inside the same uvicorn worker. A 50M-point LAZ peaks at
~ 3 GB of RSS; even after the pipeline returns and Python frees its
local references, glibc rarely hands the freed pages back to the
kernel, so the worker stayed permanently above the high-water mark.
A few conversions in a row pushed the worker into swap and the
service stopped answering requests.

This child is a thin wrapper around the existing `rasterise` call.
Progress is published through the same `Job.save()` path the in-
process version used, so the polling endpoint at /jobs/{id} sees
the same status JSON without any change.

Exit codes have meaning so the parent can produce a useful FAILED
status message:

    0  - success, ndsm.tif written
    2  - bad CLI usage
    3  - job id unknown (status.json missing)
    4  - input.la? missing on disk
    10 - upstream pipeline raised ValidationError
    11 - any other pipeline exception
"""

from __future__ import annotations

import sys
import time

from app.config import settings
from app.jobs import Job
from pipeline import laz_to_ndsm
from pipeline.validate import ValidationError


PHASE_BAND = {
    "reading":      (25, 33),
    "reprojecting": (33, 40),
    "kdtree":       (40, 43),
    "querying":     (43, 70),
    "rasterising":  (70, 73),
    "writing":      (73, 75),
}
PHASE_MSG = {
    "reading":      "Reading points",
    "reprojecting": "Reprojecting to metres",
    "kdtree":       "Building ground KDTree",
    "querying":     "Computing height-above-ground",
    "rasterising":  "Building the nDSM raster",
    "writing":      "Writing nDSM to disk",
}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m app.pipeline_runner <job_id>", file=sys.stderr)
        return 2

    job_id = sys.argv[1]
    job = Job.load(job_id)
    if job is None:
        print(f"job not found: {job_id}", file=sys.stderr)
        return 3

    job_dir = job.dir()
    laz_candidates = list(job_dir.glob("input.la?"))
    if not laz_candidates:
        print("input.la? missing on disk", file=sys.stderr)
        return 4
    laz_path = laz_candidates[0]
    ndsm_path = job_dir / "ndsm.tif"

    last_save = [0.0]

    def on_progress(phase: str, fraction: float) -> None:
        lo, hi = PHASE_BAND.get(phase, (25, 75))
        pct = lo + (hi - lo) * max(0.0, min(1.0, fraction))
        job.progress_pct = pct
        job.progress_message = PHASE_MSG.get(phase, "Processing")
        now = time.monotonic()
        #Throttle to ~ 1 status.json write per second so the polling
        #endpoint sees movement without us hammering the disk during
        #the hot inner loops of the pipeline.
        if now - last_save[0] >= 0.8 or fraction >= 1.0:
            job.save()
            last_save[0] = now

    try:
        laz_to_ndsm.rasterise(
            laz_path,
            ndsm_path,
            pixel_meters=settings.raster_pixel_meters,
            on_progress=on_progress,
        )
    except ValidationError as exc:
        print(f"VALIDATION_ERROR: {exc}", file=sys.stderr)
        return 10
    except Exception as exc:  # noqa: BLE001
        #Catch-all so the parent always sees a controlled exit code
        #instead of a Python-level exception leaking through stderr
        #only. The repr keeps both the type and the message.
        print(f"PIPELINE_ERROR: {exc!r}", file=sys.stderr)
        return 11

    return 0


if __name__ == "__main__":
    sys.exit(main())
