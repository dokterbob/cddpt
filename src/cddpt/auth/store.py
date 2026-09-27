"""Keyring-backed storage for cddpt's long-lived CDD credential (username +
password).

Service name: ``"cddpt"`` (keyring's ``service_name``). Two kinds of entry,
both under that one service:

- ``username`` -- a fixed keyring "username" slot holding the CDD account's
  username/email.
- the password for that username -- keyring's own data model is
  ``(service, username) -> password``, so the password is naturally stored
  keyed by whatever username was last set via :meth:`CredentialStore.set_username`.

The CDD session cookie itself is **never** stored here (or anywhere else):
it's a short-lived credential (30-minute absolute expiry, no rolling -- see
docs/PLAN.md's Auth design) that :class:`~cddpt.auth.base.AuthManager` keeps
in memory only, for the lifetime of one process. Keyring holds only the
long-lived secret that makes unattended re-login possible.

Never any plaintext file fallback. If the keyring is unusable (no backend
configured, locked, or similar --
:class:`keyring.errors.KeyringError`/:class:`~keyring.errors.NoKeyringError`),
every method here raises a clear :class:`~cddpt.errors.AuthError` explaining
the options (env vars, a session cookie, or fixing the keyring backend)
rather than silently degrading to disk.
"""

from __future__ import annotations

import keyring
import keyring.errors
from pydantic import SecretStr

from ..errors import AuthError

#: keyring's ``service_name`` for every cddpt entry.
SERVICE_NAME = "cddpt"

_USERNAME_KEY = "username"


def _wrap(action: str, exc: Exception) -> AuthError:
    return AuthError(
        f"cddpt: could not {action} in the system keyring "
        f"({exc.__class__.__name__}). Your OS keyring backend may be "
        "unavailable, locked, or not configured. Options: pass credentials "
        "via the CDDPT_USERNAME/CDDPT_PASSWORD environment variables, pass a "
        "session cookie via CDDPT_SESSION_COOKIE or `download --cookie`, or "
        "install/unlock a working keyring backend. cddpt never falls back "
        "to storing secrets in a plaintext file."
    )


class CredentialStore:
    """Keyring-backed storage for the CDD account's username and password."""

    def __init__(self, service_name: str = SERVICE_NAME) -> None:
        self._service_name = service_name

    def backend_name(self) -> str:
        """The active keyring backend's class name, or a ``"<unavailable:
        ...>"`` placeholder if even asking for one fails."""

        try:
            return type(keyring.get_keyring()).__name__
        except Exception as exc:  # keyring.get_keyring() itself isn't narrowly typed
            return f"<unavailable: {exc.__class__.__name__}>"

    # -- username ---------------------------------------------------------

    def get_username(self) -> str | None:
        try:
            return keyring.get_password(self._service_name, _USERNAME_KEY)
        except keyring.errors.KeyringError as exc:
            raise _wrap("read the stored username", exc) from exc

    def set_username(self, username: str) -> None:
        try:
            keyring.set_password(self._service_name, _USERNAME_KEY, username)
        except keyring.errors.KeyringError as exc:
            raise _wrap("store the username", exc) from exc

    # -- password (keyed by username) --------------------------------------

    def get_password(self, username: str) -> SecretStr | None:
        try:
            value = keyring.get_password(self._service_name, username)
        except keyring.errors.KeyringError as exc:
            raise _wrap("read the stored password", exc) from exc
        return SecretStr(value) if value is not None else None

    def set_password(self, username: str, password: SecretStr) -> None:
        try:
            keyring.set_password(self._service_name, username, password.get_secret_value())
        except keyring.errors.KeyringError as exc:
            raise _wrap("store the password", exc) from exc

    # -- bulk purge -----------------------------------------------------------

    def clear_all(self) -> None:
        """Purge every cddpt keyring entry: the stored username and its
        password.

        Best-effort: an entry that was never set is simply skipped (not an
        error) -- but a genuinely broken keyring backend still raises
        :class:`~cddpt.errors.AuthError`, same as every other method here.
        """

        username = self.get_username()
        if username:
            self._delete_quiet(username)
        self._delete_quiet(_USERNAME_KEY)

    def _delete_quiet(self, key: str) -> None:
        try:
            keyring.delete_password(self._service_name, key)
        except keyring.errors.PasswordDeleteError:
            pass  # already absent -- not an error.
        except keyring.errors.KeyringError as exc:
            raise _wrap(f"delete the {key!r} entry", exc) from exc


__all__ = ["SERVICE_NAME", "CredentialStore"]
