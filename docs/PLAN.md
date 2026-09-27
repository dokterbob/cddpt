# cddpt — Design Plan

> **Licensing decision (supersedes the original plan text):** the project is licensed
> **AGPL-3.0-or-later** (see `LICENSE`). Wherever older text said GPL-3.0-or-later, read
> AGPL-3.0-or-later.

## Context

`portugal3d.dgterritorio.gov.pt` is a bespoke Three.js LiDAR viewer with a private,
non-standard streaming protocol — not something QGIS can consume directly. The official
distribution channel for this data is DGT's (Direção-Geral do Território) **CDD – Centro de
Dados** portal (`cdd.dgterritorio.gov.pt`), whose web UI is tedious for bulk/AOI-driven
downloads (manual map clicks, 200 km² selection cap, 2-item cart limit).

An existing community QGIS plugin (`qgispt/dgtcd_downer`, GPLv2) automates this but has
serious problems:

- It **unconditionally disables TLS certificate verification**. Root cause of the problem it
  worked around: three DGT hosts (`ogcapi`, `infogeo`, `www`) migrated from Sectigo/GÉANT to
  the **HARICA TLS RSA Root CA 2021** in late 2025, a legitimate but newer root that can be
  missing from stale/vendored trust stores (e.g. QGIS's bundled Python on Windows) — not a
  broken server. All relevant hosts verify cleanly today with an up-to-date trust store.
- Auth is done by scraping the Keycloak login HTML form with a fragile hand-rolled parser;
  the password is stored in a plaintext QGIS dialog field.
- Three duplicated, drifting implementations.
- Hardcoded collection list and un-paginated search (`limit: 1000`) that silently truncated
  results.

Goal: a clean-room rewrite — a **pip-installable core library**, a **thin CLI** over it
(PyPI candidate), and a **QGIS plugin as a second thin adapter** — prioritizing correctness
and renowned maintained libraries over minimizing dependencies.

## Package

- **Name:** `cddpt` ("CDD Portugal", no implied DGT affiliation).
- **License:** AGPL-3.0-or-later.
- **Clean-room discipline:** implemented from this plan and live API probing only — never
  from `dgtcd_downer`'s source (not copied, not read for implementation purposes).
- Every artifact (README, `--help`, plugin "About") states: *"Unofficial, independent client.
  Not affiliated with, endorsed by, or supported by Direção-Geral do Território."*

## Key findings from live API probing (ground truth)

**The API** is a hybrid OGC API Features / STAC API at `https://cdd.dgterritorio.gov.pt/dgt-be/v1`:
- `GET /collections`, `GET /collections/{id}` — response is **envelope-wrapped**:
  `{"status":200,"message":"OK","data":{...actual STAC payload...}}`.
- `GET /collections/{id}/items?bbox=...` — also envelope-wrapped.
- **`POST /v1/search`** — **bare, spec-shaped STAC response**, fully anonymous, supports
  native STAC Item Search params (`bbox`, `intersects`, `collections`, `datetime`) *and*
  `cql2-json` filters. Real STAC `links` array with `rel: "next"` + POST `body` for
  pagination — confirmed correct across 8 pages / 40 items, 0 duplicates, terminates cleanly.
  Hard server-side cap of **10,000 items/page** (matches `pystac-client`'s own ceiling).
- Root (`/`), `/conformance`, `/queryables` all **302 to login** — so
  `pystac_client.Client.open()` cannot be used, but
  `pystac_client.ItemSearch(url=".../search", method="POST", ...)` used **standalone** works
  directly against the one clean endpoint.
- The catalog has **~21+ collections** (incl. Sentinel-2 mosaics, orthophotos, etc.) —
  collections must be discovered from `/collections` at runtime, never hardcoded, and every
  search must pass an explicit `collections=[...]` filter.
- Known downloadable LiDAR-derived collections: `LAZ`, `MDT-2m`, `MDT-50cm`, `MDS-2m`, `MDS-50cm`.
- **`item.bbox` is corrupted for raster collections** from `/search` — it's actually a
  serialized GDAL geotransform `(originX, pixelWidth, rowRot, originY, colRot, pixelHeight)`,
  not a bounding box (LAZ items are unaffected; `/items` returns correct bboxes for the same
  data). **Always derive geometry/bounds from `item.geometry`** (correct WGS84 everywhere) and
  sanitize/strip `bbox` before it reaches shapely or the user.
- Asset keys vary per collection (`assets["data"]` for LiDAR products, `assets["visual"]` for
  orthophotos) — **select by `roles` containing `"data"`**, never hardcode a key.
- Real download sizes (`properties["file:size"]`): **LAZ ≈ 342 MB/tile → ~30 TB for mainland
  Portugal**; MDT-50cm ≈ 16 MB/tile → ~1.4 TB; MDT-2m/MDS-2m ≈ 1 MB/tile → ~89 GB each.
  Preflight size totals + free-disk check + `--dry-run` are load-bearing features.
- The website's 200 km² AOI cap is **client-side UI policy only** — a full-mainland
  (~89,000 km²) single search succeeded server-side. AOI chunking is a
  *performance/robustness optimization* (a full-country 10k-item page was 55 MB/35 s), not a
  correctness requirement.
- Tile IDs (`LO-114202-07-2024`, `MDT-2m-114202-07-2024`, ...) follow a reverse-engineered,
  undocumented scheme: `{prefix}-{tileX:03d}{tileY:03d}-{lot:02d}-{year}`, where
  `originX = (tileX-200)*1000`, `originY = (tileY-300)*1000` in EPSG:3763. Useful for output
  naming/cross-collection grouping only — discovery stays geometry-driven (coverage has gaps).
- Collection STAC metadata says `"license": "proprietary"` / `access: ["private"]`, which
  **contradicts** the CC-BY-4.0 claim in the portal's ISO 19115 metadata. Never assert a
  license ourselves — surface `collection.license` verbatim.

**TLS**: default to the **`truststore`** package (OS-native trust store), with an explicit
`ca_bundle` config escape hatch for corporate MITM proxies. **Never a `verify=False` code
path, anywhere** — enforced by `tests/test_no_verify_false_anywhere.py`.

**Auth** (resolved by probing):
- File download (`GET /v1/download/{token}`) 302s to `/auth/login` → Keycloak
  (`realm=dgterritorio`, `client_id=aai-oidc-dgt`), Authorization Code + PKCE shape.
  Browsing/search need no auth.
- Loopback PKCE impossible (`redirect_uri` locked). `aai-oidc-dgt` is a confidential client:
  ROPC, device-code, self-exchange all `401 unauthorized_client`.
- The API has no token/API-key path — authorization is exclusively an Express `connect.sid`
  session cookie set by DGT's BFF.
- Therefore: a direct HTTP POST against Keycloak's real login form is the only automated
  path. Do it robustly: **`requests` + `BeautifulSoup4`** for parsing, **`keyring`** for
  credentials/session — never Playwright, never plaintext.
- Session TTL is **30 minutes exactly**, `HttpOnly; SameSite=Lax`. Mid-run
  re-auth-and-resume is a core requirement.

## Architecture

```
pyproject.toml                # src/ layout, AGPL-3.0-or-later, PEP 621
src/cddpt/
  __init__.py                 # public re-exports: Aoi, CddCatalog, Downloader, Settings
  settings.py                 # pydantic-settings Settings + platformdirs paths
  errors.py                   # CddError hierarchy (incl. SessionExpired)
  http.py                     # SINGLE place TLS/session/retry are decided (truststore + urllib3.Retry)
  ratelimit.py                # pyrate-limiter token bucket + Retry-After observer + CircuitBreaker
  models.py                   # AssetRef, TileKey-adjacent types, DownloadOutcome, CollectionInfo
  tiles.py                    # decode_tile_key()/tile_extent_3763() — reverse-engineered scheme
  aoi.py                      # Aoi: from_bbox/from_file/from_geojson/from_wkt + shapely chunking
  catalog.py                  # CddCatalog: pystac-client ItemSearch wiring, envelope unwrap, bbox sanitize
  auth/
    base.py                   # AuthProvider Protocol, AuthSession, AuthManager
    store.py                  # keyring-backed credential/session storage
    form_provider.py          # KeycloakFormAuthProvider — requests + BeautifulSoup4
    manual_provider.py        # paste-cookie escape hatch
    probe.py                  # capability probe + regression detector
  download.py                 # Downloader: concurrency, Range-resume, re-auth-and-resume, rate limiting
  naming.py                   # ByCollectionLayout (default) / ByTileLayout / FlatLayout
  cli/                        # typer app (extra)
  py.typed
qgis_plugin/cddpt_qgis/       # thin adapter, built last
```

### Library choices

| Subsystem | Library | Why |
|---|---|---|
| STAC search + pagination | **`pystac-client`** (`ItemSearch` standalone, not `Client.open()`) | Conformant on `POST /search` incl. link pagination; root is 302-blocked. |
| STAC typed models | **`pystac`** (lenient), not `stac-pydantic` | Strict validation would reject real data (corrupt raster `bbox`). |
| `/collections` envelope | ~20 lines custom unwrap → `pystac.Collection.from_dict` | DGT's `{status,message,data}` envelope isn't STAC-shaped. |
| Geometry / AOI | **`shapely` 2.x** + `pyproj`; **`geopandas`+`pyogrio`** as extra `cddpt[files]` | QGIS plugin reads AOIs via `QgsVectorLayer` instead. |
| HTTP | **`requests`** | Already `pystac-client`'s dependency. |
| Retry/backoff | **`urllib3.util.Retry`** + **`tenacity`** (outer re-auth/resume loop) | |
| Rate limiting | **`pyrate-limiter`** | Token bucket; thin `requests`-hook glue. |
| TLS trust | **`truststore`** | OS-native trust store. |
| Credential/session storage | **`keyring`** | OS keychain; never plaintext. |
| Keycloak login parsing | **`BeautifulSoup4`** (+ `lxml`) | |
| CLI | **`typer`** + **`rich`** (extra `cddpt[cli]`) | |
| Progress | **`tqdm`** (extra), behind an injected callback Protocol | QGIS swaps in `QgsTask` progress. |
| Config | **`pydantic-settings`** | |
| Paths | **`platformdirs`** | |
| Tile ID decode | ~30 lines custom, table-tested | |
| `owslib` | **Rejected** | |
| Testing | **`pytest`** + **`vcrpy`** cassettes of real responses | |

### Auth design

- `AuthProvider` Protocol (`is_available`, `authenticate`, `refresh`, `invalidate`) with an
  `AuthManager` facade (`current()` → cache → refresh → re-auth; `on_unauthorized()` for
  302-to-login detection via a `requests` response hook).
- **`KeycloakFormAuthProvider`**: a `requests.Session` follows `GET /auth/login` → Keycloak
  authorize URL, parses the login page with BeautifulSoup4 to find `<form id="kc-form-login">`
  action URL and hidden fields, POSTs `username`/`password`, follows redirects back to
  `cdd.dgterritorio.gov.pt` to capture `connect.sid`. Username + password stay in `keyring`
  for silent re-auth — never plaintext, never logged, purged by `cddpt auth logout`.
- **`ManualCookieAuthProvider`**: paste a `connect.sid` value, stored via `keyring`.
- Session death (302-to-`/auth/login`) → `SessionExpired` → `AuthManager.on_unauthorized()`
  silent re-auth → in-flight download **resumes from its byte offset**.
- `probe.py`: capability probe/regression detector; backs `cddpt auth doctor`.
- QGIS plugin shares the same `keyring` entry as the CLI (not `QgsAuthManager`).

### Download manager

- `ThreadPoolExecutor`, default **2 concurrent**, hard-capped at 4 with a warning.
- Stream to `<dest>.part`, atomic `os.replace`; `Range: bytes=<size>-` resume (probe
  `Accept-Ranges`; degrade gracefully).
- Validate against `properties["file:size"]` when present; log when absent.
- Preflight `plan()`: count + bytes + free-disk check; confirmation above a size threshold
  unless `--yes`.
- `--manifest run.json` records per-asset outcomes.

### Backoff & rate-limiting policy (deliberately conservative)

DGT has published no rate limits, and no load-testing was done (deliberately). Absence of a
known ceiling is a reason to be *more* conservative. Defaults are starting points; users may
explicitly opt into more aggressive settings, never the other way round.

- **Global token bucket** (one shared limiter instance for both the search session and the
  download session): **2 requests/second sustained**, burst capacity **4**.
- **Concurrency**: **2 concurrent downloads** default, hard-capped at 4 with an explicit
  warning if overridden, never higher.
- **Per-request retry**: `urllib3.util.Retry(total=5, backoff_factor=2.0, backoff_max=120,
  status_forcelist=(429, 500, 502, 503, 504), respect_retry_after_header=True,
  allowed_methods=frozenset({"GET", "POST"}))` — server-sent `Retry-After` takes precedence.
- **Circuit breaker** (`ratelimit.py`, a `CircuitBreaker` class wrapping the shared limiter):
  track a rolling count of 429/503 responses **across the whole run**; if **5 such failures
  occur within any 2-minute window**, pause **all** new requests for a **10-minute cooldown**,
  logging clearly why.
- **Large-run etiquette in the CLI**: for large estimates, the preflight prompt says the run
  may take hours-to-days at these rates and that this is intentional.
- **Revisit only with real evidence** (documented limits or observed `RateLimit-*`/`Retry-After`).

### AOI + search pagination

- `Aoi.from_bbox` / `.from_file` (geopandas+pyogrio extra) / `.from_geojson` / `.from_wkt`,
  always normalized to WGS84.
- `Aoi.chunks(max_km2)` — shapely `box()`-grid intersected with the AOI (area computed in an
  equal-area / EPSG:3763 projection). Default threshold ~2000 km².
- `CddCatalog.iter_items()` streams via `pystac_client.ItemSearch`, dedups across chunk
  boundaries by item ID, never materializes the full result set.
- `downloadable_collection_ids()` discovers from `/collections` dynamically
  (`summaries.visibility == "show"` + a data-role asset present) — never hardcoded.

### CLI surface (`typer`)

```
cddpt auth login [--headless] | status | logout | doctor
cddpt collections list [--all] [--json] | show <id>
cddpt search   --bbox W,S,E,N | --aoi FILE | --wkt STR
               --collection LAZ [--collection MDT-2m ...]
               [--datetime ...] [--chunk-km2 N] [--estimate-only]
               [--output items.geojson] [--format table|json|geojson]
cddpt download <same selection flags> --out DIR
               [--layout by-collection|by-tile|flat] [--concurrency 2]
               [--dry-run] [--overwrite] [--manifest run.json]
```
Exit codes: auth failure 3, insufficient disk 4, generic failure 1.

### QGIS plugin (built last)

Two `QgsProcessingAlgorithm`s (search, download); guided `pip install --user cddpt` via
`bootstrap.py` + `QgsTask` (never silent, never at startup); core calls always inside a
`QgsTask`; `qgisMinimumVersion=3.34`.

## Milestones

1. **Skeleton + HTTP/TLS foundation** — `pyproject.toml`, `settings.py`, `errors.py`,
   `http.py`, `ratelimit.py`, `test_no_verify_false_anywhere`.
2. **Catalog + AOI, fully anonymous** — `tiles.py`, `aoi.py`, `catalog.py`, `models.py`;
   `vcrpy` cassettes.
3. **CLI part 1** — `collections list/show`, `search`, `--estimate-only`, GeoJSON export.
4. **Auth** — needs a human-registered CDD account.
5. **Downloader** — validate on small MDT-2m AOI before LAZ.
6. **Hardening + PyPI** — `mypy --strict`, ≥85% coverage, README, CI (Linux/macOS/Windows ×
   Python 3.10–3.13), publish 0.1.0.
7. **QGIS plugin.**

Companion tool `tools/cog_recipe/` (`build-cog convert` / `mosaic-10m`, rio-cogeo LERC_ZSTD
with `MAX_Z_ERROR=0.05`) follows after.

## Open items requiring a human

- Register a CDD account; DevTools-verify: bare `connect.sid` sufficient for
  `/v1/download/{token}`? `Range` honored? download tokens stable across sessions? session
  TTL rolling or absolute?
- DGT outreach deliberately deferred.
- Rate limits uncharacterized — ship conservative defaults.
- License contradiction in DGT metadata — surface `collection.license` verbatim; print an
  attribution reminder after downloads.

## Findings during implementation (M2, 2026-09-26)

These refine the probing results above:

- `/collections` `data` is `{"collections": [...], "models", "reservedKeys", "keywords",
  "translations", "catalog"}`; `/collections/{id}` `data` is the bare Collection.
- Visibility lives at `summaries.visibility` (a list). Of 21 live collections, the 14 mainland
  ones are `["show"]`, the 7 Açores ones `["hide"]`. `summaries.access` is `["private"]`
  everywhere. No collection has `item_assets`, so "downloadable" = visibility only.
- `pystac.Collection.from_dict()` silently rewrites `"proprietary"` → `"other"` during
  migration; cddpt uses `migrate=False` and reads `license` from the raw dict.
- The corrupted geotransform `bbox` is **per batch, not per collection** (e.g. 2024 MDT-2m
  items corrupted, 2025 ones fine) — sanitisation is unconditional for every item.
- Tile-ID `originY` is the tile's **top (max-Y) edge**, confirmed against geotransforms and
  reprojected LAZ bboxes.
- Orthophoto assets have `roles: ["visual"]` only (no `"data"`); asset selection prefers
  `"data"` and falls back to `"visual"`.
- `pystac_client.StacApiIO` has no session-injection parameter; cddpt replaces its
  `.session` with the governed session before passing it to `ItemSearch(stac_io=...)`.

## Findings with an authenticated account (M4 pre-work, 2026-09-26)

These resolve the "open items requiring a human" and **change the download design**:

- **Login flow** (works with plain `requests` + BeautifulSoup): `GET /dgt-be/v1/download/{token}`
  → 302 `https://cdd.dgterritorio.gov.pt/auth/login` → 302 Keycloak
  `https://auth.cdd.dgterritorio.gov.pt/realms/dgterritorio/protocol/openid-connect/auth`
  (200, login page). `<form id="kc-form-login">` posts to
  `/realms/dgterritorio/login-actions/authenticate` with inputs `username`, `password`,
  hidden `credentialId` (empty). On success: 302 → `https://cdd.dgterritorio.gov.pt/auth/callback`
  (sets `connect.sid`, `auth_session`, `auth_user`, `auth_email`, all 30-min `HttpOnly`)
  → 302 → `/dgt-fe/downloads` (200 HTML). Anonymous visitors already get a `connect.sid`;
  login upgrades that session server-side.
- **A bare `connect.sid` is sufficient** to authorize `/download/{token}` (no CSRF/Origin
  check) — `ManualCookieAuthProvider` is viable.
- **Download tokens are minted per `/search` response and are single-use.** Two searches for
  the same item yield different tokens; a used token returns
  `403 {"status":403,"message":"Forbidden Access - Expired token or file not found"}`.
  Consequence: never persist/reuse hrefs; mint a fresh token right before each download
  (`POST /search` with `{"collections":[...], "ids":[item_id]}` works and is cheap).
- **Authenticated `/download/{token}` does not serve bytes**: it 302s to a **pre-signed S3
  URL** (MinIO at `https://stor-002.a.acnca.pt:9000/...`, `X-Amz-Expires=3600`). That URL
  needs no cookies, is reusable within its hour, and **honours `Range`** (`206`,
  `Accept-Ranges: bytes`). Hence the 30-minute CDD session only gates the token →
  pre-signed-URL exchange, not the byte transfer. Resume = `Range` against the same
  pre-signed URL while valid; if it has expired (S3 403), re-mint (search by id → download
  token → new pre-signed URL) and continue with `Range` from the `.part` size.
- An unauthenticated or expired session on `/download/{token}` gives **302 → `/auth/login`**
  (the `SessionExpired` signal); an authorized session with a spent token gives **403 JSON**.
- **The session expires absolutely 30 min after login** (no rolling): authenticated responses carry no `Set-Cookie`, and a session last used 1 min after login was rejected (302 → `/auth/login`) at login + 31.5 min.

## Notes from the downloader (M5, 2026-09-27)

- **One governor per run, passed explicitly everywhere.** Any component that falls back to
  `RequestGovernor.from_settings(...)` when not handed one (e.g. `KeycloakFormAuthProvider`)
  silently fragments the rate budget. `cddpt download` wires a single governor through
  search, login, token exchange and transfer; new components must accept and use it too.
- **Manual-cookie sessions cannot be renewed mid-run.** With only `cddpt auth login --cookie`,
  a download that outlives the 30-minute session fails with an auth error asking for a fresh
  cookie (`ManualCookieAuthProvider.refresh()` raises `SessionExpired` by design). Stored
  credentials renew transparently.
- **Locked/unavailable keyring + env credentials** → `cddpt download` warns and continues
  with an in-memory session instead of failing.
- A `206` whose `Content-Range` starts at neither the requested offset nor 0 fails the asset
  and discards the `.part` (it can be neither appended nor rewritten safely).
- Destination names derive from item id + media type (`image/tiff*` → `.tif`,
  `application/vnd.laszip` → `.laz`, else `mimetypes`, else `.bin`), so reruns can skip
  completed files without spending tokens; the storage filename is kept in the manifest.
