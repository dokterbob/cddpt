"""cddpt: unofficial, independent client for DGT's CDD geodata portal.

Not affiliated with, endorsed by, or supported by Direção-Geral do
Território.

Milestone 1 provided the package skeleton and HTTP/TLS foundation.
Milestone 2 adds fully anonymous catalog browsing, AOI handling and search:
:class:`~cddpt.aoi.Aoi` and :class:`~cddpt.catalog.CddCatalog`. Auth,
downloading and the CLI land in later milestones (``Downloader`` is not
exported yet).
"""

from __future__ import annotations

from ._version import __version__
from .aoi import Aoi
from .catalog import CddCatalog
from .settings import Settings

__all__ = ["Aoi", "CddCatalog", "Settings", "__version__"]
