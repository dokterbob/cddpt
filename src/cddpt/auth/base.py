"""The core auth data model: :class:`AuthSession`, the :class:`AuthProvider`
Protocol, the :class:`AuthManager` facade, and :func:`is_login_redirect`.

``AuthManager`` is meant to be **the only auth thing the rest of cddpt
touches** -- callers never read/write cookies or talk to a provider
directly. It:

- caches one :class:`AuthSession` in memory;
- falls back to a caller-supplied :class:`SessionStore` (typically
  :class:`cddpt.auth.store.CredentialStore`) for a still-valid session
  persisted from a previous process, when the in-memory cache is empty or
  stale;
- otherwise asks its :class:`AuthProvider` to (re-)authenticate, proactively
  -- i.e. *before* a session actually expires, once fewer than
  ``refresh_margin`` remains (docs/PLAN.md: "refresh proactively with a
  safety margin (e.g. re-auth when < 3 min left)");
- is thread-safe: :meth:`current` and :meth:`on_unauthorized` both hold a
  single lock across their whole check-and-maybe-reauthenticate sequence, so
  concurrent download threads that all observe an expired/rejected session
  at once trigger exactly *one* real re-authentication, not one per thread
  (see :meth:`on_unauthorized`'s docstring for the exact mechanism).

Why ``AuthManager`` doesn't own a real, persistent store by default
---------------------------------------------------------------------
``store`` is a small :class:`SessionStore` Protocol, not a concrete
:class:`~cddpt.auth.store.CredentialStore` -- and defaults to ``None``
(in-memory-only caching for the lifetime of this ``AuthManager``, never
touching the keyring). This is also exactly the mechanism behind
``cddpt auth login --no-save``: the CLI passes ``store=None`` in that case,
and a real ``CredentialStore()`` otherwise. Keeping the dependency this way
round (rather than ``AuthManager`` importing and default-constructing
``cddpt.auth.store.CredentialStore`` itself) also avoids a base.py <->
store.py import cycle, since ``store.py`` needs ``AuthSession`` from this
module.

Why ``refresh()`` (not ``authenticate()``) is used to recover a dead session
-------------------------------------------------------------------------------
:meth:`AuthManager.on_unauthorized` and the proactive path in
:meth:`AuthManager.current` both call ``provider.refresh(old_session)``
whenever there *is* a previous session to hand back, and only fall back to
``provider.authenticate()`` on a true cold start (no session at all, e.g.
the very first call in a fresh process). This distinction matters for
:class:`~cddpt.auth.manual_provider.ManualCookieAuthProvider`: its
``refresh()`` always raises :class:`~cddpt.errors.SessionExpired` (a pasted
cookie cannot be silently renewed), whereas its ``authenticate()`` happily
re-resolves whatever cookie value is currently configured/stored. If
``on_unauthorized`` called ``authenticate()`` unconditionally, a manual
session that had genuinely died server-side would silently come back with a
*new* ``AuthSession`` wrapping the exact same (still-dead) cookie value --
a false "success". Routing through ``refresh()`` instead makes that failure
surface correctly.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests

from ..errors import ConfigError
from ..settings import Settings

#: Seconds-free, timezone-aware clock function type, injectable everywhere in
#: this package for deterministic tests.
ClockFn = Callable[[], datetime]

#: DGT's session TTL, confirmed live (docs/PLAN.md, M4 pre-work): "Session
#: TTL 30 min; authenticated responses send no Set-Cookie, so treat it as
#: absolute". Every provider stamps ``expires_at = obtained_at + SESSION_TTL``.
SESSION_TTL = timedelta(minutes=30)

#: The exact path DGT's BFF redirects an unauthenticated/expired session to
#: (docs/PLAN.md, M4 pre-work: "Unauthenticated/expired session on
#: /download/{token} -> 302 to /auth/login"). Used by :func:`is_login_redirect`.
_LOGIN_REDIRECT_PATH = "/auth/login"


def utcnow() -> datetime:
    """The real, timezone-aware current time -- the default clock everywhere
    in :mod:`cddpt.auth`."""

    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class AuthSession:
    """An authenticated (or manually-supplied) CDD session.

    ``cookies`` holds every cookie captured from the ``cdd.dgterritorio.gov.pt``
    domain during login -- at minimum ``connect.sid`` (the *only* cookie
    actually required to authorize downloads; see docs/PLAN.md's M4
    pre-work), but also whatever else the BFF sets (``auth_session``,
    ``auth_user``, ``auth_email``), since carrying them costs nothing.

    ``source`` records how this session was obtained -- ``"form"``
    (username + password through Keycloak) or ``"manual"`` (a pasted browser
    cookie) -- surfaced by ``cddpt auth status``.

    Deliberately excluded from ``repr()`` (``field(repr=False)``): ``cookies``
    holds a live ``connect.sid`` value, which must never land in a log line,
    an exception message, or a test failure diff.
    """

    cookies: dict[str, str] = field(repr=False)
    obtained_at: datetime
    expires_at: datetime
    source: str

    def is_expired(self, margin: timedelta = timedelta(0), *, now: datetime | None = None) -> bool:
        """True if this session is expired, or will expire within ``margin``.

        ``now`` is injectable (tests pass a fake clock's value); defaults to
        real UTC time via :func:`utcnow`.
        """

        current = now if now is not None else utcnow()
        return current >= (self.expires_at - margin)

    def to_json_dict(self) -> dict[str, Any]:
        """A JSON-safe representation for :class:`~cddpt.auth.store.CredentialStore`."""

        return {
            "cookies": dict(self.cookies),
            "obtained_at": self.obtained_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "source": self.source,
        }

    @classmethod
    def from_json_dict(cls, data: Mapping[str, Any]) -> AuthSession:
        return cls(
            cookies=dict(data["cookies"]),
            obtained_at=datetime.fromisoformat(data["obtained_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"]),
            source=str(data["source"]),
        )


class AuthProvider(Protocol):
    """How :class:`AuthManager` obtains/renews/discards an :class:`AuthSession`.

    Implemented by :class:`~cddpt.auth.form_provider.KeycloakFormAuthProvider`
    and :class:`~cddpt.auth.manual_provider.ManualCookieAuthProvider`.
    """

    def is_available(self) -> bool:
        """True if this provider currently has enough to authenticate with
        (credentials/cookie from an explicit arg, env var, or keyring) --
        without actually attempting a login."""

    def authenticate(self) -> AuthSession:
        """A fresh, cold-start login (no prior session assumed)."""

    def refresh(self, session: AuthSession) -> AuthSession:
        """Renew (or re-derive) a session that is expired or about to
        expire. May raise :class:`~cddpt.errors.SessionExpired` if this
        provider cannot renew without user interaction (see
        :class:`~cddpt.auth.manual_provider.ManualCookieAuthProvider`)."""

    def invalidate(self) -> None:
        """Drop any provider-local cached state (not the stored
        credentials themselves) -- called before a forced re-auth."""


class SessionStore(Protocol):
    """The minimal persistence surface :class:`AuthManager` needs.

    :class:`~cddpt.auth.store.CredentialStore` satisfies this (and does
    more: credential storage, keyring backend introspection, etc.) -- kept
    as a narrow Protocol here so ``base.py`` never needs to import
    ``store.py`` (see this module's docstring).
    """

    def load_session(self) -> AuthSession | None: ...

    def save_session(self, session: AuthSession) -> None: ...

    def clear_session(self) -> None: ...


def is_login_redirect(response: requests.Response) -> bool:
    """True if ``response`` is -- or, if redirects were followed, passed
    through -- DGT's "session expired" signal: a 3xx redirect whose
    ``Location`` path is ``/auth/login``.

    Checks every hop in ``response.history`` as well as ``response`` itself,
    so this works whether the caller made the request with
    ``allow_redirects=False`` (``response`` itself is the 302) or let
    ``requests`` follow the whole chain (the 302-to-``/auth/login`` hop is in
    ``response.history``, and the final ``response`` is Keycloak's 200 login
    page).
    """

    for hop in (*response.history, response):
        if not (300 <= hop.status_code < 400):
            continue
        location = hop.headers.get("Location")
        if location and urlsplit(location).path.rstrip("/") == _LOGIN_REDIRECT_PATH:
            return True
    return False


class AuthManager:
    """The single façade the rest of cddpt uses for authentication.

    See this module's docstring for the caching/refresh/thread-safety
    design. ``clock``/``refresh_margin`` are both injectable for tests.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        provider: AuthProvider,
        store: SessionStore | None = None,
        clock: ClockFn = utcnow,
        refresh_margin: timedelta = timedelta(minutes=3),
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._store = store
        self._clock = clock
        self._refresh_margin = refresh_margin
        self._session: AuthSession | None = None
        self._lock = threading.Lock()

    @property
    def provider(self) -> AuthProvider:
        return self._provider

    def current(self) -> AuthSession:
        """The current, still-valid (with margin) session -- from cache, the
        store, or a fresh provider call, in that order."""

        with self._lock:
            return self._current_locked()

    def apply(self, session: requests.Session) -> None:
        """Set this manager's current session's cookies on ``session``,
        scoped strictly to the CDD site's own domain.

        Explicit ``domain=`` (rather than letting ``requests`` infer scope
        from whatever URL is requested next) is what keeps these cookies
        from ever being sent to the pre-signed S3 download host -- see
        docs/PLAN.md's M4 pre-work: the S3 URL "needs no cookies".
        """

        auth_session = self.current()
        host = urlsplit(self._settings.site_base_url).hostname
        if not host:
            raise ConfigError(
                f"cddpt: settings.site_base_url has no hostname ({self._settings.site_base_url!r})"
            )
        for name, value in auth_session.cookies.items():
            session.cookies.set(name, value, domain=host, path="/", secure=True)

    def on_unauthorized(self, failed_session: AuthSession | None = None) -> AuthSession:
        """Called when a caller observes a session has been rejected (e.g.
        :func:`is_login_redirect` on a download response): invalidate and
        obtain a fresh session.

        Pass the exact :class:`AuthSession` the caller was using when it got
        rejected as ``failed_session``. If another thread has *already*
        replaced :attr:`current`'s cached session by the time this thread
        acquires the lock (because two download threads hit the same dead
        session at once), this returns that already-fresh session directly,
        without authenticating a second time -- ``AuthSession`` instances
        returned by :meth:`current` while the cache is warm are the exact
        same object, so an ``is`` comparison reliably detects "still the
        session that failed" vs. "someone already fixed this".
        """

        with self._lock:
            current = self._session
            if failed_session is not None and current is not None and current is not failed_session:
                return current

            target = current if current is not None else failed_session
            self._provider.invalidate()
            if self._store is not None:
                self._store.clear_session()
            self._session = None

            new_session = (
                self._provider.refresh(target)
                if target is not None
                else self._provider.authenticate()
            )
            self._set_session(new_session)
            return new_session

    def _current_locked(self) -> AuthSession:
        if self._session is not None and not self._is_stale(self._session):
            return self._session

        stored = self._store.load_session() if self._store is not None else None
        if stored is not None and not self._is_stale(stored):
            self._session = stored
            return stored

        base_session = self._session if self._session is not None else stored
        new_session = (
            self._provider.refresh(base_session)
            if base_session is not None
            else self._provider.authenticate()
        )
        self._set_session(new_session)
        return new_session

    def _is_stale(self, session: AuthSession) -> bool:
        return session.is_expired(self._refresh_margin, now=self._clock())

    def _set_session(self, session: AuthSession) -> None:
        self._session = session
        if self._store is not None:
            self._store.save_session(session)


__all__ = [
    "SESSION_TTL",
    "AuthManager",
    "AuthProvider",
    "AuthSession",
    "ClockFn",
    "SessionStore",
    "is_login_redirect",
    "utcnow",
]
