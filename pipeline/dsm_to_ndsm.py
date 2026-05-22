"""Subtract a DTM from a DSM to produce a height-above-ground (nDSM)
raster the Helios card consumes through its `lidar-local-ndsm-*`
provider path. Writes a TWO-BAND COG: band 1 is the nDSM (height
above local ground, the original Helios contract); band 2 is the
DTM in absolute metres (datum left untouched: the card subtracts
the home's DTM value at load time, so the absolute datum drops out
of the ray-march math). The DTM lets the card's LiDAR-aware ray-
march account for terrain slope around the home, instead of
assuming flat ground when checking whether a far obstacle blocks
the sun.

The whole raster is loaded into memory: a 1 km x 1 km tile at 1 m
pitch is ~ 4 MB as Float32 per band, so ~ 8 MB for the two-band
output. A 10 km x 10 km tile is ~ 800 MB across both bands. Both
fit comfortably on the VPS (~ 7 GB free RAM as of writing). Real-
world inputs the Helios card cares about sit in the lower end of
that range, so we keep the implementation simple instead of
streaming block-by-block via gdal.RasterIO. If we ever take
national-scale inputs, this is the module to swap out.

Negative heights (DSM lower than DTM, usually a sign of a noisy
point cloud near a building edge or a bathymetric tile) are clamped
to zero on band 1 so the downstream flood-fill in Helios doesn't
pick up spurious depressions as features. Band 2 (DTM) is left
unclamped, real ground elevation can sit below sea level and the
card needs the true value to compute relative slope.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from osgeo import gdal

NDSM_NODATA: float = -9999.0
#The DTM band's nodata sentinel. Far below any real-world ground
#elevation (Dead Sea -430 m is the lowest land surface), close
#enough to NDSM_NODATA to keep the on-disk encoding uniform.
DTM_NODATA:  float = -9999.0


def subtract(dsm_path: Path, dtm_path: Path, output_path: Path) -> None:
    """Write DSM - DTM as a Float32 GeoTIFF at `output_path`.

    Both inputs must share the same grid; `pipeline.validate.validate_pair`
    is the contract that guarantees that.
    """
    gdal.UseExceptions()

    dsm_ds = gdal.Open(str(dsm_path))
    dtm_ds = gdal.Open(str(dtm_path))

    dsm_band = dsm_ds.GetRasterBand(1)
    dtm_band = dtm_ds.GetRasterBand(1)

    dsm = dsm_band.ReadAsArray().astype(np.float32, copy=False)
    dtm = dtm_band.ReadAsArray().astype(np.float32, copy=False)

    if dsm.shape != dtm.shape:
        #Defensive: validate_pair should have caught this already.
        raise ValueError(
            f"DSM shape {dsm.shape} doesn't match DTM shape {dtm.shape}."
        )

    #Build a boolean mask of cells that either input flags as nodata.
    #The mask drives the final nDSM nodata pixels so we never produce
    #spurious zero-height cells from invalid input.
    dsm_nodata = dsm_band.GetNoDataValue()
    dtm_nodata = dtm_band.GetNoDataValue()
    mask = np.zeros(dsm.shape, dtype=bool)
    if dsm_nodata is not None:
        mask |= np.isclose(dsm, dsm_nodata)
    if dtm_nodata is not None:
        mask |= np.isclose(dtm, dtm_nodata)
    #NaN inputs (some COG-encoded float tiles use NaN for nodata)
    #also propagate to the mask.
    mask |= ~np.isfinite(dsm)
    mask |= ~np.isfinite(dtm)

    ndsm = (dsm - dtm).astype(np.float32, copy=False)
    np.maximum(ndsm, 0.0, out=ndsm)
    ndsm[mask] = NDSM_NODATA

    #Band 2: the DTM itself, in its source vertical datum. The card
    #subtracts the home's DTM value at load time so the absolute
    #reference cancels out of the ray-march; we just need the
    #SLOPE between any sample point and the home.
    dtm_out = dtm.astype(np.float32, copy=True)
    dtm_out[mask] = DTM_NODATA

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(
        str(output_path),
        xsize=dsm_ds.RasterXSize,
        ysize=dsm_ds.RasterYSize,
        bands=2,
        eType=gdal.GDT_Float32,
        options=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"],
    )
    out_ds.SetGeoTransform(dsm_ds.GetGeoTransform())
    out_ds.SetProjection(dsm_ds.GetProjection())

    out_band1 = out_ds.GetRasterBand(1)
    out_band1.SetNoDataValue(NDSM_NODATA)
    out_band1.SetDescription("nDSM (height above local ground, metres)")
    out_band1.WriteArray(ndsm)
    out_band1.FlushCache()

    out_band2 = out_ds.GetRasterBand(2)
    out_band2.SetNoDataValue(DTM_NODATA)
    out_band2.SetDescription("DTM (ground elevation, source vertical datum, metres)")
    out_band2.WriteArray(dtm_out)
    out_band2.FlushCache()

    out_ds.FlushCache()
    out_ds = None
