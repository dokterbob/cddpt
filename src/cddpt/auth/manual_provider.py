"""``ManualCookieAuthProvider`` -- the paste-a-cookie escape hatch.

docs/PLAN.md's M4 pre-work confirmed a bare ``connect.sid`` value is
sufficient to authorize ``GET /dgt-be/v1/download/{token}`` (no CSRF/Origin
check) -- so a user can copy that cookie's value out of their browser's
DevTools and hand it to cddpt directly, bypassing the Keycloak form entirely.
Useful when the form flow can't be driven automatically (MFA, a CAPTCHA,
DGT changing the login page's markup, ...).

The cookie value is always a **per-run** input -- either ``CDDPT_SESSION_COOKIE``
(via :class:`~cddpt.settings.Settings`) or ``download --cookie``'s hidden
prompt -- never persisted anywhere: it's exactly as short-lived as any other
CDD session (30 minutes, absolute expiry), so there is nothing to gain from
storing it and every reason not to.

Unlike :class:`~cddpt.auth.form_provider.KeycloakFormAuthProvider`, this
provider has no way to silently renew a session: a pasted cookie is exactly
as good as it is, for exactly as long as DGT's server says so, and there is
no credential behind it cddpt could resubmit. :meth:`refresh` therefore
always raises :class:`~cddpt.errors.SessionExpired` rather than pretending
to succeed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from pydantic import SecretStr

from ..errors import AuthError, SessionExpired
from .base import SESSION_TTL, AuthSession, utcnow

#: The single cookie this provider deals in -- see docs/PLAN.md's M4
#: pre-work: "A bare connect.sid is sufficient to authorize /download/{token}".
COOKIE_NAME = "connect.sid"


class ManualCookieAuthProvider:
    """Wraps a user-supplied ``connect.sid`` value as an :class:`AuthSession`.

    ``cookie_value`` is the pasted cookie for this run -- resolved by the
    caller from ``CDDPT_SESSION_COOKIE`` or an interactive prompt, never
    persisted by this provider.
    """

    def __init__(
        self,
        *,
        cookie_value: SecretStr | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._cookie_value = cookie_value
        self._clock = clock

    def is_available(self) -> bool:
        return self._cookie_value is not None

    def authenticate(self) -> AuthSession:
        if self._cookie_value is None:
            raise AuthError(
                "cddpt: no manually-supplied session cookie available. Set "
                "CDDPT_SESSION_COOKIE or pass `download --cookie`."
            )
        now = self._clock()
        return AuthSession(
            cookies={COOKIE_NAME: self._cookie_value.get_secret_value()},
            obtained_at=now,
            expires_at=now + SESSION_TTL,
            source="manual",
        )

    def refresh(self, session: AuthSession) -> AuthSession:
        raise SessionExpired(
            "cddpt: the manually-supplied session cookie has expired (or is "
            "about to). A pasted cookie cannot be renewed automatically -- "
            "supply a fresh 'connect.sid' value via CDDPT_SESSION_COOKIE or "
            "`download --cookie`."
        )

    def invalidate(self) -> None:
        pass


__all__ = ["COOKIE_NAME", "ManualCookieAuthProvider"]
