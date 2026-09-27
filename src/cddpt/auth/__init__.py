"""cddpt's authentication subsystem (Milestone 4).

:class:`~cddpt.auth.base.AuthManager` is the only thing the rest of cddpt
should ever touch for auth -- session caching/expiry/proactive-refresh,
thread-safe re-auth on rejection, and scoping cookies to the CDD domain all
live there. The CDD session cookie itself is kept **in memory only**, for
the lifetime of one process -- it's a short-lived credential (30-minute
absolute expiry) with nothing to gain from persisting it.
:class:`~cddpt.auth.store.CredentialStore` is the keyring-backed persistence
layer for the long-lived credential (username + password) that makes
unattended re-login possible; nothing in cddpt ever falls back to a
plaintext file for it.

Two :class:`~cddpt.auth.base.AuthProvider` implementations are provided:
:class:`~cddpt.auth.form_provider.KeycloakFormAuthProvider` (drives DGT's
real Keycloak login form) and
:class:`~cddpt.auth.manual_provider.ManualCookieAuthProvider` (a pasted
``connect.sid`` value, for when the form flow can't be used).
:func:`~cddpt.auth.probe.doctor` backs ``cddpt auth doctor``.
"""

from __future__ import annotations

from .base import SESSION_TTL, AuthManager, AuthProvider, AuthSession, is_login_redirect
from .form_provider import KeycloakFormAuthProvider
from .manual_provider import ManualCookieAuthProvider
from .probe import CheckResult, DoctorReport, doctor
from .store import CredentialStore

__all__ = [
    "SESSION_TTL",
    "AuthManager",
    "AuthProvider",
    "AuthSession",
    "CheckResult",
    "CredentialStore",
    "DoctorReport",
    "KeycloakFormAuthProvider",
    "ManualCookieAuthProvider",
    "doctor",
    "is_login_redirect",
]
