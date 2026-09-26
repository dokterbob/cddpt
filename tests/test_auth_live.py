"""One real login against DGT's live CDD/Keycloak, plus session validation.

Skipped unless CDDPT_USERNAME/CDDPT_PASSWORD are set (see the module-level
skip below) -- never run as part of the normal offline suite (pytest's
default ``addopts = "-m 'not network'"`` already deselects it too). This is
the ONE test in this task that is allowed to touch the real account; it
performs exactly one real login. It never downloads any bytes -- the
download-token validation stops at ``allow_redirects=False`` and inspects
only the resulting status code and redirect host, exactly like
``cddpt auth doctor --live``.
"""

from __future__ import annotations

import os

import pytest

from cddpt.auth.base import AuthManager
from cddpt.auth.form_provider import KeycloakFormAuthProvider
from cddpt.auth.probe import _validate_session_download
from cddpt.settings import Settings

pytestmark = pytest.mark.network

_USERNAME = os.environ.get("CDDPT_USERNAME")
_PASSWORD = os.environ.get("CDDPT_PASSWORD")


@pytest.mark.skipif(
    not (_USERNAME and _PASSWORD),
    reason="CDDPT_USERNAME/CDDPT_PASSWORD not set -- skipping the one live auth test",
)
def test_real_login_and_session_validation() -> None:
    settings = Settings()
    provider = KeycloakFormAuthProvider(settings=settings)
    manager = AuthManager(settings=settings, provider=provider)

    session = manager.current()
    assert "connect.sid" in session.cookies
    assert session.source == "form"

    http_session = provider._session  # the same governed session the login used
    manager.apply(http_session)

    result = _validate_session_download(http_session, settings)
    assert result.ok, result.detail
