"""cddpt: unofficial, independent client for DGT's CDD geodata portal.

Not affiliated with, endorsed by, or supported by Direção-Geral do
Território.

Milestone 1 provided the package skeleton and HTTP/TLS foundation.
Milestone 2 added fully anonymous catalog browsing, AOI handling and search:
:class:`~cddpt.aoi.Aoi` and :class:`~cddpt.catalog.CddCatalog`. Milestone 4
added authentication (:mod:`cddpt.auth`). Milestone 5 adds the downloader:
:class:`~cddpt.download.Downloader`.
"""

from __future__ import annotations

from ._version import __version__
from .aoi import Aoi
from .catalog import CddCatalog
from .download import Downloader
from .settings import Settings

__all__ = ["Aoi", "CddCatalog", "Downloader", "Settings", "__version__"]
