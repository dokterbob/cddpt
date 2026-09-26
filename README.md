# cddpt
Search and download information from the DGT's CDD in Portugal.

> **Unofficial, independent client. Not affiliated with, endorsed by, or supported by
> Direção-Geral do Território.**

Status: early development — see the roadmap below.

## Design documents

- [docs/PLAN.md](docs/PLAN.md) — design plan: API findings, architecture, library choices,
  auth, download and rate-limiting policy
- [docs/roadmap.md](docs/roadmap.md) — milestone status, verification, review checkpoints,
  open items
- [docs/qgis-plugin.md](docs/qgis-plugin.md) — QGIS plugin design (Milestone 7)
- [docs/build-cog.md](docs/build-cog.md) — companion `build-cog` COG re-encoder

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE).

## Quick start (anonymous search — no account needed)

```sh
uv tool install 'cddpt[cli,files]'   # or: pip install 'cddpt[cli,files]'

cddpt collections list
cddpt search --aoi my_area.geojson --collection MDT-2m --estimate-only
cddpt search --bbox -9.15,38.70,-9.13,38.72 --collection LAZ --output tiles.geojson
```

Downloading (which requires a free CDD account) is not implemented yet — see the roadmap.
