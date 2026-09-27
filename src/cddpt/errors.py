"""cddpt's exception hierarchy.

Kept small and typed. Every error raised by cddpt's own code (as opposed to
letting a third-party exception propagate) should be one of these.
"""

from __future__ import annotations


class CddError(Exception):
    """Base class for all errors raised by cddpt."""


class ConfigError(CddError):
    """Configuration is missing or invalid (e.g. a bad ``Settings`` value)."""


class HttpError(CddError):
    """An HTTP request failed in a way cddpt could not recover from.

    Carries the offending ``status`` code and ``url`` when known, so callers
    (and the CLI) can report something actionable instead of a bare message.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class TlsVerificationError(HttpError):
    """A server's TLS certificate could not be verified (or, defensively, a
    connection turned out not to be verified at all).

    Never retried: a certificate problem does not fix itself with backoff.
    The usual legitimate cause is a TLS-inspecting corporate proxy whose CA
    is not in the OS trust store -- pass it via ``ca_bundle``
    (``--ca-bundle`` / ``CDDPT_CA_BUNDLE``). cddpt never offers a way to
    disable verification.
    """


class ApiError(CddError):
    """The API returned a response that did not match the expected shape.

    For example: a ``/collections`` envelope missing ``data``, or a STAC
    payload that ``pystac`` refused to parse.
    """


class RateLimited(CddError):
    """The server (or cddpt's own circuit breaker) is refusing new requests
    due to rate limiting."""


class CircuitOpen(CddError):
    """cddpt's own circuit breaker (see :mod:`cddpt.ratelimit`) is open and
    pausing all new requests until its cooldown elapses."""


class AuthError(CddError):
    """Authentication with DGT's Keycloak/BFF failed."""


class SessionExpired(AuthError):
    """A previously-valid session cookie has expired mid-run."""


class InsufficientDiskSpace(CddError):
    """Not enough free disk space to complete a planned download."""


class DownloadError(CddError):
    """A file download failed in a way that could not be resumed/retried."""
