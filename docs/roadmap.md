# Roadmap, verification & process

Status tracker for the milestones in [PLAN.md](PLAN.md).

| # | Milestone | Status |
|---|---|---|
| 1 | Skeleton + HTTP/TLS foundation | done |
| 2 | Catalog + AOI (anonymous) | done |
| 3 | CLI part 1 (collections, search, estimate) | done |
| 4 | Auth — **blocked on a human-registered CDD account** | planned |
| 5 | Downloader | planned |
| 6 | Hardening + PyPI 0.1.0 | planned |
| 7 | QGIS plugin — see [qgis-plugin.md](qgis-plugin.md) | planned |
| — | `build-cog` companion tool — see [build-cog.md](build-cog.md) | planned |

## Milestone details (M4–M6)

- **M4 Auth** — `auth/` package; `ManualCookieAuthProvider` first, then
  `KeycloakFormAuthProvider` (requests + BeautifulSoup4); `cddpt auth login/status/logout/doctor`.
  Before building, with a real account (~20 min DevTools): (1) does the bare `connect.sid`
  cookie authorize `GET /v1/download/{token}` via plain `curl`, or is a CSRF/Origin check also
  needed (then `AuthSession` must carry more than the cookie)? (2) does `/v1/download/{token}`
  honor `Range`? (3) are download tokens stable across sessions or per-login (does re-auth need
  a re-search for fresh hrefs)? (4) does the 30-minute session roll on activity or expire
  absolutely?
- **M5 Downloader** — `download.py`, `naming.py`, Range-resume, re-auth-and-resume,
  concurrency, manifest. Validate against a small MDT-2m AOI (≈1 MB tiles) before touching
  LAZ (≈342 MB tiles).
- **M6 Hardening + PyPI** — `mypy --strict`, ≥85% coverage, README with the non-affiliation
  disclaimer, CI across Linux/macOS/Windows × Python 3.10–3.13, publish **0.1.0**.

## Verification

- `pytest` for `tiles.py` against verified tile-ID/geotransform pairs, and `catalog.py`'s
  envelope-unwrap + bbox-sanitize against `vcrpy` cassettes of the real anonymous API.
- `test_no_verify_false_anywhere`: AST/grep guard — no `CERT_NONE`, `check_hostname=False`,
  or `verify=False` anywhere in `src/`.
- End-to-end smoke test after M4/M5: `cddpt auth login` → `cddpt search --bbox <small AOI>
  --collection MDT-2m --estimate-only` → `cddpt download` the same AOI → files land, sizes
  match `file:size`, a second run is a no-op.

## Review checkpoints

Each phase follows *build → independent review → fix → re-review*. Checkpoints:

1. **After the core library (M1–M6)** — review `src/cddpt/` for:
   - the no-`verify=False` guard genuinely holds (re-derived independently);
   - credentials / `connect.sid` touch `keyring` only — never a plaintext file, log, or config;
   - `item.bbox` is never read outside the single sanitization step in `catalog.py`;
   - collections always come from a live `/collections` call — no hardcoded list;
   - re-auth-and-resume resumes from a byte offset (`Range` against `.part` size), not a restart;
   - ≥85% coverage and `mypy --strict` clean;
   - module boundaries match the architecture; no subsystem reimplements a chosen library.
2. **After the QGIS plugin** — additionally: `bootstrap.py` install flow never silent / at
   startup; plugin shares the CLI's `keyring` entry; no core call outside a `QgsTask`.
3. **After `build-cog`** — additionally: atomic rename is gated by validation; `MAX_Z_ERROR`
   enforced end-to-end (pixel-diff actually run); Stage 2 has no chunking beyond `--bbox`.

## Deferred / policy

- DGT outreach (courtesy email, acceptable automated-access rates, API token path) —
  deliberately deferred until the tool works.
- Rate limits uncharacterized — conservative defaults (2 concurrent, 2 req/s, whole-run
  circuit breaker); relax only on real evidence.
- DGT metadata license is contradictory — surface `collection.license` verbatim and print an
  attribution reminder after downloads.
