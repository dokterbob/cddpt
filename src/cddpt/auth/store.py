"""Keyring-backed storage for cddpt's CDD credentials and current session.

Service name: ``"cddpt"`` (keyring's ``service_name``). Four kinds of entry,
all under that one service:

- ``username`` -- a fixed keyring "username" slot holding the CDD account's
  username/email.
- the password for that username -- keyring's own data model is
  ``(service, username) -> password``, so the password is naturally stored
  keyed by whatever username was last set via :meth:`CredentialStore.set_username`.
- ``manual-cookie`` -- a fixed slot holding a pasted ``connect.sid`` value
  for :class:`~cddpt.auth.manual_provider.ManualCookieAuthProvider`, kept
  distinct from the real account's password entry.
- ``session`` -- a fixed slot holding the current
  :class:`~cddpt.auth.base.AuthSession`, JSON-encoded (cookie values +
  timestamps + source).

Never any plaintext file fallback. If the keyring is unusable (no backend
configured, locked, or similar --
:class:`keyring.errors.KeyringError`/:class:`~keyring.errors.NoKeyringError`),
every method here raises a clear :class:`~cddpt.errors.AuthError` explaining
the options (env vars, ``--cookie``, or fixing the keyring backend) rather
than silently degrading to disk.
"""

from __future__ import annotations

import json
import logging

import keyring
import keyring.errors
from pydantic import SecretStr

from ..errors import AuthError
from .base import AuthSession

logger = logging.getLogger(__name__)

#: keyring's ``service_name`` for every cddpt entry.
SERVICE_NAME = "cddpt"

_USERNAME_KEY = "username"
_MANUAL_COOKIE_KEY = "manual-cookie"
_SESSION_KEY = "session"


def _wrap(action: str, exc: Exception) -> AuthError:
    return AuthError(
        f"cddpt: could not {action} in the system keyring "
        f"({exc.__class__.__name__}). Your OS keyring backend may be "
        "unavailable, locked, or not configured. Options: pass credentials "
        "via the CDDPT_USERNAME/CDDPT_PASSWORD environment variables, use "
        "`cddpt auth login --cookie` with a pasted session cookie, or "
        "install/unlock a working keyring backend. cddpt never falls back "
        "to storing secrets in a plaintext file."
    )


class CredentialStore:
    """Keyring-backed storage for username, password, a manually-pasted
    cookie, and the current :class:`~cddpt.auth.base.AuthSession`."""

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

    # -- manual cookie ------------------------------------------------------

    def get_manual_cookie(self) -> SecretStr | None:
        try:
            value = keyring.get_password(self._service_name, _MANUAL_COOKIE_KEY)
        except keyring.errors.KeyringError as exc:
            raise _wrap("read the stored manual cookie", exc) from exc
        return SecretStr(value) if value is not None else None

    def set_manual_cookie(self, cookie_value: SecretStr) -> None:
        try:
            keyring.set_password(
                self._service_name, _MANUAL_COOKIE_KEY, cookie_value.get_secret_value()
            )
        except keyring.errors.KeyringError as exc:
            raise _wrap("store the manual cookie", exc) from exc

    # -- session ------------------------------------------------------------

    def load_session(self) -> AuthSession | None:
        try:
            raw = keyring.get_password(self._service_name, _SESSION_KEY)
        except keyring.errors.KeyringError as exc:
            raise _wrap("read the stored session", exc) from exc
        if raw is None:
            return None
        try:
            return AuthSession.from_json_dict(json.loads(raw))
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("cddpt: stored session in keyring was malformed, ignoring it: %s", exc)
            return None

    def save_session(self, session: AuthSession) -> None:
        try:
            keyring.set_password(
                self._service_name, _SESSION_KEY, json.dumps(session.to_json_dict())
            )
        except keyring.errors.KeyringError as exc:
            raise _wrap("store the session", exc) from exc

    def clear_session(self) -> None:
        self._delete_quiet(_SESSION_KEY)

    # -- bulk purge -----------------------------------------------------------

    def clear_all(self) -> None:
        """Purge every cddpt keyring entry: the stored username, its
        password, the manual cookie, and the cached session.

        Best-effort: an entry that was never set is simply skipped (not an
        error) -- but a genuinely broken keyring backend still raises
        :class:`~cddpt.errors.AuthError`, same as every other method here.
        """

        username = self.get_username()
        if username:
            self._delete_quiet(username)
        self._delete_quiet(_USERNAME_KEY)
        self._delete_quiet(_MANUAL_COOKIE_KEY)
        self._delete_quiet(_SESSION_KEY)

    def _delete_quiet(self, key: str) -> None:
        try:
            keyring.delete_password(self._service_name, key)
        except keyring.errors.PasswordDeleteError:
            pass  # already absent -- not an error.
        except keyring.errors.KeyringError as exc:
            raise _wrap(f"delete the {key!r} entry", exc) from exc


__all__ = ["SERVICE_NAME", "CredentialStore"]
