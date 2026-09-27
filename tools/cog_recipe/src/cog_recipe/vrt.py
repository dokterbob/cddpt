"""Hand-written GDAL VRT mosaic construction (no `osgeo` needed).

The design originally called for ``osgeo.gdal.BuildVRT()``, which needs a
system GDAL install with Python bindings. Those aren't reliably available to
a user who installs this tool via `uv`/pip -- `rasterio` wheels bundle their
own libgdal but not `osgeo` (see ``gdal_support.py``). GDAL's VRT format is
a public, simple XML schema, and the VRT *driver* that reads it is compiled
into every libgdal build (including rasterio's bundled one) -- writing the
XML directly and opening it with ``rasterio.open()`` gets the same seamless,
lazily-read mosaic that ``gdalbuildvrt`` would produce, entirely through
rasterio.

Only axis-aligned, non-rotated tiles on a shared pixel grid are supported
(guaranteed by :func:`cog_recipe.tileset.load_tile_set`), which keeps each
tile a single, whole-tile ``<SimpleSource>`` -- no partial source-rect
geometry to compute.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from cog_recipe.tileset import TileInfo


def build_mosaic_vrt(
    tiles: list[TileInfo],
    vrt_path: Path,
    *,
    crs_wkt: str,
    dtype: str,
    pixel_size_x: float,
    pixel_size_y: float,
    nodata: float,
) -> tuple[float, float, float, float]:
    """Write a mosaic VRT covering the union of ``tiles``.

    Returns the mosaic's ``(minx, miny, maxx, maxy)`` bounds. ``dtype`` must
    be a GDAL type name (e.g. ``"Float32"``, as `rasterio` reports it).
    """

    minx = min(t.bounds[0] for t in tiles)
    miny = min(t.bounds[1] for t in tiles)
    maxx = max(t.bounds[2] for t in tiles)
    maxy = max(t.bounds[3] for t in tiles)

    width = round((maxx - minx) / pixel_size_x)
    height = round((maxy - miny) / pixel_size_y)

    gdal_dtype = dtype[0].upper() + dtype[1:] if dtype[0].islower() else dtype

    lines = [
        f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">',
        f"  <SRS>{escape(crs_wkt)}</SRS>",
        # GDAL geotransform order: originX, pixelWidth, rowRot, originY, colRot, -pixelHeight
        f"  <GeoTransform>{minx}, {pixel_size_x}, 0.0, {maxy}, 0.0, {-pixel_size_y}</GeoTransform>",
        f'  <VRTRasterBand dataType="{gdal_dtype}" band="1">',
        f"    <NoDataValue>{nodata}</NoDataValue>",
        "    <ColorInterp>Gray</ColorInterp>",
    ]

    for tile in tiles:
        dx = round((tile.bounds[0] - minx) / pixel_size_x)
        dy = round((maxy - tile.bounds[3]) / pixel_size_y)
        source_path = escape(str(tile.path.resolve()))
        lines += [
            "    <SimpleSource>",
            f'      <SourceFilename relativeToVRT="0">{source_path}</SourceFilename>',
            "      <SourceBand>1</SourceBand>",
            f'      <SourceProperties RasterXSize="{tile.width}" RasterYSize="{tile.height}" '
            f'DataType="{gdal_dtype}" BlockXSize="{tile.width}" BlockYSize="{tile.height}"/>',
            f'      <SrcRect xOff="0" yOff="0" xSize="{tile.width}" ySize="{tile.height}"/>',
            f'      <DstRect xOff="{dx}" yOff="{dy}" xSize="{tile.width}" ySize="{tile.height}"/>',
            f"      <NODATA>{nodata}</NODATA>",
            "    </SimpleSource>",
        ]

    lines += ["  </VRTRasterBand>", "</VRTDataset>"]

    vrt_path.write_text("\n".join(lines), encoding="utf-8")
    return (minx, miny, maxx, maxy)
