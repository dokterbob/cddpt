"""cddpt: unofficial, independent client for DGT's CDD geodata portal.

Not affiliated with, endorsed by, or supported by Direção-Geral do
Território.

This milestone (Milestone 1) provides only the package skeleton and the
HTTP/TLS foundation: configuration (:class:`~cddpt.settings.Settings`), the
error hierarchy, the governed HTTP session factory, and the shared
rate-limiter/circuit-breaker. Search, download, auth and the CLI land in
later milestones.
"""

from __future__ import annotations

from ._version import __version__
from .settings import Settings

__all__ = ["Settings", "__version__"]
