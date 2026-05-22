"""FastAPI entry point for Helios-Lidar.

Exposes:

* `GET /healthz` , liveness probe.
* `POST /jobs` , accept either a DSM + DTM raster pair OR a single
  LAS / LAZ point cloud, kick off processing.
* `GET /jobs/{job_id}` , status JSON for the upload UI to poll.
* `GET /` , minimal API metadata; nginx serves the real frontend
  out of `/var/helios-lidar/frontend/` in production, the route
  below is a sane dev-mode fallback.

The actual conversion lives in the `pipeline/` package; this module
is transport, validation of HTTP inputs, and bookkeeping only.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from pydantic import BaseModel, Field

from app import helios_readme, jobs as job_store, lidar_sources
from app.config import settings
from app.jobs import Job, JobStatus
from app.stats import ACTIVE_WINDOW_DAYS, StatsStore
from pipeline import cog as cog_mod
from pipeline import dsm_to_ndsm, laz_to_ndsm, yaml_snippet
from pipeline.validate import ValidationError, inspect, validate_pair

log = logging.getLogger("helios-lidar")

#How long the generated COG stays on the VPS after a job finishes.
#The frontend auto-triggers a browser download on job done, so the
#user's local copy is in place within seconds; we keep the VPS copy
#around for a short window to absorb a slow connection / a manual
#right-click "Save As", then delete it so the disk doesn't fill up
#with per-user output files. 10 minutes = a 5-minute download window
#plus 5 minutes of slack for a paused / rate-limited download. Users
#who need to keep the COG host it themselves under HA's
#config/www/helios/ (the YAML snippet points at exactly that path).
COG_TTL_SECONDS: int = 10 * 60

app = FastAPI(
    title="Helios-Lidar",
    description=(
        "Web pipeline that turns user-uploaded LiDAR data into nDSM "
        "Cloud-Optimized GeoTIFFs ready to consume by the Helios Home "
        "Assistant card."
    ),
    #Locked to the Helios card version so the two projects ship in
    #lock-step; bump both at once when releasing a paired feature.
    version="1.6.3",
)

#CORS for the public /api endpoints. The Helios card lives on the
#user's Home Assistant origin (anything from homeassistant.local
#to a public cloudflare tunnel domain), so the heartbeat would
#otherwise be blocked by the browser's same-origin policy. Allow
#any origin since the endpoints are GET (counts) or POST (single
#anonymous UUID, no auth, no cookies). nginx adds the rate-limit
#layer on top.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["content-type"],
    allow_credentials=False,
    max_age=86400,
)


#Frontend directory: rsync target on the VPS is /var/helios-lidar/frontend,
#but FastAPI runs from /var/helios-lidar/app, so resolve the sibling
#`frontend/` next to the `app/` package. Same layout in dev.
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

#Anonymous-stats store. Lives next to the existing jobs / output
#directories; persists across deploys because it's outside the
#git checkout. Single shared instance because FastAPI runs as one
#uvicorn worker and StatsStore serialises its own writes.
_stats = StatsStore(settings.jobs_dir.parent / "stats" / "stats.db")


class HeartbeatPayload(BaseModel):
    install_id: str = Field(min_length=36, max_length=36)


@app.post("/api/heartbeat", status_code=204)
def heartbeat(payload: HeartbeatPayload) -> JSONResponse:
    """Record one anonymous install heartbeat from a Helios card.

    Body : `{ "install_id": "<uuidv4>" }`. The UUID is generated +
    persisted in the card's localStorage; the card pings here at
    most once per browser per 24 h. The endpoint validates the
    UUIDv4 shape inside StatsStore.record_install() and silently
    discards any payload that doesn't match.

    Never returns a body. nginx adds a 10 req/min/IP rate-limit
    on top of this route to absorb spikes.
    """
    _stats.record_install(payload.install_id)
    return JSONResponse(status_code=204, content=None)


@app.get("/api/install-count")
def install_count() -> JSONResponse:
    """Number of distinct Helios installs that pinged in the last
    ACTIVE_WINDOW_DAYS days. Consumed by the helios-lidar.org
    landing page so it can show "Join the N users running Helios".
    """
    n = _stats.active_installs()
    return JSONResponse(
        content={"count": n, "window_days": ACTIVE_WINDOW_DAYS},
        headers={"cache-control": "public, max-age=300"},
    )


@app.get("/api/conversions-count")
def conversions_count() -> JSONResponse:
    """All-time count of successful pipeline conversions (jobs that
    reached JobStatus.DONE). Same landing page consumes it for
    "Already N COGs converted through the pipeline".
    """
    n = _stats.total_conversions()
    return JSONResponse(
        content={"count": n},
        headers={"cache-control": "public, max-age=300"},
    )


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Liveness probe."""
    return JSONResponse({"status": "ok", "version": app.version})


@app.get("/")
def index() -> JSONResponse:
    """Dev-mode landing; nginx serves the real frontend in production."""
    return JSONResponse(
        {
            "service": "helios-lidar",
            "version": app.version,
            "docs": f"{settings.public_base_url}/docs",
            "frontend": f"{settings.public_base_url}/",
        }
    )


@app.get("/helios-card")
def helios_card_page() -> FileResponse:
    """Serve the static Helios-card landing page. The README content
    itself is injected by the page's JS on load via
    `GET /api/helios-readme`.
    """
    page = FRONTEND_DIR / "helios-card.html"
    return FileResponse(page, media_type="text/html; charset=utf-8")


@app.get("/api/lidar-sources")
def api_lidar_sources() -> JSONResponse:
    """Return the rendered LIDAR_SOURCES.md as HTML. Source of truth
    is the markdown file at the repo root, community-maintained via
    pull requests.
    """
    return JSONResponse({"html": lidar_sources.render_html()})


@app.get("/api/helios-readme")
def api_helios_readme() -> JSONResponse:
    """Return the rendered Helios card README + the release tag it
    came from. Cached in-process for an hour, see `helios_readme`.
    """
    rendered = helios_readme.get_rendered_readme()
    if rendered is None:
        return JSONResponse(
            {
                "html": None,
                "release_tag": None,
                "release_url": (
                    f"https://github.com/{helios_readme.GITHUB_OWNER}/"
                    f"{helios_readme.GITHUB_REPO}"
                ),
                "error": "Helios README is not available yet, retry shortly.",
            },
            status_code=503,
        )
    return JSONResponse(
        {
            "html": rendered.html,
            "release_tag": rendered.release_tag,
            "release_url": rendered.release_url,
        }
    )


@app.post("/jobs")
async def create_job(
    background: BackgroundTasks,
    dsm: UploadFile | None = File(None, description="Digital Surface Model GeoTIFF (paired with `dtm`)"),
    dtm: UploadFile | None = File(None, description="Digital Terrain Model GeoTIFF (paired with `dsm`)"),
    laz: UploadFile | None = File(None, description="LAS / LAZ point cloud (single-file workflow)"),
) -> JSONResponse:
    """Accept one of:

    * a DSM + DTM raster pair (per-pixel subtraction → nDSM → COG)
    * a single LAS / LAZ point cloud (per-point height-above-ground
      → max per cell → COG)

    Returns the new job id immediately; processing is dispatched to
    a background task and the UI polls `GET /jobs/{job_id}`.
    """
    raster_mode = dsm is not None and dtm is not None
    laz_mode = laz is not None

    if raster_mode and laz_mode:
        raise HTTPException(
            status_code=400,
            detail="Pick one workflow: either DSM + DTM rasters OR a single LAS / LAZ file, not both.",
        )
    if not raster_mode and not laz_mode:
        raise HTTPException(
            status_code=400,
            detail="Upload either a DSM + DTM raster pair (`dsm` + `dtm` fields) or a single LAS / LAZ file (`laz` field).",
        )

    job = job_store.new()
    job_dir = job.dir()

    try:
        if raster_mode:
            assert dsm is not None and dtm is not None
            await _stream_upload(dsm, job_dir / "dsm.tif")
            await _stream_upload(dtm, job_dir / "dtm.tif")
            job.input_mode = "raster_pair"
        else:
            assert laz is not None
            suffix = ".laz" if (laz.filename or "").lower().endswith(".laz") else ".las"
            await _stream_upload(laz, job_dir / f"input{suffix}")
            job.input_mode = "point_cloud"
        job.save()
    except Exception:
        job.status = JobStatus.FAILED
        job.error = "Upload failed before processing started."
        job.save()
        raise

    background.add_task(_process, job.job_id)

    return JSONResponse(
        {
            "job_id": job.job_id,
            "status": job.status.value,
            "input_mode": job.input_mode,
            "poll_url": f"/jobs/{job.job_id}",
        },
        status_code=202,
    )


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    """Return the JSON status of a previously-created job."""
    job = Job.load(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse(job.to_dict())


async def _stream_upload(upload: UploadFile, target: Path) -> None:
    """Stream a multipart upload to `target`, closing the source
    handle even if the copy raises.
    """
    try:
        with target.open("wb") as fh:
            shutil.copyfileobj(upload.file, fh)
    finally:
        await upload.close()


def _process(job_id: str) -> None:
    """Background task: dispatch to the raster or LAZ pipeline,
    then COG-ify and publish.

    Runs synchronously inside FastAPI's BackgroundTasks executor.
    Any exception flips the job to FAILED with the message exposed
    in the status JSON so the UI can show it directly.
    """
    job = Job.load(job_id)
    if job is None:
        log.warning("process called for missing job %s", job_id)
        return

    try:
        job_dir = job.dir()
        ndsm_path = job_dir / "ndsm.tif"

        if job.input_mode == "raster_pair":
            dsm_path = job_dir / "dsm.tif"
            dtm_path = job_dir / "dtm.tif"

            job.status = JobStatus.VALIDATING
            job.progress_message = "Inspecting DSM and DTM"
            job.progress_pct = 10
            job.save()
            dsm_meta, _ = validate_pair(dsm_path, dtm_path)

            job.bounds_wgs84 = dsm_meta.bounds_wgs84
            job.pixel_size_meters = round((dsm_meta.pixel_size_x + dsm_meta.pixel_size_y) / 2, 3)
            job.epsg = dsm_meta.epsg
            job.save()

            job.status = JobStatus.PROCESSING
            job.progress_message = "Computing height-above-ground"
            job.progress_pct = 40
            job.save()
            dsm_to_ndsm.subtract(dsm_path, dtm_path, ndsm_path)

            #Free raster inputs once nDSM is built.
            dsm_path.unlink(missing_ok=True)
            dtm_path.unlink(missing_ok=True)

        elif job.input_mode == "point_cloud":
            laz_candidates = list(job_dir.glob("input.la?"))
            if not laz_candidates:
                raise ValidationError("Uploaded LAS / LAZ file is missing on disk.")
            laz_path = laz_candidates[0]

            job.status = JobStatus.PROCESSING
            job.progress_message = "Reading points"
            job.progress_pct = 25
            job.save()

            #Map the laz_to_ndsm internal phases to the 25 -> 75 %
            #band reserved for PROCESSING. Each phase covers a slice
            #of that band; the fraction reported by the pipeline
            #drives the bar inside its slice. Saving on every call
            #would hammer the disk, throttle to ~ once a second.
            phase_band = {
                "reading":      (25, 33),
                "reprojecting": (33, 40),
                "kdtree":       (40, 43),
                "querying":     (43, 70),
                "rasterising":  (70, 73),
                "writing":      (73, 75),
            }
            phase_msg = {
                "reading":      "Reading points",
                "reprojecting": "Reprojecting to metres",
                "kdtree":       "Building ground KDTree",
                "querying":     "Computing height-above-ground",
                "rasterising":  "Building the nDSM raster",
                "writing":      "Writing nDSM to disk",
            }
            last_save = [0.0]

            def _laz_progress(phase: str, fraction: float) -> None:
                lo, hi = phase_band.get(phase, (25, 75))
                pct = lo + (hi - lo) * max(0.0, min(1.0, fraction))
                job.progress_pct = pct
                job.progress_message = phase_msg.get(phase, "Processing")
                now = time.monotonic()
                if now - last_save[0] >= 0.8 or fraction >= 1.0:
                    job.save()
                    last_save[0] = now

            laz_to_ndsm.rasterise(
                laz_path,
                ndsm_path,
                pixel_meters=settings.raster_pixel_meters,
                on_progress=_laz_progress,
            )

            #Inspect the result we just wrote so we have the same
            #bounds_wgs84 + pixel + EPSG metadata the raster path
            #produces upstream.
            ndsm_meta = inspect(ndsm_path)
            job.bounds_wgs84 = ndsm_meta.bounds_wgs84
            job.pixel_size_meters = round(ndsm_meta.pixel_size_x, 3)
            job.epsg = ndsm_meta.epsg
            job.progress_pct = 75
            job.save()

            #Free the original point cloud once we have the nDSM.
            laz_path.unlink(missing_ok=True)

        else:
            raise ValidationError(f"Unknown input mode {job.input_mode!r}")

        job.status = JobStatus.COGGING
        job.progress_message = "Wrapping as Cloud-Optimized GeoTIFF"
        job.progress_pct = 90
        job.save()
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = settings.output_dir / f"{job_id}.tif"
        cog_mod.cogify(ndsm_path, output_path)
        ndsm_path.unlink(missing_ok=True)

        assert job.bounds_wgs84 is not None
        download_filename = f"helios-ndsm-{job_id}.tif"
        #The cog_url stays a real VPS URL because the browser needs
        #it to trigger the download. The YAML snippet uses HA's
        #/local/ path instead so a Helios card config never depends
        #on helios-lidar.org being up.
        job.cog_url = f"{settings.public_base_url.rstrip('/')}/output/{job_id}.tif"
        job.download_filename = download_filename
        job.yaml_snippet = yaml_snippet.render(download_filename, job.bounds_wgs84)
        job.progress_message = "Ready"
        job.progress_pct = 100
        job.status = JobStatus.DONE
        #Mirror the COG_TTL_SECONDS timer below so the frontend can
        #render a live countdown to the deletion moment.
        job.cog_expires_at = time.time() + COG_TTL_SECONDS
        job.save()
        #Bump the public conversions counter. Best-effort, a
        #stats hiccup never blocks a finished job.
        try:
            _stats.record_conversion()
        except Exception as exc:
            log.warning("stats.record_conversion failed: %s", exc)

        #Schedule a delayed delete of the COG so the VPS doesn't keep
        #per-user outputs around. The browser auto-download fires
        #immediately on job done, so the user's local copy is in
        #place well before this timer trips.
        threading.Timer(COG_TTL_SECONDS, _delete_output_cog, args=[job_id]).start()

    except ValidationError as exc:
        job.status = JobStatus.FAILED
        job.error = str(exc)
        job.progress_message = "Validation failed"
        job.save()
    except Exception as exc:  # noqa: BLE001
        log.exception("processing failed for job %s", job_id)
        job.status = JobStatus.FAILED
        job.error = f"{type(exc).__name__}: {exc}"
        job.progress_message = "Internal error"
        job.save()


def _delete_output_cog(job_id: str) -> None:
    """Remove the published COG once its TTL window expires. Best-
    effort: a missing file is fine (cleanup cron may have run first;
    the user may have replayed the job; the file may already be gone
    for any other reason).
    """
    cog_path = settings.output_dir / f"{job_id}.tif"
    try:
        cog_path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("delayed cog cleanup failed for %s: %s", job_id, exc)
