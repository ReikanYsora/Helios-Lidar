# LiDAR data sources

This file powers the "Where do I download LiDAR data for my country?"
section of [helios-lidar.org](https://helios-lidar.org). Each entry
points users at an open national portal where they can grab the raw
LAZ point cloud or the DSM + DTM raster pair that the pipeline then
converts to a Helios-ready nDSM Cloud-Optimized GeoTIFF.

This list is community-maintained. To add a country, edit the
`## Sources` block below and open a pull request, the
[PR template](./.github/PULL_REQUEST_TEMPLATE.md) walks you through
the format and asks for a screenshot of a successful conversion as
proof the source actually feeds the pipeline.

## Sources

* **France**, [IGN HD France](https://geoservices.ign.fr/lidarhd).
  Tile picker at
  [geoservices.ign.fr/services-page-lidar-hd](https://geoservices.ign.fr/services-page-lidar-hd).
* **Switzerland**, [swissSURFACE3D](https://www.swisstopo.admin.ch/en/geodata/height/surface3d.html).
* **Netherlands**, [AHN](https://www.ahn.nl/).
* **Spain**, [IGN PNOA-LiDAR](https://centrodedescargas.cnig.es/CentroDescargas/buscador-de-productos?productoCNIG=LIDAR).
* **UK (England)**, [Environment Agency LiDAR Composite](https://environment.data.gov.uk/survey).
* **USA**, [USGS 3DEP – National Map Downloader](https://apps.nationalmap.gov/downloader/).
  Free, no registration required. Select "Elevation Source Data (3DEP)" in the dataset filter to find LAZ point clouds and DSM/DTM rasters.
* **Other countries**,
  [OpenTopography](https://opentopography.org/) aggregates public
  LiDAR worldwide, otherwise check your national geoportal.
