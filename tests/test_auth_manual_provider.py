"""Tests for cddpt.auth.manual_provider.ManualCookieAuthProvider.

The cookie value is always a per-run input passed in explicitly (from
``CDDPT_SESSION_COOKIE`` or a ``--cookie`` prompt) -- this provider never
reads or writes the keyring.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import SecretStr

from cddpt.auth.base import SESSION_TTL, AuthSession
from cddpt.auth.manual_provider import COOKIE_NAME, ManualCookieAuthProvider
from cddpt.errors import AuthError, SessionExpired


def test_authenticate_with_explicit_cookie_value() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    provider = ManualCookieAuthProvider(cookie_value=SecretStr("sentinel-value"), clock=lambda: now)

    session = provider.authenticate()

    assert isinstance(session, AuthSession)
    assert session.source == "manual"
    assert session.cookies == {COOKIE_NAME: "sentinel-value"}
    assert session.obtained_at == now
    assert session.expires_at == now + SESSION_TTL


def test_is_available_true_with_explicit_value() -> None:
    provider = ManualCookieAuthProvider(cookie_value=SecretStr("x"))
    assert provider.is_available() is True


def test_is_available_false_with_nothing_configured() -> None:
    provider = ManualCookieAuthProvider()
    assert provider.is_available() is False


def test_authenticate_raises_when_nothing_configured() -> None:
    provider = ManualCookieAuthProvider()
    with pytest.raises(AuthError, match="no manually-supplied session cookie"):
        provider.authenticate()


def test_refresh_always_raises_session_expired() -> None:
    """A pasted cookie can never be silently renewed -- see module docstring."""

    provider = ManualCookieAuthProvider(cookie_value=SecretStr("x"))
    old_session = provider.authenticate()
    with pytest.raises(SessionExpired, match="supply a fresh"):
        provider.refresh(old_session)


def test_invalidate_is_a_harmless_no_op() -> None:
    provider = ManualCookieAuthProvider(cookie_value=SecretStr("x"))
    provider.invalidate()  # must not raise
