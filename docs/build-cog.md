# Companion tool: `build-cog` — batch COG re-encoder

A separate, standalone deliverable: a downstream *usage* of `cddpt`'s output, not part of the
core package. Given a directory of downloaded plain-GeoTIFF DTM/DSM tiles (e.g. from
`cddpt download --collection MDT-50cm`), re-encode them into tiled, LERC-compressed
Cloud-Optimized GeoTIFFs. Pure local-file processing — no auth, no network, no AOI logic.
Lives in `tools/cog_recipe/`; its Typer command could later be registered on cddpt's CLI
with no redesign.

**Why re-encode**: a live sample tile (`MDT-50cm-194469-07-2025`) confirmed DGT's source
rasters are single-band Float32, 2000×2000 px (0.5 m pixels), **strip-encoded (not tiled)**,
effectively uncompressed (~19.1 MiB/tile). At ~92,000 tiles that's **~1.8 TB** for MDT-50cm
alone, with no internal tiling for efficient partial/range reads.

**The lossy step is deliberate and disclosed**: LERC with `MAX_Z_ERROR=0.05` m — half of
DGT's published 10 cm vertical-accuracy requirement — bounds quantization error uniformly
across the elevation range (unlike a float16 downcast, which degrades to ~2 m error at Serra
da Estrela elevations). Native Float32 dtype preserved; only the *encoding* is lossy, with a
known bound.

## Stage 1 — per-tile COG conversion

- **`rio-cogeo`** Python API (`cog_translate` / `cog_validate`), not a `gdal_translate`
  subprocess. Built-in `"lerc_zstd"` profile (`cog_profiles.get("lerc_zstd")`) sets
  `COMPRESS=LERC_ZSTD`, tiling, 512×512 blocks; `MAX_Z_ERROR` merged in as an extra creation
  option. **Override two defaults**: `overview_resampling="average"` (default `"nearest"` is
  wrong for continuous elevation) and `use_cog_driver=True` (GDAL's single-pass COG driver).
  Preserve source `nodata` (default to `-999` only if unset, with a logged warning).
- **Concurrency**: stdlib `ProcessPoolExecutor`, default `workers = cpu_count() - 1`,
  `GDAL_NUM_THREADS=1` per worker; both flags, with a documented "keep
  `workers × gdal-threads` near `cpu_count()`" guardrail.
- **Resumability = file existence at final path**, no manifest. Write `<dest>.tmp` in the
  same directory, validate, then atomically `os.replace` — "exists at final path" always
  implies "validated". Resume pre-scan filters the to-do list and clears stray `.tmp` orphans.
- **Error handling**: per-tile `try/except` returns `Result(ok=False, error=...)`; failures
  appended immediately to `_failed.csv` (`source_path,error,timestamp`) and retried on the
  next run. Pre-flight check that the local GDAL supports `LERC_ZSTD` — fail fast with one
  message instead of 92,000.
- **Validation** (gates the atomic rename): `cog_validate()` plus width/height/CRS/nodata
  round-trip check against the source via `rasterio`.
- **Output layout** mirrors the input tree 1:1.
- **Reporting**: `tqdm` over `as_completed()`; final `_summary.json` with
  processed/skipped/failed counts and measured total input vs. output bytes.

## Stage 2 — country-wide 10 m mosaic for contour generation

Per-tile overviews are power-of-2 decimations (1/2/4/8/16 m — none is 10 m) and are decimated
*per 1 km tile*, baking seams into contours at every tile boundary. So: **seamless mosaic
first, then resample once**.

- **Size**: mainland bbox (~561 km × ~218 km) at 10 m ≈ 1.22 G pixels ≈ **~4.9 GB Float32** —
  practical on a workstation. **No chunking**; only an optional `--bbox` clip for constrained
  test machines.
- **Step 1**: `osgeo.gdal.BuildVRT()` (a deliberate, narrow exception to preferring rasterio)
  over the **Stage-1 COG outputs** (their tiling and overviews make windowed reads cheap).
- **Step 2**: `osgeo.gdal.Warp()` with `xRes=yRes=10`, `targetAlignedPixels=True`,
  **`resampleAlg="average"`** (integrates all 20×20 source pixels per cell), same nodata, no
  reprojection. The VRT lets each averaging window span tile boundaries — this removes seams.
- **Step 3**: COG-encode via Stage 1's shared `_write_cog` helper (same `lerc_zstd` profile,
  same `MAX_Z_ERROR=0.05` default, exposed as its own flag).
- **README** documents the intended consumer —
  `gdal_contour -a elev -i 10 mosaic_10m_cog.tif contours_10m.gpkg` — and why mosaic-first
  (seams) and why 10 m.

## CLI surface

```
build-cog convert INPUT_DIR OUTPUT_DIR
  --pattern TEXT [*.tif]  --max-z-error FLOAT [0.05]  --compress TEXT [lerc_zstd]
  --overview-resampling TEXT [average]  --blocksize INT [512]  --nodata FLOAT [source]
  --workers INT [cpu_count()-1]  --gdal-threads INT [1]
  --resume/--force [--resume]  --validate/--no-validate [--validate]
  --log-file PATH [OUTPUT_DIR/_failed.csv]  --report-file PATH [OUTPUT_DIR/_summary.json]

build-cog mosaic-10m INPUT_DIR OUTPUT_FILE
  --pattern TEXT [*.tif]  --resolution FLOAT [10.0]  --resampling TEXT [average]
  --max-z-error FLOAT [0.05]  --nodata FLOAT [-999]
  --warp-threads TEXT [ALL_CPUS]  --warp-memory-mb INT [2048]
  --keep-intermediate/--no-keep-intermediate [--no-keep-intermediate]
```

## Files

```
tools/cog_recipe/__init__.py
tools/cog_recipe/build_cog.py     # scan_tiles, _write_cog (shared by both stages), convert
tools/cog_recipe/mosaic_10m.py    # build_vrt, resample_to_10m, mosaic-10m
tools/cog_recipe/README.md        # usage + lossy-tradeoff and mosaic-first rationale
tests/test_build_cog.py           # resume-skip, atomic-rename-on-failure, validation catching
                                  # a deliberately mismatched CRS/nodata fixture
```

Dependencies: `rio-cogeo>=7.0`, `rasterio>=1.3.3`, `tqdm`, `typer`; local GDAL must support
`LERC_ZSTD` (checked at startup).

## Verification

Before full-country scale, run both stages against `MDT-50cm-194469-07-2025.tif`: assert
output 2000×2000, `nodata == -999`, CRS == EPSG:3763, `cog_validate()` passes; then diff
pixel values against the source (`numpy.abs(diff).max() <= 0.05 + epsilon`) to confirm
`MAX_Z_ERROR` is honored end-to-end.
