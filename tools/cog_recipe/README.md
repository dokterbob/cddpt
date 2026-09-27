# `cddpt-build-cog`

A standalone companion tool for [`cddpt`](https://github.com/dokterbob/cddpt): a downstream
*usage* of its output, not part of the core package. Given a directory of plain-GeoTIFF
DTM/DSM tiles downloaded with `cddpt download` (e.g. `--collection MDT-50cm`), it re-encodes
them into a single, seamless, tiled, LERC-compressed Cloud-Optimized GeoTIFF (COG).

**Unofficial, independent tool. Not affiliated with, endorsed by, or supported by
Direção-Geral do Território.**

## Why this exists

A live sample tile (`MDT-50cm-194469-07-2025`) confirmed DGT's source rasters are single-band
Float32, 2000×2000 px (0.5 m pixels), **strip-encoded (not tiled)**, effectively uncompressed
(~19 MiB/tile). At ~92,000 tiles that's **~1.8 TB** for MDT-50cm alone, with no internal
tiling for efficient partial/range reads, and each 1 km tile is a separate file with no
knowledge of its neighbours.

## Mosaic-first, not per-tile-then-mosaic

The primary command is `build-cog merge`: it builds one seamless mosaic across *all* input
tiles first, then writes *one* COG from it. This is deliberate, not just convenient:

- A per-tile COG's own overview pyramid is built independently, decimating only that 1 km
  tile. Every overview level is a power of two of *that tile's* pixels, computed with no
  knowledge of the neighbouring tile. Downstream tools that read overviews (most rendering
  and, critically, contouring) then see a visible/measurable discontinuity at every former
  tile boundary.
- Merging first and decimating the *merged* raster removes this: an averaging window at a
  tile boundary spans real neighbouring pixels from both tiles, not a tile edge and nothing.
- `merge` therefore also guarantees an overview level at an exact, contour-friendly
  resolution (10 m by default, `--ensure-overview-res`) that GDAL's own automatic
  (power-of-2-only) overview builder can never produce for 0.5 m or 2 m native data.

Per-tile COG conversion (`build-cog convert`) is still available as a secondary command, for
the case where independently-servable per-tile COGs are themselves the wanted output (e.g.
serving individual tiles to a tile-aware client) -- but most users downloading an AOI should
reach for `merge` directly.

## The lossy trade-off (disclosed)

`merge`/`convert` both compress with **LERC_ZSTD**, `MAX_Z_ERROR=0.05` m by default -- half of
DGT's published 10 cm vertical-accuracy requirement. This bounds the *encoding* quantization
error uniformly across the whole elevation range (unlike, say, a float16 downcast, which
degrades to multi-metre error at high elevations such as Serra da Estrela). The native
Float32 dtype and CRS (EPSG:3763) are preserved exactly; only the on-disk *encoding* of the
elevation values is lossy, with a known, configurable bound (`--max-z-error`). This is
disclosed in `--help` on both commands.

The intermediate "base" GeoTIFF (`merge`'s temporary full-resolution mosaic, before COG
encoding) also uses LERC_ZSTD but with GDAL's default (lossless) `MAX_Z_ERROR`, so the only
place any precision is deliberately given up is the *final* COG write -- both the native-
resolution band and every overview are re-derived from the merged raster at that point, so
resampling error never compounds across pipeline stages.

## GDAL approach: rasterio's bundled GDAL, no `osgeo`, no system GDAL

The original design sketch called for `osgeo.gdal.BuildVRT()`/`gdal.Warp()`. On this
development machine there is **no system GDAL** (`gdalinfo`/`gdalbuildvrt`/`gdalwarp` are not
installed, no Homebrew `gdal` keg) and **no `osgeo` Python package** available. That is
expected to be the common case for a user who installs this tool via `uv`/`pip`: `rasterio`'s
PyPI wheels vendor their own libgdal build, but do **not** install `osgeo` bindings, and there
is no reliable guarantee of a system GDAL being present at all.

So this tool is built entirely on **`rasterio`'s bundled GDAL** (verified here: rasterio
1.5.1, bundled GDAL 3.12.4, confirmed to support `LERC_ZSTD` by actually writing a probe
file -- see `check_lerc_zstd_support()` in `gdal_support.py`, run as a preflight on every
command):

- **Mosaic ("VRT")**: no `osgeo.gdal.BuildVRT()` available, so `vrt.py` writes the GDAL VRT
  XML directly. VRT is a plain, public XML format, and the VRT *driver* that interprets it is
  compiled into every libgdal build, including rasterio's bundled one -- `rasterio.open()` on
  a hand-written `.vrt` gets exactly the same lazy, seamless, block-streamed mosaic view that
  `gdalbuildvrt` would produce. Because all of `cddpt`'s tiles sit on one shared, axis-aligned
  pixel grid (see `src/cddpt/tiles.py`), every source is a single whole-tile `<SimpleSource>`
  -- no partial source-rect geometry to compute.
- **Streamed copy / clip**: `rasterio.shutil.copy()` (a thin wrapper over GDAL's
  `CreateCopy`) streams the mosaic into a base GeoTIFF block-by-block at the C level -- the
  full mosaic is never materialized as one array. A `--bbox` clip instead does a manual
  block loop (`dataset.block_windows()`), bounded to one block's worth of memory at a time.
- **Overviews**: `Dataset.build_overviews(factors, Resampling.average)` -- GDAL's overview
  *builder* accepts arbitrary integer decimation factors (unlike the COG driver's own
  automatic overview generation, which is power-of-2-only); this is how the guaranteed 10 m
  level is produced (see "Overview pyramid" below).
- **Final COG encode**: `rasterio.shutil.copy(..., driver="COG", OVERVIEWS="FORCE_USE_EXISTING")`.
  Verified empirically (see `tests/`): with `OVERVIEWS=FORCE_USE_EXISTING`, GDAL's COG driver
  re-packs the file into proper COG layout using the overviews already built above, instead of
  discarding them and recomputing its own power-of-2-only set.

No step shells out to a GDAL CLI utility and no step imports `osgeo`. If a future `cddpt`
integration needs true reprojection (this tool never reprojects -- same CRS in and out), the
natural next step is `rasterio.vrt.WarpedVRT`, which is also pure-rasterio.

## Overview pyramid: guaranteeing an exact 10 m level

GDAL's COG driver only ever builds power-of-2 overviews. None of `2, 4, 8, 16, ...` lands on
10 m for 0.5 m or 2 m native data. `overviews.py`'s `compute_overview_factors()` instead:

1. Requires `--ensure-overview-res` to be an exact integer multiple of the native resolution
   (e.g. 10 m / 0.5 m = factor 20; 10 m / 2 m = factor 5) -- **fails fast with a clear error**
   if not (e.g. 3 m native -> 10 m has no integer factor), rather than silently landing on a
   nearby but wrong resolution.
2. Builds a power-of-2 pyramid *below* that factor, but **only the power-of-2 levels that
   evenly divide it** (for smooth zooming without sacrificing exactness -- see below), inserts
   the exact factor, then continues a power-of-2 pyramid *above* it (each an exact multiple of
   the guaranteed level), stopping once the smallest overview's longest side is
   `<= --blocksize` pixels.

E.g. 0.5 m native, `--ensure-overview-res 10` (factor 20, whose power-of-2 divisors are 2 and
4 -- 8 and 16 do not divide 20): factors `[2, 4, 20, 40, 80, ...]` -> resolutions
`[1, 2, 10, 20, 40, ...]` m. 2 m native (factor 5, prime -- no power-of-2 divisor besides 1,
so no intermediate zoom level below it): factors `[5, 10, 20, 40, ...]` -> resolutions
`[10, 20, 40, 80, ...]` m.

**Why only divisors, not every power of two below the factor**: GDAL's overview builder, when
asked for several levels in one call, cascades each new level from the nearest existing one
rather than always recomputing from the full-resolution source (a speed optimization).
Cascading over an *exact* integer ratio is mathematically identical to averaging directly from
the source (associativity of the mean over a uniform partition); cascading over a
non-integer ratio (e.g. building a factor-5 level from an existing factor-4 one) is a
different, approximate resampling. This was measured directly (see `tests/test_overviews.py`
and `tests/test_merge.py`): mixing in non-dividing power-of-2 levels introduced ~0.0077 m
error into a 10 m-equivalent test level, an order of magnitude past LERC quantization noise
and a silent violation of the "seamless, exactly averaged" guarantee this tool exists to make.
Restricting every step to an exact multiple of its predecessor keeps the *entire* pyramid --
not just the guaranteed level -- mathematically exact.

With `--bbox`, the clip is snapped outward to the `--ensure-overview-res` grid (`grid.py`'s
`snap_outward()`) so the 10 m level stays pixel-aligned to the same global grid regardless of
which AOI was clipped -- and, because that grid resolution is an integer multiple of the
native resolution, the native pixel grid stays aligned too.

## Usage

```console
$ build-cog merge INPUT_DIR OUTPUT.tif
$ build-cog merge INPUT_DIR OUTPUT.tif --bbox -30000,-110000,10000,-70000
$ build-cog merge INPUT_DIR OUTPUT.tif --ensure-overview-res 10 --max-z-error 0.05

$ build-cog convert INPUT_DIR OUTPUT_DIR   # secondary: per-tile COGs, 1:1 output tree
```

`merge --help` / `convert --help` show every option, including the lossy `MAX_Z_ERROR`
trade-off above. Key `merge` options:

| Option | Default | Meaning |
|---|---|---|
| `--pattern` | `*.tif` | Glob for input tiles |
| `--bbox minx,miny,maxx,maxy` | full extent | Clip (EPSG:3763), snapped to the overview grid |
| `--max-z-error` | `0.05` | LERC max error (m) for the final COG encode |
| `--nodata` | source | Override; `-999` with a warning if no source tile declares one |
| `--ensure-overview-res` | `10` | Guaranteed exact overview resolution (m); `0` disables |
| `--compress` | `lerc_zstd` | COG compression profile |
| `--blocksize` | `512` | Internal tile size (px) |
| `--overview-resampling` | `average` | Resampling for building overviews from the merged raster |
| `--keep-intermediate` | off | Keep the intermediate mosaic VRT + base GeoTIFF |
| `--gdal-threads` | `ALL_CPUS` | `GDAL_NUM_THREADS` for the streaming copy/encode steps |
| `--cache-mb` | `min(25% RAM, 8192)` | `GDAL_CACHEMAX` (MB) for the whole pipeline |
| `--pool-size` | env `GDAL_MAX_DATASET_POOL_SIZE` or `1000` | `GDAL_MAX_DATASET_POOL_SIZE` -- open source tiles GDAL keeps cached |
| `--tmp-dir` | OUTPUT's directory | Directory for the intermediate mosaic VRT + base GeoTIFF |
| `--force` | off | Skip the disk-space preflight check |
| `--scan-workers` | ~4x CPU count, capped at 32 | Thread-pool size for the initial tile scan |
| `--fast-scan` | off | Cross-validate CRS/dtype/pixel-size/rotation on a sample only |

See "National-scale runs" below for what these are for and how to size them for a
91,000-tile mainland-Portugal merge.

### National-scale runs

Mainland Portugal is ~91,202 tiles per collection: MDT-50cm is ~1.46 TB in (2000x2000 px,
0.5 m, mosaic ~1.12M x 0.44M px, ~490 Gpx); MDT-2m is ~95 GB in (~30 Gpx). At that scale,
GDAL's own defaults, sized for a handful of files, need raising -- and a single multi-hour
run benefits from being checked *before* it starts, not partway through.

**Recommended approach**:

- **MDT-2m**: the whole country in one `merge` call. ~30 Gpx is well within what a single
  streamed pass handles comfortably (see "Mosaic-first, not per-tile-then-mosaic" above --
  no full-mosaic array is ever held in memory regardless of extent).
- **MDT-50cm**: merge by region, not the whole country in one call. ~490 Gpx makes one run's
  peak-disk footprint and multi-hour-with-no-incremental-progress profile impractical; use
  `build-cog plan-regions` (below) to generate a set of `--bbox`-clipped `merge` invocations
  instead, one per region, run sequentially or in parallel across machines/disks.

**Tuning flags** (all have sane defaults; override only if the defaults don't fit your
machine):

- **`--pool-size`** (`GDAL_MAX_DATASET_POOL_SIZE`, default `1000`): a full-width 0.5 m block
  row touches 500+ source tiles at once (each a `<SimpleSource>` in the mosaic VRT); GDAL's
  own compiled-in default (~100) is well below that, risking open/close thrashing on the
  source tile handles. `merge` also checks the process's file-descriptor limit
  (`resource.getrlimit(RLIMIT_NOFILE)`) and raises the soft limit toward the hard limit if
  `--pool-size` needs more headroom than it currently has, with a clear warning printed (and
  a Python `UserWarning`) if it can't get enough.
- **`--cache-mb`** (`GDAL_CACHEMAX`, default `min(25% of detected RAM, 8192)` MB): GDAL's
  block cache across the whole pipeline. RAM is detected via `os.sysconf` (no `psutil`
  dependency); if it can't be detected, a conservative 4096 MB fallback is used.
- **`BIGTIFF`**: automatic -- `YES` once the estimated *uncompressed* mosaic exceeds ~2 GB,
  `IF_SAFER` (GDAL decides) below that. A 0.5 m national mosaic is ~2 TB uncompressed, well
  past the classic-TIFF 4 GiB offset ceiling.
- **Disk-space preflight**: before any tile data is written, `merge` estimates peak disk
  usage -- the intermediate base GeoTIFF plus the final COG (with its overview pyramid),
  both as *uncompressed* upper bounds (real LERC_ZSTD usage is normally far lower) -- against
  free space on the output and, if `--tmp-dir` is given, the temp filesystem. It fails fast
  with a clear message if the estimate exceeds free space; pass `--force` to proceed anyway.
  Use `--tmp-dir` to put the intermediate base GeoTIFF on a different disk than the final
  output, splitting the requirement across two filesystems instead of stacking it on one.
- **`--scan-workers`/`--fast-scan`**: the initial tile scan opens every input file to read
  its georeferencing. Sequentially that's ~150 files/s (~10 min for 91k tiles) --
  `load_tile_set` now does this in a thread pool by default (`rasterio.open()` releases the
  GIL for its GDAL call), sized to ~4x CPU count. `--fast-scan` additionally skips the
  CRS/dtype/pixel-size/rotation cross-check on all but a sample (~200 tiles), trusting the
  rest to share the sampled tiles' grid -- only safe for a directory of consistently-named,
  already-trusted DGT tiles. Measured on this development machine (fast local NVMe/APFS,
  synthetic tiles): the thread pool gave **no measurable speedup over sequential** here,
  because file-open latency on this disk is already near zero -- the ~150 tiles/s figure is
  CPU/GIL-bound, not I/O-wait-bound, on this hardware. With an artificial 5 ms per-open
  latency injected (closer to a network-mounted or spinning-disk source), the same thread
  pool gave a measured **~2x speedup** at 16-32 workers. In other words: parallelising the
  scan is close to free insurance -- it costs nothing on fast local storage and pays off on
  slower ones -- but isn't guaranteed to help on every machine. `--fast-scan` still trims
  the fixed per-tile Python-side cost regardless of storage speed.

**`build-cog plan-regions`**: prints `build-cog merge --bbox ...` commands tiling an input
directory's extent into `--region-grid KM`-ish squares, each snapped to the
`--ensure-overview-res` grid (via `grid.py`'s `snap_outward`) so every region's COG stays
pixel- and overview-aligned with its neighbours:

```console
$ build-cog plan-regions INPUT_DIR --region-grid 50 --ensure-overview-res 10 \
    --output-dir OUTPUT_DIR > run_regions.sh
$ sh run_regions.sh   # or hand out lines to separate machines/processes
```

This only prints the commands -- it does not run them, and does not merge the resulting
per-region COGs back into one file (there is no "merge of merges" step; each region's COG is
the final deliverable for that region).

### Reading the 10 m level with `gdal_contour`

`gdal_contour` reads a specific overview level via the `OVERVIEW_LEVEL` open option (0 = the
first/coarsest-requested overview here, since `--ensure-overview-res 10` is the *first*
non-power-of-2 level after the sub-10m pyramid in a typical 0.5 m/2 m run -- check
`gdalinfo OUTPUT.tif` for the exact index, since it depends on native resolution):

```console
$ gdalinfo OUTPUT.tif | grep -A1 "Overviews:"
$ gdal_contour -a elev -i 10 \
    "OUTPUT.tif,OVERVIEW_LEVEL=<index-of-the-10m-level>" \
    contours_10m.gpkg
```

Equivalently, resample the full-resolution COG down to a plain 10 m GeoTIFF first (heavier,
but avoids hunting for the right `OVERVIEW_LEVEL` index):

```console
$ gdalwarp -tr 10 10 -r near -tap OUTPUT.tif mosaic_10m.tif
$ gdal_contour -a elev -i 10 mosaic_10m.tif contours_10m.gpkg
```

(`-r near` here is a pure decimation of an *already-averaged* 10 m overview band, not a
second average pass -- the seamless averaging already happened when `merge` built that
overview from the full-resolution merged raster.)

## Install / run

This tool is **not** part of the root `cddpt` project's dependency set or lockfile -- see
"Deviations from docs/build-cog.md" below. Run it via its own `uv` project:

```console
$ uv run --project tools/cog_recipe build-cog merge INPUT_DIR OUTPUT.tif
```

Or install it as a standalone `uv` tool, from a Git checkout:

```console
$ uv tool install 'cddpt-build-cog @ git+https://github.com/dokterbob/cddpt#subdirectory=tools/cog_recipe'
$ build-cog merge INPUT_DIR OUTPUT.tif
```

Verified locally (equivalent local-path form, into a scratch `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR`
so it doesn't touch the real user tool environment):

```console
$ UV_TOOL_DIR=/tmp/uvtools UV_TOOL_BIN_DIR=/tmp/uvtools/bin \
    uv tool install ./tools/cog_recipe
$ /tmp/uvtools/bin/build-cog --help
```

## Tests

```console
$ uv run --project tools/cog_recipe pytest
```

Synthetic Float32 GeoTIFF tiles (small, strip-encoded like DGT's real output) are generated
on the fly in `tests/conftest.py` -- no network access, no credentials, no real DGT data.

**Deviation from docs/build-cog.md**: tests live in `tools/cog_recipe/tests/`, not the root
`tests/`, and this project has its own `pyproject.toml`/lockfile/dev dependency group
entirely separate from the root `cddpt` project's. This keeps the two test suites and their
dependencies independent -- the root suite never needs `rasterio`/`rio-cogeo`/GDAL, and this
tool's tests never need `pystac`/`shapely`/etc.

## Deviations from `docs/build-cog.md`

The design doc describes an earlier plan (per-tile-first, then a separate `mosaic-10m`
resample step, via `osgeo.gdal`). Implementation followed later course corrections instead:

1. **Merge-first is now the primary command**, not per-tile-then-mosaic. `build-cog merge`
   builds a seamless mosaic and writes one native-resolution COG directly, with a guaranteed
   overview at `--ensure-overview-res` (default 10 m, built with `average` resampling on the
   *merged* raster so that level has no tile-boundary seams). The doc's `mosaic-10m` (a
   separate resampled-down-to-10m output, invoked only after a full `convert` pass) is
   superseded by this single command producing both the native band and the 10 m guarantee in
   one pass. `build-cog mosaic-10m` is kept as a deprecated, hidden CLI alias for `merge` for
   anyone scripting against the doc's original command name.
2. **No `osgeo`/system GDAL anywhere.** Every step goes through `rasterio`'s bundled GDAL
   (see "GDAL approach" above): a hand-written VRT XML instead of
   `osgeo.gdal.BuildVRT()`/`gdal.Warp()`, `rasterio.shutil.copy()` for streamed
   copy/clip/COG-encode, and `Dataset.build_overviews()` with explicit factors instead of
   `gdal.Warp(resampleAlg="average")`.
3. **Per-tile `convert` is secondary and deliberately minimal**: sequential (no
   `ProcessPoolExecutor`/worker-pool concurrency, no `--workers`/`--gdal-threads`-per-worker
   knobs from the original CLI surface), since the primary deliverable users actually want is
   the merged COG. Resume-by-existence, atomic `.tmp`, `_failed.csv`/`_summary.json` are kept.
4. **Core pipeline is CLI/progress-library-agnostic by design**: `merge.py`/`convert.py`/etc.
   take no dependency on `typer`/`tqdm` and use a plain `ProgressCallback` protocol
   (`progress.py`) instead; `cli.py` is a thin wrapper. This is meant to make a later
   `cddpt build-cog` subcommand (or optional `cddpt[cog]` extra) a drop-in, not a rewrite.
5. **Own `src/` layout** (`tools/cog_recipe/src/cog_recipe/...`) and its own
   `pyproject.toml`/lockfile/`tests/` directory, matching the constraint to keep this tool
   fully independent of the root project's dependencies and lockfile -- rather than the flat
   `tools/cog_recipe/*.py` file list the doc originally sketched.
6. **Pre-guaranteed-level power-of-2 overviews are restricted to exact divisors of the
   guaranteed factor**, not every power of two below it (see "Overview pyramid" above) --
   discovered empirically while implementing, not something either the original doc or the
   course-correction anticipated. Building an arbitrary mix of factors in one
   `Dataset.build_overviews()` call lets GDAL cascade a non-dividing level from an
   already-built one instead of the full-resolution source, silently breaking the
   "seamless, exactly averaged" guarantee for that level.
