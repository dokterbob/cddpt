"""cddpt's authentication subsystem (Milestone 4).

:class:`~cddpt.auth.base.AuthManager` is the only thing the rest of cddpt
should ever touch for auth -- session caching/expiry/proactive-refresh,
thread-safe re-auth on rejection, and scoping cookies to the CDD domain all
live there. :class:`~cddpt.auth.store.CredentialStore` is the keyring-backed
persistence layer (credentials + the current session); nothing in cddpt
ever falls back to a plaintext file for either.

Two :class:`~cddpt.auth.base.AuthProvider` implementations are provided:
:class:`~cddpt.auth.form_provider.KeycloakFormAuthProvider` (drives DGT's
real Keycloak login form) and
:class:`~cddpt.auth.manual_provider.ManualCookieAuthProvider` (a pasted
``connect.sid`` value, for when the form flow can't be used).
:func:`~cddpt.auth.probe.doctor` backs ``cddpt auth doctor``.
"""

from __future__ import annotations

from .base import (
    SESSION_TTL,
    AuthManager,
    AuthProvider,
    AuthSession,
    SessionStore,
    is_login_redirect,
)
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
    "SessionStore",
    "doctor",
    "is_login_redirect",
]
