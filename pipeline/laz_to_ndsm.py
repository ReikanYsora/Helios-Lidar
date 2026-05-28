"""Rasterise a LAS / LAZ point cloud directly into a height-above-
ground (nDSM) Float32 GeoTIFF.

This skips the intermediate DSM + DTM raster pair the two-input flow
needs: we compute the per-point height relative to the nearest ground
point first, then take the per-cell maximum of those heights. Matches
the result of PDAL's `filters.hag_nn` + `writers.gdal` with
`output_type=max`, but in pure-Python on top of laspy (with the
`lazrs` Rust LAZ codec) + scipy (cKDTree) + the GDAL Python bindings
the rest of the pipeline already speaks. No system PDAL needed,
which sidesteps the absence of PDAL packages in Ubuntu 25.04 plucky
and the UbuntuGIS PPA's missing plucky release.

The implementation expects classified LiDAR (the standard 1 byte
per point with code 2 = ground, the ASPRS spec used by virtually
every modern national LiDAR programme). If a file has no ground-
classified points we fail with a user-facing message rather than
guessing a ground filter; that decision belongs upstream.

Memory: a 1 km x 1 km tile at 10 points / m^2 holds ~ 10 M points,
~ 240 MB resident as four Float64 arrays (x, y, z, hag). On a 50 M-
point input the function used to peak around 6.7 GB (the laspy
LasData held all dimensions until end-of-function, plus the KDTree
allocated `nn_idx` (n_points, k) and `ground_z[nn_idx]` of the same
shape, both Float64 / intp), which OOM-killed the uvicorn worker on
the 7.6 GB VPS. The current version `del`s the laspy point-record as
soon as the four dimensions we need are copied out, builds the
KDTree result chunk-by-chunk into a single Float32 buffer instead of
the full (n_points, k) matrix, and `del`s the per-point working
arrays (x, y, z, hag, classification, ground_mask, ground_z, tree)
the moment each is no longer referenced. Peak comes down to roughly
3 GB on the same input.
"""

from __future__ import annotations

import gc
from collections.abc import Callable
from pathlib import Path

import laspy
import numpy as np
import pyproj
from osgeo import gdal, osr
from scipy.spatial import cKDTree

from pipeline.dsm_to_ndsm import DTM_NODATA, NDSM_NODATA
from pipeline.validate import ValidationError

#ASPRS Standard LIDAR Point Classes, ground returns are class 2.
GROUND_CLASS: int = 2

#K nearest ground neighbours averaged for the per-point ground
#elevation lookup. 1 is closest to PDAL's filters.hag_nn but tends to
#pick up noisy outliers right at the bottom of building walls; 3 is a
#robust middle ground.
GROUND_KNN: int = 3


#10 query chunks so the on_progress callback can tick the bar
#through the KDTree pass instead of jumping from 25 % to 75 % when
#one giant scipy call returns. Reused for the reprojection pass
#below for the same reason: 12 M points through pyproj is ~ 10 s of
#wall-clock and the user wants to see the bar move.
QUERY_CHUNKS: int = 10


#Tolerance for "is this CRS already in metres?" against the
#unit-conversion factor pyproj exposes. PROJ stores the metre
#exactly, but a couple of EPSG entries round-trip through 1.0 + tiny
#float noise; 1e-9 is comfortably under any real foot / chain / link
#unit factor we'd ever see, so the test stays robust.
_METRE_FACTOR_TOL: float = 1e-9


def _horizontal_part(crs: pyproj.CRS) -> pyproj.CRS:
    """Return the planar CRS that carries the (x, y) coordinates.

    A compound CRS (`COMPD_CS` in WKT, common in US LiDAR products
    that bundle a projected horizontal datum with NAVD88 heights)
    keeps the horizontal piece at index 0 of `sub_crs_list`. Other
    CRSes are themselves the horizontal piece.
    """
    if crs.is_compound:
        return crs.sub_crs_list[0]
    return crs


def _vertical_unit_factor(crs: pyproj.CRS) -> float:
    """Multiplier that takes the LAZ's Z values into metres.

    Compound CRSes carry an explicit vertical sub-CRS, so we read
    its unit factor (e.g. NAVD88 height in US survey feet returns
    0.3048006096012192). When the source is a plain projected CRS,
    LAZ files conventionally store Z in the same linear unit as the
    horizontal axes, so the horizontal factor doubles as the vertical
    one. Geographic sources have no linear unit on the projected
    axes; the LAZ spec then expects Z in metres, so we return 1.0.
    """
    if crs.is_compound:
        vert = crs.sub_crs_list[1]
        return float(vert.axis_info[0].unit_conversion_factor)
    if crs.is_projected:
        return float(crs.axis_info[0].unit_conversion_factor)
    return 1.0


def _utm_epsg_for(lon: float, lat: float) -> int:
    """Pick the WGS84 UTM zone (EPSG 326XX / 327XX) covering the
    given lon / lat. The pipeline reprojects non-metric inputs into
    this zone so the rasterised nDSM ships in true metres regardless
    of the source unit. UTM zones are 6 degrees wide; the formula
    matches the standard "longitude + 180, divide by 6, add 1"
    convention. Polar latitudes (> 84 or < -80) fall outside UTM
    proper; we still return a UTM zone there since nobody is
    flying residential LiDAR at the poles.
    """
    zone = int((lon + 180.0) // 6.0) + 1
    zone = max(1, min(60, zone))
    return (32600 if lat >= 0.0 else 32700) + zone


def _select_target_crs(
    src_crs: pyproj.CRS, x_arr: np.ndarray, y_arr: np.ndarray,
) -> tuple[pyproj.CRS, bool]:
    """Choose the working CRS for rasterisation.

    Pass-through path: the source horizontal CRS is a projected CRS
    whose linear unit is already the metre. The pipeline keeps that
    CRS as-is so French Lambert-93, UTM tiles, ETRS89-LAEA and the
    rest of the metric national grids ship through with zero
    coordinate change.

    Reprojection path: anything else (US survey foot, international
    foot, geographic lat / lon, anything PROJ exposes a non-metre
    factor for) gets reprojected into the UTM zone covering the data
    centre. UTM is a universally available metric CRS and the
    distortion across a single ~ 1 km LiDAR tile is well below the
    pipeline's 1 m raster pitch, so picking the local zone keeps the
    nDSM geometrically faithful without any per-source calibration.

    The function returns the target CRS plus a flag that tells the
    caller whether to actually run the transform; the pass-through
    case skips an expensive numpy + pyproj pass over the whole point
    cloud.
    """
    horiz = _horizontal_part(src_crs)

    if horiz.is_projected:
        factor = float(horiz.axis_info[0].unit_conversion_factor)
        if abs(factor - 1.0) <= _METRE_FACTOR_TOL:
            return horiz, False

    #Pick the UTM zone from the data centre, reprojected from the
    #source horizontal CRS into geographic WGS84. Mean of the raw
    #coordinates is a fine approximation of the tile centre for any
    #LiDAR tile we'll realistically see.
    geo = pyproj.CRS.from_epsg(4326)
    to_geo = pyproj.Transformer.from_crs(horiz, geo, always_xy=True)
    cx = float(x_arr.mean())
    cy = float(y_arr.mean())
    center_lon, center_lat = to_geo.transform(cx, cy)
    target_epsg = _utm_epsg_for(center_lon, center_lat)
    return pyproj.CRS.from_epsg(target_epsg), True


def rasterise(
    laz_path: Path,
    output_path: Path,
    pixel_meters: float = 1.0,
    on_progress: Callable[[str, float], None] | None = None,
) -> None:
    """Write the nDSM raster derived from `laz_path` at the given
    cell pitch (default 1 m, matching the rest of the Helios
    pipeline).

    `on_progress(phase, fraction)` is called with `fraction` in [0, 1]
    at each phase boundary so a long-running caller (a FastAPI
    BackgroundTask in our case) can surface a moving progress bar
    instead of the bare 0 % -> 100 % step the unchunked version
    showed. The phase strings are stable: "reading", "kdtree",
    "querying", "rasterising", "writing".

    Raises:
        ValidationError: when the input has no usable ground returns
            or no resolvable CRS; the message is shown verbatim in
            the job status JSON.
    """
    gdal.UseExceptions()
    report = on_progress or (lambda _phase, _frac: None)

    report("reading", 0.0)
    with laspy.open(str(laz_path)) as reader:
        header = reader.header
        crs = header.parse_crs()
        if crs is None:
            raise ValidationError(
                "The LAS / LAZ file has no embedded coordinate "
                "reference system. Reproject and embed a CRS before "
                "uploading."
            )
        points = reader.read()
    report("reading", 1.0)

    x = np.asarray(points.x, dtype=np.float64)
    y = np.asarray(points.y, dtype=np.float64)
    z = np.asarray(points.z, dtype=np.float64)
    classification = np.asarray(points.classification, dtype=np.uint8)
    #Drop the laspy LasData object as soon as we have copies of the four dimensions we actually use, the rest of the record (intensity,
    #return-number, GPS time, RGB, ...) holds 8-12 extra dimensions for every point, which on a 50 M-point tile is ~ 1.5 GB of resident
    #memory we never read. del + gc.collect forces the unused dimensions back to the OS through glibc's mmap arenas.
    del points
    gc.collect()

    if x.size == 0:
        raise ValidationError("The LAS / LAZ file contains no points.")

    #Bring the point cloud into a metric working CRS before we touch
    #anything spatial. Sources already in metres (Lambert-93, UTM,
    #ETRS89-LAEA, etc.) skip the transform entirely; sources in US
    #survey feet, international feet, or geographic lat / lon get
    #reprojected into the local UTM zone so the 1 m raster pitch
    #downstream is honestly 1 m and Helios's ray-march reads the
    #correct cell size from the COG geotransform.
    target_crs, needs_reproject = _select_target_crs(crs, x, y)
    if needs_reproject:
        src_horiz = _horizontal_part(crs)
        report("reprojecting", 0.0)
        transformer = pyproj.Transformer.from_crs(src_horiz, target_crs, always_xy=True)
        #Chunked so the progress bar moves through the transform.
        #pyproj returns new arrays per call; we overwrite the
        #already-read indices in-place so peak memory stays at the
        #point cloud's own footprint.
        chunk_size = max(1, x.size // QUERY_CHUNKS)
        for start in range(0, x.size, chunk_size):
            end = min(start + chunk_size, x.size)
            cx, cy = transformer.transform(x[start:end], y[start:end])
            x[start:end] = cx
            y[start:end] = cy
            report("reprojecting", end / x.size)

    #Z conversion: the source vertical CRS dictates the factor.
    #Compound CRSes (e.g. NAVD88 height in ftUS) expose it directly;
    #single-CRS sources reuse the horizontal factor since LAZ stores
    #Z in the same linear unit as the projected axes by convention.
    z_factor = _vertical_unit_factor(crs)
    if abs(z_factor - 1.0) > _METRE_FACTOR_TOL:
        z *= z_factor

    ground_mask = classification == GROUND_CLASS
    ground_count = int(ground_mask.sum())
    if ground_count < 10:
        raise ValidationError(
            f"The LAS / LAZ file has only {ground_count} ground-classified "
            "points (ASPRS class 2). A height-above-ground raster needs a "
            "classified ground surface; re-run the LiDAR processing chain "
            "with a ground filter (PMF, SMRF) before uploading."
        )

    #Compute height-above-ground for every point: query the K nearest
    #ground points in (x, y), take the mean of their Z, subtract from
    #the point's Z. cKDTree on 2D is the right shape because we're
    #asking "what's the ground elevation directly below this point",
    #not a true 3D nearest neighbour.
    report("kdtree", 0.0)
    ground_xy = np.column_stack((x[ground_mask], y[ground_mask]))
    ground_z = z[ground_mask]
    tree = cKDTree(ground_xy)
    #cKDTree copies the input internally, the column-stacked array is no longer needed. del + collect frees ~80 MB / 5 M ground points.
    del ground_xy
    gc.collect()
    report("kdtree", 1.0)

    #Chunked KDTree query so we can tick the progress bar between
    #batches AND so the per-chunk index buffer never grows to the full
    #point-count shape. The old version allocated `nn_idx` of shape
    #(n_points, k) up front (~1.2 GB at 50 M points × k=3 × intp 8 bytes),
    #then a second (n_points, k) buffer for `ground_z[nn_idx]` before the
    #mean reduction, peaking at ~2.5 GB of intermediates. Writing each
    #chunk's mean directly into a single `(n_points,)` float32 buffer
    #drops that peak to one chunk's worth at a time (~120 MB).
    k = min(GROUND_KNN, ground_count)
    n_points = x.size
    chunk_size = max(1, n_points // QUERY_CHUNKS)
    ground_z_per_point = np.empty(n_points, dtype=np.float32)
    for start in range(0, n_points, chunk_size):
        end = min(start + chunk_size, n_points)
        chunk_xy = np.column_stack((x[start:end], y[start:end]))
        _, chunk_idx = tree.query(chunk_xy, k=k, workers=-1)
        if k == 1:
            ground_z_per_point[start:end] = ground_z[chunk_idx].astype(np.float32, copy=False)
        else:
            ground_z_per_point[start:end] = ground_z[chunk_idx].mean(axis=1).astype(np.float32, copy=False)
        report("querying", end / n_points)
    #Tree + chunk-xy buffers are no longer needed past this point. The tree itself can hold several hundred MB on a dense input.
    del tree
    gc.collect()

    hag = (z - ground_z_per_point).astype(np.float32)
    np.maximum(hag, 0.0, out=hag)

    #Bin to a raster: bbox snapped to integer pixel multiples so the
    #output stays clean at any pixel pitch. Y axis flipped because
    #raster rows scan top-to-bottom.
    min_x_snap = np.floor(x.min() / pixel_meters) * pixel_meters
    max_x_snap = np.ceil(x.max() / pixel_meters) * pixel_meters
    min_y_snap = np.floor(y.min() / pixel_meters) * pixel_meters
    max_y_snap = np.ceil(y.max() / pixel_meters) * pixel_meters
    width = int(round((max_x_snap - min_x_snap) / pixel_meters))
    height = int(round((max_y_snap - min_y_snap) / pixel_meters))
    if width == 0 or height == 0:
        raise ValidationError(
            "The LAS / LAZ file footprint collapses to less than one "
            "raster cell at the configured 1 m pitch. The file is "
            "probably empty or has corrupt coordinates."
        )

    col = np.clip(((x - min_x_snap) / pixel_meters).astype(np.int64), 0, width - 1)
    row = np.clip(((max_y_snap - y) / pixel_meters).astype(np.int64), 0, height - 1)
    #x and y are no longer needed for either the nDSM or the DTM rasterisation, both downstream passes work off col/row plus the
    #per-point arrays (hag, ground_z_per_point). On a 50 M-point input that frees 2 × 400 MB.
    del x, y
    gc.collect()

    #Band 1: per-cell max height-above-ground. np.maximum.at is the
    #accumulator-style ufunc for "take the per-cell max over a
    #sequence of unordered writes"; O(points) and avoids a Python
    #loop. Any cell that never saw a point stays at NDSM_NODATA.
    report("rasterising", 0.0)
    ndsm = np.full((height, width), NDSM_NODATA, dtype=np.float32)
    np.maximum.at(ndsm, (row, col), hag)
    del hag
    gc.collect()

    #Band 2: per-cell ground elevation, computed from the ground-
    #classified points only (ASPRS class 2). Mean Z of the ground
    #returns is the standard PDAL-style DTM definition and matches
    #what the DSM/DTM raster-pair workflow already supplies on
    #band 2. The card needs this to ray-march obstacles through
    #sloped ground correctly: a building 50 m east of the home with
    #the terrain dropping 8 m on the way no longer reads as a 5 m
    #obstacle but as a -3 m one, i.e. below the panel and invisible
    #to the sun ray.
    #
    #Cells that contain no ground return fall back to the KDTree
    #ground estimate built earlier for the nDSM subtraction, so the
    #DTM stays dense even where the ground point cloud is sparse.
    #Cells with no points at all stay at DTM_NODATA.
    ground_col_idx = col[ground_mask]
    ground_row_idx = row[ground_mask]
    ground_z_pts   = z[ground_mask]
    del z, classification, ground_mask, ground_z
    gc.collect()
    dtm_sum   = np.zeros((height, width), dtype=np.float64)
    dtm_count = np.zeros((height, width), dtype=np.int32)
    np.add.at(dtm_sum,   (ground_row_idx, ground_col_idx), ground_z_pts)
    np.add.at(dtm_count, (ground_row_idx, ground_col_idx), 1)
    del ground_row_idx, ground_col_idx, ground_z_pts
    dtm = np.full((height, width), DTM_NODATA, dtype=np.float32)
    has_ground = dtm_count > 0
    dtm[has_ground] = (dtm_sum[has_ground] / dtm_count[has_ground]).astype(np.float32)
    if not has_ground.all():
        #Fill ground-empty cells with the KDTree-derived ground Z
        #(same value the nDSM subtraction uses for each non-ground
        #point). Adding everything in then masking back to "only
        #empty cells" keeps the code allocation-free.
        np.add.at(dtm_sum,   (row, col), ground_z_per_point)
        np.add.at(dtm_count, (row, col), 1)
        blended = (~has_ground) & (dtm_count > 0)
        dtm[blended] = (dtm_sum[blended] / dtm_count[blended]).astype(np.float32)
    del row, col, ground_z_per_point, dtm_sum, dtm_count, has_ground
    gc.collect()
    report("rasterising", 1.0)

    #Write as a 2-band Float32 GeoTIFF with the right projection so
    #the downstream COG step + the Helios bbox lookup work
    #unchanged. Band 1 is the nDSM (height above local ground),
    #band 2 is the DTM (ground elevation in the source vertical
    #datum).
    report("writing", 0.0)
    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(
        str(output_path),
        xsize=width,
        ysize=height,
        bands=2,
        eType=gdal.GDT_Float32,
        options=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"],
    )
    #(origin_x, pixel_w, 0, origin_y, 0, -pixel_h) , Y step negative
    #because rows scan top to bottom.
    out_ds.SetGeoTransform((min_x_snap, pixel_meters, 0.0, max_y_snap, 0.0, -pixel_meters))

    #The output COG is tagged with the working (metric) CRS, not the
    #original source. For sources that were already in metres these
    #are the same CRS; for ftUS / lat-lon sources this is the UTM
    #zone we reprojected into. Either way, gt[1] now matches the
    #cell size in metres, which is what the Helios card expects when
    #it ray-marches the nDSM for LiDAR shading.
    srs = osr.SpatialReference()
    srs.ImportFromWkt(target_crs.to_wkt())
    out_ds.SetProjection(srs.ExportToWkt())

    band1 = out_ds.GetRasterBand(1)
    band1.SetNoDataValue(NDSM_NODATA)
    band1.SetDescription("nDSM (height above local ground, metres)")
    band1.WriteArray(ndsm)
    band1.FlushCache()

    band2 = out_ds.GetRasterBand(2)
    band2.SetNoDataValue(DTM_NODATA)
    band2.SetDescription("DTM (ground elevation, source vertical datum, metres)")
    band2.WriteArray(dtm)
    band2.FlushCache()

    out_ds.FlushCache()
    out_ds = None
    report("writing", 1.0)
