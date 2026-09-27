"""Runtime configuration for cddpt.

Every field can be overridden by an environment variable named
``CDDPT_<FIELD_NAME>`` (case-insensitive) -- e.g. ``CDDPT_CONCURRENCY=3`` --
or by passing keyword arguments to :class:`Settings` directly. A ``.env``
file in the current working directory is also honoured.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from platformdirs import PlatformDirs
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ._version import __version__

_DIRS = PlatformDirs(appname="cddpt")

#: Hard cap on concurrent downloads. Values above this are always rejected --
#: see docs/PLAN.md, "Backoff & rate-limiting policy": "hard-capped at 4 with
#: an explicit warning if overridden, never higher".
_MAX_CONCURRENCY = 4
#: Above this, a warning is emitted but the value is still accepted.
_RECOMMENDED_MAX_CONCURRENCY = 2


def _default_user_agent() -> str:
    return f"cddpt/{__version__} (+https://pypi.org/project/cddpt/)"


def validate_concurrency(value: int) -> int:
    """The concurrency hard-cap/recommended-max policy from docs/PLAN.md's
    "Backoff & rate-limiting policy": 1..4 (raises :class:`ValueError`
    outside that range), with a warning above
    :data:`_RECOMMENDED_MAX_CONCURRENCY`.

    Factored out of :class:`Settings`'s own field validator so
    :meth:`cddpt.download.Downloader.run` can apply the *exact same* policy
    to an explicit ``concurrency=`` override -- which bypasses
    :class:`Settings` entirely and would otherwise never be capped at all
    (see ``download.py``'s module docstring for why that mattered).
    """

    if value < 1 or value > _MAX_CONCURRENCY:
        msg = f"concurrency must be between 1 and {_MAX_CONCURRENCY} (hard cap; got {value})"
        raise ValueError(msg)
    if value > _RECOMMENDED_MAX_CONCURRENCY:
        warnings.warn(
            f"concurrency={value} exceeds the recommended default of "
            f"{_RECOMMENDED_MAX_CONCURRENCY}. DGT has published no rate "
            "limits for this API; higher concurrency is an explicit "
            "opt-in, not a recommendation.",
            stacklevel=2,
        )
    return value


class Settings(BaseSettings):
    """cddpt configuration.

    Deliberately conservative defaults throughout (see docs/PLAN.md,
    "Backoff & rate-limiting policy"): DGT has published no rate limits and
    no load-testing was done, so the absence of a known ceiling is a reason
    to be *more* conservative, not less. Users may explicitly opt into more
    aggressive settings; cddpt never does so on their behalf.
    """

    model_config = SettingsConfigDict(env_prefix="CDDPT_", extra="ignore")

    # -- API endpoints --------------------------------------------------
    api_base_url: str = "https://cdd.dgterritorio.gov.pt/dgt-be/v1"
    site_base_url: str = "https://cdd.dgterritorio.gov.pt"
    #: Keycloak's own host -- separate from `site_base_url` since it's a
    #: distinct TLS endpoint DGT could in principle move independently (see
    #: cddpt.auth.probe.doctor()'s TLS-reachability checks).
    auth_base_url: str = "https://auth.cdd.dgterritorio.gov.pt"
    keycloak_realm: str = "dgterritorio"
    #: Confidential client; loopback redirect URIs are rejected and there is
    #: no token/API-key path -- see docs/PLAN.md's "Auth design". Used only
    #: by cddpt.auth.probe's regression detector, never to attempt OAuth.
    keycloak_client_id: str = "aai-oidc-dgt"

    # -- Credentials (all optional; auth.form_provider falls back to keyring
    # when neither is set) -----------------------------------------------
    #: CDD account username/email. Settable via CDDPT_USERNAME.
    username: str | None = None
    #: CDD account password. A pydantic SecretStr so it is masked in
    #: repr()/str() and never appears in logs by accident. Settable via
    #: CDDPT_PASSWORD.
    password: SecretStr | None = None

    # -- TLS --------------------------------------------------------------
    #: Escape hatch for corporate TLS-inspecting (MITM) proxies. When unset
    #: (the default), cddpt verifies against the OS-native trust store via
    #: ``truststore`` -- see ``http.py``. Never a path to disable
    #: verification.
    ca_bundle: Path | None = None

    # -- Rate limiting / concurrency -------------------------------------
    requests_per_second: float = 2.0
    burst: int = 4
    #: Concurrent downloads. Validated to 1..4 below; values above 2 emit a
    #: warning, values above 4 are rejected outright.
    concurrency: int = 2

    # -- AOI chunking ------------------------------------------------------
    chunk_km2: float = 2000.0

    # -- HTTP timeouts (seconds) -------------------------------------------
    connect_timeout: float = 10.0
    read_timeout: float = 120.0

    # -- HTTP identification -------------------------------------------
    user_agent: str = Field(default_factory=_default_user_agent)

    # -- Filesystem locations (platformdirs-derived) -----------------------
    config_dir: Path = Field(default_factory=lambda: Path(_DIRS.user_config_dir))
    cache_dir: Path = Field(default_factory=lambda: Path(_DIRS.user_cache_dir))
    state_dir: Path = Field(default_factory=lambda: Path(_DIRS.user_state_dir))

    @field_validator("concurrency")
    @classmethod
    def _validate_concurrency(cls, value: int) -> int:
        return validate_concurrency(value)


__all__ = ["Settings", "validate_concurrency"]
