# QGIS plugin design (Milestone 7)

Thin adapter over the published `cddpt` package, built last — only once 0.1.0+ is stable on
PyPI. See [PLAN.md](PLAN.md) for the core library design.

- **Two `QgsProcessingAlgorithm`s** (search, download) under a Processing provider — gives
  AOI-from-canvas/layer, model-builder composability, and batch mode for free; no bespoke
  dialog beyond an auth-status widget.
- **Dependency delivery: guided `pip install`, not vendoring.** QGIS's `plugin_dependencies`
  metadata field covers QGIS plugins only, and vendoring compiled packages like
  `shapely`/`geopandas` risks ABI conflicts with QGIS's bundled GDAL/shapely/pyproj stack.
  `bootstrap.py` checks for `cddpt` via `importlib.util.find_spec`; on failure it shows a
  dialog offering a one-click `pip install --user cddpt` via `QgsTask` — **never silent, never
  at startup**. `requests` + `BeautifulSoup4` (instead of Playwright) keep this light: no
  browser binary, no `playwright install` step.
- The core dependency list is deliberately narrow (`pystac-client`, `shapely`, `requests`,
  `beautifulsoup4`, `truststore`, `keyring`, `platformdirs`, `pydantic-settings`, `tenacity`,
  `pyrate-limiter`); `geopandas`/`pyogrio`/`typer`/`tqdm` stay extras since the plugin reads
  AOIs via `QgsVectorLayer` → shapely WKB.
- **Auth shares the CLI's `keyring` entry** rather than `QgsAuthManager`: there is no real
  token flow (only a session cookie obtained by POSTing the login form), so bending
  `QgsAuthManager`'s OAuth2/Bearer API around it would be a misuse, and would give CLI and
  plugin divergent cached sessions — the exact drift that hurt the old plugin. The password
  field is masked, writes straight to `keyring`, and is never rendered back.
- **Every core call runs inside a `QgsTask`** so the GUI thread is never blocked; the
  downloader's injected progress-callback Protocol drives `feedback.setProgress()`.
- `qgisMinimumVersion=3.34` (Python ≥3.10 needed for `truststore`/`pystac-client`).
- "About" states: *Unofficial, independent client. Not affiliated with, endorsed by, or
  supported by Direção-Geral do Território.*
- Submit to the QGIS plugin repository once stable.

## Verification

Run both Processing algorithms against a small canvas extent inside QGIS; confirm progress
reporting doesn't block the UI and downloaded rasters can be auto-loaded.
