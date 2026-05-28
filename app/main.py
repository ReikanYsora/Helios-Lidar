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

import json
import logging
import shutil
import threading
import time
from pathlib import Path

import os
import secrets

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app import github_stars, helios_downloads, jobs as job_store, lidar_sources, server_stats, visitor_stats
from app.config import settings
from app.jobs import Job, JobStatus
from app.stats import StatsStore
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
    version="1.6.4",
)

#CORS for the public /api endpoints. The landing page consumes
#the conversion counter + the GitHub-downloads proxy from the same
#origin, but we keep the wildcard so a third-party dashboard could
#embed the counter widget if it ever wanted to. nginx adds the
#rate-limit layer on top.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["content-type"],
    allow_credentials=False,
    max_age=86400,
)


#Frontend directory: rsync target on the VPS is /var/helios-lidar/frontend,
#but FastAPI runs from /var/helios-lidar/app, so resolve the sibling
#`frontend/` next to the `app/` package. Same layout in dev.
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

#Pipeline conversion counter. Lives next to the existing jobs /
#output directories; persists across deploys because it's outside
#the git checkout. Single shared instance because FastAPI runs as
#one uvicorn worker and StatsStore serialises its own writes.
_stats = StatsStore(settings.jobs_dir.parent / "stats" / "stats.db")

#Server load sampler: writes one (load, mem, disk) row per minute
#into stats.db. Started in-process so we don't need a separate
#cron job; daemon thread so it exits cleanly with the worker.
_server_sampler = server_stats.ServerSampler(settings.jobs_dir.parent / "stats" / "stats.db")
_server_sampler.start()


def _backfill_conversion_ips_once() -> None:
    """Best-effort: for every conversion row that pre-dates the
    client_ip column, scan the last year of nginx logs for POST
    /jobs entries and pair each completion timestamp with the
    most recent POST /jobs request from the same IP that landed
    within 20 minutes before it. Runs once in a daemon thread
    at startup so we don't block boot on log parsing."""
    def _run():
        try:
            missing = _stats.conversions_missing_ip()
            if not missing:
                return
            from datetime import datetime, timedelta, timezone
            since = datetime.now(timezone.utc) - timedelta(days=365)
            posts = visitor_stats.parse_post_jobs_entries(since)
            n = _stats.backfill_conversion_ips(posts)
            log.info("conversions backfill: paired %d / %d missing IPs",
                     n, len(missing))
        except Exception:
            log.exception("conversion IP backfill failed")
    threading.Thread(target=_run, name="conv-backfill", daemon=True).start()


_backfill_conversion_ips_once()


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


@app.get("/api/helios-downloads")
def helios_downloads_endpoint() -> JSONResponse:
    """Per-release download counts for the Helios card, proxied off
    the GitHub Releases API. Consumed by the landing page so it can
    show the latest-version download count with a hover-revealed
    per-version breakdown. Falls back to the stale cache on GitHub
    errors; returns 503 only when we never managed a cold fetch.
    """
    #Record a download_snapshots row on every fresh GitHub fetch
    #so the cumulative-download history accumulates regardless of
    #whether anyone is hitting the /stats dashboard; the home-page
    #counter endpoint runs far more often, so this is where most
    #snapshots come from in steady state.
    snap = helios_downloads.get_downloads_snapshot(
        on_fresh=_stats.record_download_snapshot,
        on_fresh_per_tag=_stats.record_download_per_tag,
    )
    if snap is None:
        return JSONResponse(
            content={
                "latest_tag": None,
                "latest_downloads": 0,
                "total_downloads": 0,
                "by_version": [],
                "error": "GitHub release data unavailable, retry shortly.",
            },
            status_code=503,
            headers={"cache-control": "no-store"},
        )
    return JSONResponse(
        content={
            "latest_tag": snap.latest_tag,
            "latest_downloads": snap.latest_downloads,
            "total_downloads": snap.total_downloads,
            "by_version": [
                {"tag": r.tag, "downloads": r.downloads}
                for r in snap.by_version
            ],
        },
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


#Stats dashboard auth: HTTP Basic with credentials from env vars.
#When STATS_USERNAME or STATS_PASSWORD is unset the auth is bypassed
#so a fresh deploy keeps working; once both are set the dashboard
#(HTML + JSON) requires the credentials. constant-time compare to
#avoid trivial timing attacks.
_stats_auth = HTTPBasic(realm="Helios Analytics", auto_error=False)


def _stats_credentials_required() -> bool:
    return bool(os.environ.get("STATS_USERNAME") and os.environ.get("STATS_PASSWORD"))


def _check_stats_auth(creds: HTTPBasicCredentials | None = Depends(_stats_auth)) -> None:
    if not _stats_credentials_required():
        return
    expected_user = os.environ.get("STATS_USERNAME", "")
    expected_pass = os.environ.get("STATS_PASSWORD", "")
    if creds is None or not (
        secrets.compare_digest(creds.username, expected_user)
        and secrets.compare_digest(creds.password, expected_pass)
    ):
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="Helios Analytics"'},
        )


@app.get("/privacy")
def privacy_page() -> FileResponse:
    """Static privacy policy. Linked from the footer. Served with
    a no-cache hint so style/markup tweaks ship to visitors on
    their next reload without an explicit hard-refresh."""
    return FileResponse(
        FRONTEND_DIR / "privacy.html",
        media_type="text/html; charset=utf-8",
        headers={"cache-control": "no-cache, must-revalidate"},
    )


@app.get("/coverage")
def coverage_page() -> FileResponse:
    """Native LiDAR coverage browser. Linked from the primer
    section on the homepage. Same no-cache hint as /privacy so
    map data + i18n updates propagate without hard reloads."""
    return FileResponse(
        FRONTEND_DIR / "coverage.html",
        media_type="text/html; charset=utf-8",
        headers={"cache-control": "no-cache, must-revalidate"},
    )


@app.get("/docs/{lang}")
def docs_page(lang: str) -> FileResponse:
    """Developer-facing documentation, served per-language from
    frontend/docs/{lang}.html. The naming convention keeps the URL
    space clean ( /docs/fr , /docs/en when it lands, etc.) and makes
    adding a new translation a single-file drop without touching this
    route. 404 if the locale isn't published yet, FastAPI handles the
    response via the FileResponse path miss."""
    #Restrict to a-z lowercase letters to keep the path safe (no
    #traversal, no funky filenames). Two- or three-letter ISO 639-1 /
    #639-2 codes only.
    if not lang.isalpha() or not lang.islower() or len(lang) > 3:
        raise HTTPException(status_code=404)
    target = FRONTEND_DIR / "docs" / f"{lang}.html"
    if not target.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        target,
        media_type="text/html; charset=utf-8",
        headers={"cache-control": "no-cache, must-revalidate"},
    )


@app.get("/stats", dependencies=[Depends(_check_stats_auth)])
def stats_page() -> FileResponse:
    """Hidden visitor-analytics dashboard. Not linked from anywhere
    on the public site + disallowed in robots.txt. The HTML page
    fetches /api/stats on load and renders the doughnut + bar
    charts via Chart.js.
    """
    page = FRONTEND_DIR / "stats.html"
    return FileResponse(
        page,
        media_type="text/html; charset=utf-8",
        headers={"cache-control": "no-cache, must-revalidate"},
    )


@app.get("/api/github-stars", dependencies=[Depends(_check_stats_auth)])
def api_github_stars() -> JSONResponse:
    """GitHub star history for the tracked repos (Helios + Helios-Lidar).
    Returned as a single per-repo payload listing every star's
    `starred_at` epoch; the frontend buckets / slices client-side
    according to the visible range tab (24h / 7d / 30d / 1y). Cached
    in-process for 1 hour to stay well inside the 60 req/h anonymous
    GitHub quota.
    """
    snap = github_stars.get_stars_snapshot()
    return JSONResponse(
        content={
            "fetched_at_unix": snap.fetched_at_unix,
            "repos": [
                {
                    "owner": r.owner,
                    "repo":  r.repo,
                    "label": r.label,
                    "total": len(r.starred_at_unix),
                    "starred_at_unix": r.starred_at_unix,
                }
                for r in snap.repos
            ],
        },
        headers={"cache-control": "public, max-age=600"},
    )


@app.get("/api/stats", dependencies=[Depends(_check_stats_auth)])
def api_stats() -> JSONResponse:
    """Aggregated visitor stats parsed from the nginx access logs.
    In-process cache (5 min TTL) inside `visitor_stats` keeps
    refresh cost negligible. Returns 503 only if the very first
    fetch can't read any log row (e.g. nginx log path unreadable).
    """
    snap = visitor_stats.get_snapshot(
        settings.jobs_dir.parent / "stats" / "geo_cache.db",
        stats_store=_stats,
        downloads_module=helios_downloads,
        server_sampler=_server_sampler,
    )
    if snap is None:
        return JSONResponse(
            content={"error": "Visitor stats not available yet."},
            status_code=503,
        )
    return JSONResponse(
        content=visitor_stats.snapshot_to_dict(snap),
        headers={"cache-control": "public, max-age=120"},
    )


@app.get("/robots.txt")
def robots_txt() -> FileResponse:
    """Serve robots.txt at the site root. Crawlers expect this at
    /robots.txt verbatim, so we route it through FastAPI rather
    than ask every operator to add a dedicated nginx location."""
    return FileResponse(FRONTEND_DIR / "robots.txt", media_type="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml() -> FileResponse:
    """Serve sitemap.xml at the site root, same reasoning as
    robots.txt: search engines look for it at the exact path."""
    return FileResponse(FRONTEND_DIR / "sitemap.xml", media_type="application/xml")


@app.get("/api/lidar-sources")
def api_lidar_sources() -> JSONResponse:
    """Return the rendered LIDAR_SOURCES.md as HTML. Source of truth
    is the markdown file at the repo root, community-maintained via
    pull requests.
    """
    return JSONResponse({"html": lidar_sources.render_html()})


@app.post("/jobs")
async def create_job(
    request: Request,
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

    #Capture the real client IP for the per-country breakdown of
    #conversions. Trust the X-Forwarded-For from nginx (we're behind
    #the OVH vhost), fall back to the direct peer address.
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    client_ip = fwd or (request.client.host if request.client else None)
    background.add_task(_process, job.job_id, client_ip)

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


def _process(job_id: str, client_ip: str | None = None) -> None:
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
        #Immediate space reclaim: now that the job is DONE and the COG
        #lives under output/, the per-job working directory only needs
        #to keep `status.json` so the polling endpoint can still answer
        #GET /jobs/{id}. The original LAZ + intermediate rasters can be
        #2-3 GB each, leaving them around for the full COG_TTL_SECONDS
        #window pinned disk space across every concurrent job AND
        #orphaned them whenever the worker was OOM-killed mid-process
        #(the threading.Timer below also dies on restart, leaving the
        #heavy files until the periodic sweeper runs).
        _trim_job_dir_to_status_only(job_id)
        #Bump the public conversions counter. Best-effort, a
        #stats hiccup never blocks a finished job.
        try:
            _stats.record_conversion(client_ip=client_ip)
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
    #Also drop the per-job working directory entirely now that the COG
    #is gone. The status.json was useful only to answer pending pollers
    #while the COG was downloadable; once the file is gone, the job
    #endpoint can return 410 instead.
    job_dir = settings.jobs_dir / job_id
    try:
        shutil.rmtree(job_dir, ignore_errors=True)
    except OSError as exc:
        log.warning("delayed job dir cleanup failed for %s: %s", job_id, exc)


def _trim_job_dir_to_status_only(job_id: str) -> None:
    """Drop every file inside the job's working dir except `status.json`.
    Called immediately after the COG has been published to output/, to
    reclaim the multi-GB LAZ + intermediate rasters straight away
    rather than waiting for the TTL window AND surviving worker
    restarts where the in-process threading.Timer would otherwise die.
    """
    job_dir = settings.jobs_dir / job_id
    if not job_dir.is_dir():
        return
    for entry in job_dir.iterdir():
        if entry.name == "status.json":
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("trim_job_dir: %s failed: %s", entry, exc)


#Sweeper interval + thresholds. 10 min between passes is short enough
#that an OOM-killed worker leaves disk waste for at most one cycle
#after the restart; 30 min stale-processing threshold is comfortably
#longer than any real conversion (~ 5-10 min) so we never trim an
#actually-running job by accident.
_SWEEPER_INTERVAL_SECONDS = 10 * 60
_STALE_PROCESSING_THRESHOLD_SECONDS = 30 * 60


def _sweep_stale_jobs() -> None:
    """One sweep pass. Removes:
      - every job dir whose `status.json` reads DONE or FAILED AND was
        last touched > COG_TTL_SECONDS ago (final cleanup, mirrors the
        threading.Timer that may have died with a worker restart)
      - every job dir whose `status.json` reads a non-terminal state
        (QUEUED / VALIDATING / PROCESSING / COGGING) AND was last
        touched > 30 min ago (worker died mid-conversion, the timer
        won't ever advance it, so the dir is dead weight)
      - every orphan dir without a `status.json` AND created > 1 h ago
        (mid-upload crash before save())

    Best-effort, swallows OSError. A sweep that fails to delete one
    dir still tries the next.
    """
    now = time.time()
    if not settings.jobs_dir.is_dir():
        return
    removed = 0
    kept = 0
    for entry in settings.jobs_dir.iterdir():
        if not entry.is_dir():
            continue
        status_file = entry / "status.json"
        try:
            if not status_file.is_file():
                #Orphan: mid-upload crash before save(). Grace period
                #of 1 hour so we don't blow up a really-just-now upload.
                mtime = entry.stat().st_mtime
                if (now - mtime) > 3600:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
                else:
                    kept += 1
                continue
            raw = status_file.read_text()
            data = json.loads(raw)
            status = data.get("status", "")
            mtime = status_file.stat().st_mtime
            age = now - mtime
            if status in ("done", "failed") and age > COG_TTL_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
            elif status in ("queued", "validating", "processing", "cogging") \
                    and age > _STALE_PROCESSING_THRESHOLD_SECONDS:
                #Worker died mid-conversion. Nothing will advance the
                #state, the dir is just consuming disk.
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
            else:
                kept += 1
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.warning("sweep: failed to inspect %s: %s", entry, exc)
            kept += 1
    if removed:
        log.info("sweep: removed %d stale job dir(s), kept %d", removed, kept)


def _sweeper_loop() -> None:
    """Daemon thread loop. Fires _sweep_stale_jobs every
    _SWEEPER_INTERVAL_SECONDS for the lifetime of the worker. One
    sweep on startup catches the orphan accumulation from any
    previous worker restart.
    """
    #Immediate first pass on startup. Important on worker restart so
    #orphans from the previous lifecycle get cleaned even if the
    #service runs continuously after that.
    try:
        _sweep_stale_jobs()
    except Exception as exc:  # noqa: BLE001
        log.warning("sweeper startup pass failed: %s", exc)
    while True:
        time.sleep(_SWEEPER_INTERVAL_SECONDS)
        try:
            _sweep_stale_jobs()
        except Exception as exc:  # noqa: BLE001
            log.warning("sweeper pass failed: %s", exc)
    #(unreachable, the loop exits only with the daemon thread)


_sweeper_thread = threading.Thread(target=_sweeper_loop, name="jobs-sweeper", daemon=True)
_sweeper_thread.start()
