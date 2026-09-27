# Companion tool: `build-cog` — seamless COG re-encoder

A separate, standalone deliverable: a downstream *usage* of `cddpt`'s output, not part of the
core package. Given a directory of downloaded plain-GeoTIFF DTM/DSM tiles (e.g. from
`cddpt download --collection MDT-50cm`), merges them into **one** seamless, LERC-compressed
Cloud-Optimized GeoTIFF (COG) at native resolution, with an overview pyramid guaranteed to
contain an exact, contour-friendly resolution (10 m by default). Pure local-file processing —
no auth, no network, no AOI logic. Lives in `tools/cog_recipe/` as its own `uv` project,
independent of the root `cddpt` dependency set and lockfile.

Full pipeline write-up, rationale, and design detail: **[tools/cog_recipe/README.md](../tools/cog_recipe/README.md)**.
This page is the short version plus the facts that motivated the design.

## Why re-encode

A live sample tile (`MDT-50cm-194469-07-2025`) confirmed DGT's source rasters are single-band
Float32, 2000×2000 px (0.5 m pixels), **strip-encoded (not tiled)**, effectively uncompressed
(~19 MiB/tile). At ~92,000 tiles that's **~1.8 TB** for MDT-50cm alone, with no internal
tiling for efficient partial/range reads, and each 1 km tile is a separate file with no
knowledge of its neighbours.

## Merge first, then encode — not per-tile-then-mosaic

A per-tile COG's overview pyramid is built independently per 1 km tile, with no knowledge of
neighbouring tiles — every overview level bakes in a visible/measurable discontinuity at each
former tile boundary (a problem for rendering and, critically, for contouring). Merging all
tiles into one mosaic first and decimating the *merged* raster removes this: an averaging
window at a tile boundary spans real neighbouring pixels from both tiles. So the primary
command, `build-cog merge`, builds a seamless mosaic across all input tiles, writes one COG
from it at native resolution, and guarantees an overview level at an exact resolution as part
of the same pass (see "Overview pyramid" below). Per-tile conversion (`build-cog convert`) is
kept as a secondary command for the case where independently-servable per-tile COGs are
themselves the wanted output.

## The lossy trade-off (disclosed)

Both commands compress with **LERC_ZSTD**, `MAX_Z_ERROR=0.05` m by default — half of DGT's
published 10 cm vertical-accuracy requirement. This bounds the *encoding* quantization error
uniformly across the whole elevation range (unlike, say, a float16 downcast, which degrades to
multi-metre error at high elevations such as Serra da Estrela). Native Float32 dtype and CRS
are preserved exactly; only the on-disk *encoding* is lossy, with a known, configurable bound
(`--max-z-error`). Disclosed in `--help` on both commands.

## Pipeline (`merge`)

Built entirely on **rasterio's bundled GDAL** — no `osgeo`, no system GDAL required (rasterio
wheels vendor their own libgdal; a preflight checks it supports LERC_ZSTD):

1. Scan and validate the input tiles form one mosaicable grid.
2. Write a mosaic as hand-written GDAL VRT XML over their union (or a `--bbox` clip, snapped
   to the overview grid) — a lazy, seamless view; no pixel data read yet.
3. Stream-copy the VRT into a base GeoTIFF at native resolution (`rasterio.shutil.copy`, or a
   bounded block loop when clipping) — the full mosaic is never materialized as one array.
4. Build overviews on the merged raster (`Dataset.build_overviews`, `average` resampling) with
   an explicit factor list that includes the guaranteed resolution.
5. Encode the final COG (`driver="COG", OVERVIEWS=FORCE_USE_EXISTING"`), re-packing the
   already-built overviews instead of discarding and recomputing them.

## Overview-factor rule

GDAL's COG driver only ever builds power-of-2 overviews, none of which lands on 10 m for 0.5 m
or 2 m native data. `--ensure-overview-res` must be an exact integer multiple of the native
resolution (fails fast otherwise); the pyramid below it is restricted to the power-of-2 levels
that **evenly divide** that factor. This restriction was discovered empirically: GDAL's
overview builder cascades each new level from the nearest already-built one rather than always
from the full-resolution source, so cascading over a non-integer ratio (e.g. a factor-5 level
built from an existing factor-4 one) introduces real error (~0.0077 m measured on a 10 m test
level) — an order of magnitude past LERC quantization noise, silently breaking the "seamless,
exactly averaged" guarantee. Restricting every step to an exact multiple of its predecessor
keeps the whole pyramid, not just the guaranteed level, mathematically exact.

- **0.5 m native**, `--ensure-overview-res 10` (factor 20; divisors 2, 4 — 8 and 16 do not
  divide 20): factors `[2, 4, 20, 40, 80, ...]` → resolutions `[1, 2, 10, 20, 40, ...]` m.
- **2 m native** (factor 5, prime — no divisor besides 1): factors `[5, 10, 20, 40, ...]` →
  resolutions `[10, 20, 40, 80, ...]` m.

## CLI surface (as implemented)

```
build-cog merge INPUT_DIR OUTPUT.tif
  --pattern TEXT [*.tif]  --bbox minx,miny,maxx,maxy  --max-z-error FLOAT [0.05]
  --nodata FLOAT [source; -999 if unset]  --ensure-overview-res FLOAT [10.0]
  --compress TEXT [lerc_zstd]  --blocksize INT [512]  --overview-resampling TEXT [average]
  --keep-intermediate/--no-keep-intermediate [off]  --gdal-threads TEXT [ALL_CPUS]
  --cache-mb INT [min(25% RAM, 8192)]  --pool-size INT [env GDAL_MAX_DATASET_POOL_SIZE or 1000]
  --tmp-dir PATH [OUTPUT's directory]  --force [off]
  --scan-workers INT [~4x CPU count, capped at 32]  --fast-scan [off]

build-cog convert INPUT_DIR OUTPUT_DIR   # secondary: per-tile COGs, 1:1 output tree
  --pattern TEXT [*.tif]  --max-z-error FLOAT [0.05]  --compress TEXT [lerc_zstd]
  --blocksize INT [512]  --overview-resampling TEXT [average]  --nodata FLOAT [source]
  --resume/--force [resume]  --log-file PATH  --report-file PATH

build-cog plan-regions INPUT_DIR --region-grid KM   # prints per-region `merge` commands
  --pattern TEXT [*.tif]  --ensure-overview-res FLOAT [10.0]  --output-dir PATH [regions]
```

Verify directly against the installed tool: `uv run --project tools/cog_recipe build-cog --help`,
`... build-cog merge --help`, `... build-cog convert --help`.

## National-scale runs

Mainland Portugal is ~91,202 tiles per collection: MDT-50cm ~1.46 TB in (mosaic ~1.12M x
0.44M px, ~490 Gpx), MDT-2m ~95 GB in (~30 Gpx). **Recommended approach**: MDT-2m — the
whole country in one `merge` call (well within a single streamed pass, see "Pipeline"
above). MDT-50cm — merge by region, not the whole country at once (`build-cog plan-regions
INPUT_DIR --region-grid 50` prints a `--bbox`-clipped `merge` invocation per region, snapped
to the overview grid so regions stay pixel-aligned; run them sequentially or in parallel).

At this scale, GDAL's own defaults (sized for a handful of files) need raising, and a
multi-hour run benefits from failing fast rather than partway through:

- `--pool-size` (`GDAL_MAX_DATASET_POOL_SIZE`, default 1000): a full-width 0.5 m block row
  touches 500+ source tiles at once, above GDAL's own ~100 default. The process's
  file-descriptor limit is checked (`resource.getrlimit`) and raised toward its hard limit if
  needed, with a warning if that's not enough headroom.
- `--cache-mb` (`GDAL_CACHEMAX`, default `min(25% of detected RAM, 8192)` MB, via
  `os.sysconf` — no `psutil`).
- `BIGTIFF` is automatic: `YES` once the estimated uncompressed mosaic exceeds ~2 GB (a 0.5 m
  national mosaic is ~2 TB uncompressed), `IF_SAFER` below that.
- A disk-space preflight estimates peak usage (intermediate base + final COG, both
  *uncompressed* upper bounds) against free space before writing anything, failing fast
  unless `--force`; `--tmp-dir` places the intermediate on a different filesystem.
- The initial tile scan (opening all 91k files to read georeferencing — sequentially ~150
  files/s, ~10 min) now runs in a thread pool by default (`rasterio.open()` releases the
  GIL); `--fast-scan` additionally validates CRS/dtype/pixel-size/rotation on a sample only,
  for trusted, consistently-named DGT tile sets. Measured on synthetic tiles: no speedup over
  sequential on this dev machine's fast local disk (already near-zero open latency), but
  ~2x at 16-32 workers with an artificial 5 ms per-open latency injected (closer to a
  network-mounted source) — see `tools/cog_recipe/README.md` "National-scale runs" for the
  full write-up and numbers.

## Install / run

Not part of the root `cddpt` project's dependencies or lockfile — its own `uv` project:

```console
$ uv run --project tools/cog_recipe build-cog merge INPUT_DIR OUTPUT.tif
```

Or as a standalone `uv` tool from a Git checkout:

```console
$ uv tool install 'cddpt-build-cog @ git+https://github.com/dokterbob/cddpt#subdirectory=tools/cog_recipe'
$ build-cog merge INPUT_DIR OUTPUT.tif
```

## Verification

Synthetic Float32 GeoTIFF tiles (small, strip-encoded like DGT's real output, generated
on the fly, no network/credentials) exercise the pipeline in `tools/cog_recipe/tests/` —
including the empirically-measured overview-cascade error above. Run with
`uv run --project tools/cog_recipe pytest`. A real-tile run against DGT output (assert output
dimensions/CRS/nodata, `cog_validate()` passes, pixel diff against source within
`MAX_Z_ERROR`) is still pending — see `docs/roadmap.md`.

## Future: integrate into cddpt

The core pipeline (`merge.py`/`convert.py`/etc.) takes no dependency on `typer`/`tqdm`; it
exposes a plain `ProgressCallback` protocol instead, with `cli.py` as a thin wrapper. This is
meant to make a later `cddpt build-cog` subcommand (or an optional `cddpt[cog]` extra) a
drop-in, not a rewrite.
