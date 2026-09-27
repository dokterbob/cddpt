"""Tests for cddpt.auth.store.CredentialStore.

Uses the ``fake_keyring``/``no_keyring_backend`` fixtures from
tests/conftest.py -- never the real OS keyring. Each test requests exactly
the fixture it needs as a function parameter (rather than a blanket
module-level ``pytestmark``), so there is never any ambiguity about which
keyring backend is active for the ``no_keyring_backend`` tests.

``CredentialStore`` holds only the long-lived credential (username +
password) -- the CDD session itself is never persisted (see
``cddpt.auth.base``'s module docstring), so there is no session/manual-cookie
storage to test here any more.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from cddpt.auth.store import CredentialStore
from cddpt.errors import AuthError


def test_username_roundtrip(fake_keyring: object) -> None:
    store = CredentialStore()
    assert store.get_username() is None
    store.set_username("alice@example.test")
    assert store.get_username() == "alice@example.test"


def test_password_roundtrip_keyed_by_username(fake_keyring: object) -> None:
    store = CredentialStore()
    store.set_password("alice@example.test", SecretStr("sentinel-password"))
    retrieved = store.get_password("alice@example.test")
    assert retrieved is not None
    assert retrieved.get_secret_value() == "sentinel-password"
    # A different username has no password of its own.
    assert store.get_password("bob@example.test") is None


def test_clear_all_purges_everything(fake_keyring: object) -> None:
    store = CredentialStore()
    store.set_username("alice@example.test")
    store.set_password("alice@example.test", SecretStr("sentinel-password"))

    store.clear_all()

    assert store.get_username() is None
    assert store.get_password("alice@example.test") is None


def test_clear_all_is_a_no_op_when_nothing_was_ever_stored(fake_keyring: object) -> None:
    store = CredentialStore()
    store.clear_all()  # must not raise


def test_backend_name_reports_the_active_backend(fake_keyring: object) -> None:
    store = CredentialStore()
    assert "InMemoryKeyring" in store.backend_name()


def test_no_keyring_backend_raises_clear_auth_error_on_every_operation(
    no_keyring_backend: object,
) -> None:
    store = CredentialStore()

    operations = (
        lambda: store.get_username(),
        lambda: store.set_username("alice"),
        lambda: store.get_password("alice"),
        lambda: store.set_password("alice", SecretStr("x")),
        lambda: store.clear_all(),
    )
    for operation in operations:
        with pytest.raises(AuthError) as excinfo:
            operation()
        message = str(excinfo.value)
        assert "keyring" in message.lower()
        # The error must point at concrete alternatives, not just fail silently.
        assert "CDDPT_USERNAME" in message or "CDDPT_SESSION_COOKIE" in message
