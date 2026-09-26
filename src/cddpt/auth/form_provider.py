"""``KeycloakFormAuthProvider`` -- logs in by POSTing DGT's real Keycloak
login form, exactly as a browser would.

Flow (docs/PLAN.md's M4 pre-work, empirically confirmed against the live
site): ``GET {site}/auth/login`` -> 302 -> Keycloak's authorize endpoint
(200, the login page) -> parse ``<form id="kc-form-login">`` -> POST
``username``/``password``/hidden fields to the form's ``action`` -> 302 ->
``{site}/auth/callback`` (sets ``connect.sid`` et al.) -> 302 ->
``{site}/dgt-fe/downloads`` (200). ``requests`` follows every redirect hop
on its own; this module never needs to know the intermediate Keycloak URLs
by name.

Credential resolution order: an explicit ``username``/``password`` passed to
the constructor, else ``settings.username``/``settings.password`` (which
pydantic-settings already resolves from ``CDDPT_USERNAME``/``CDDPT_PASSWORD``
-- see ``settings.py``), else a caller-supplied
:class:`~cddpt.auth.store.CredentialStore`.

Never Playwright, never a headless browser -- this is a handful of
``requests`` calls plus BeautifulSoup4 parsing, per docs/PLAN.md's explicit
"Auth design" choice.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag
from pydantic import SecretStr

from ..errors import AuthError
from ..http import make_session
from ..ratelimit import RequestGovernor
from ..settings import Settings
from .base import SESSION_TTL, AuthSession, utcnow
from .store import CredentialStore

logger = logging.getLogger(__name__)

#: The id `docs/PLAN.md` (and live probing) found on Keycloak's real login
#: form.
_LOGIN_FORM_ID = "kc-form-login"

#: Keycloak "required action" forms that mean the login can't be completed
#: by a single username+password POST -- detected by form id, named in the
#: resulting AuthError so a human knows what to go do in a real browser.
_UNEXPECTED_FORM_IDS: dict[str, str] = {
    "kc-otp-login-form": "a one-time-password (OTP/MFA) code",
    "kc-passwd-update-form": "a forced password update/reset",
    "kc-update-profile-form": "a forced profile update",
    "kc-terms-form": "accepting updated terms and conditions",
}

#: Selectors Keycloak's theme uses to render a login error message -- tried
#: in order; the first one that matches and has text wins. Kept as a list
#: (rather than a single selector) since docs/PLAN.md flags this as
#: something to verify empirically and Keycloak's exact theme markup is not
#: itself part of any stable contract.
_ERROR_SELECTORS = ("#input-error", ".kc-feedback-text", ".alert-error")


@dataclass(frozen=True, slots=True)
class ParsedLoginForm:
    """The bits of a parsed Keycloak login page :class:`KeycloakFormAuthProvider`
    (and :mod:`cddpt.auth.probe`) need."""

    action_url: str
    hidden_fields: dict[str, str]
    field_names: frozenset[str]


def _check_for_unexpected_page(soup: BeautifulSoup, page_url: str) -> None:
    for form_id, description in _UNEXPECTED_FORM_IDS.items():
        if soup.find("form", id=form_id) is not None:
            raise AuthError(
                f"cddpt: Keycloak is asking for {description} (form #{form_id} at "
                f"{urlsplit(page_url).path}), which cddpt's automated login cannot "
                "complete. Log in once via a real browser to clear this, then retry."
            )


def _find_login_form(soup: BeautifulSoup) -> Tag | None:
    form = soup.find("form", id=_LOGIN_FORM_ID)
    if isinstance(form, Tag):
        return form

    # Fall back to any form containing a password input -- DGT may have
    # changed Keycloak's theme/markup without changing the field names.
    for candidate in soup.find_all("form"):
        if not isinstance(candidate, Tag):
            continue
        if candidate.find("input", attrs={"type": "password"}) is not None:
            return candidate
    return None


def parse_login_page(html: str, page_url: str) -> ParsedLoginForm:
    """Parse a Keycloak login page's HTML into a :class:`ParsedLoginForm`.

    Raises :class:`~cddpt.errors.AuthError` for an unexpected required-action
    page (MFA/OTP/terms/password update -- detected by form id) or for a
    page with no recognisable login form at all. Used by both
    :class:`KeycloakFormAuthProvider` and :mod:`cddpt.auth.probe`.
    """

    soup = BeautifulSoup(html, "lxml")
    _check_for_unexpected_page(soup, page_url)

    form = _find_login_form(soup)
    if form is None:
        raise AuthError(
            f"cddpt: could not find a login form on the Keycloak page at "
            f"{urlsplit(page_url).path} -- DGT may have changed their login "
            "page markup."
        )
    if form.get("id") != _LOGIN_FORM_ID:
        logger.warning(
            "cddpt: form#%s not found on Keycloak's login page (%s); falling "
            "back to the first form containing a password field (id=%r). "
            "DGT may have changed their login page markup.",
            _LOGIN_FORM_ID,
            urlsplit(page_url).path,
            form.get("id"),
        )

    action = form.get("action")
    if not action or not isinstance(action, str):
        raise AuthError(f"cddpt: the login form at {urlsplit(page_url).path} has no 'action' URL.")
    action_url = urljoin(page_url, action)

    hidden_fields: dict[str, str] = {}
    field_names: set[str] = set()
    for input_tag in form.find_all("input"):
        if not isinstance(input_tag, Tag):
            continue
        name = input_tag.get("name")
        if not name or not isinstance(name, str):
            continue
        field_names.add(name)
        input_type = input_tag.get("type")
        if input_type == "hidden":
            value = input_tag.get("value")
            hidden_fields[name] = value if isinstance(value, str) else ""

    return ParsedLoginForm(
        action_url=action_url, hidden_fields=hidden_fields, field_names=frozenset(field_names)
    )


def _extract_login_error(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    for selector in _ERROR_SELECTORS:
        element = soup.select_one(selector)
        if element is not None:
            text = element.get_text(strip=True)
            if text:
                return text
    return None


class KeycloakFormAuthProvider:
    """Logs in to CDD by POSTing username/password to DGT's real Keycloak
    login form, using a governed :mod:`requests` session and BeautifulSoup4.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session: requests.Session | None = None,
        governor: RequestGovernor | None = None,
        username: str | None = None,
        password: SecretStr | None = None,
        store: CredentialStore | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._settings = settings if settings is not None else Settings()
        self._governor = (
            governor if governor is not None else RequestGovernor.from_settings(self._settings)
        )
        self._session = (
            session
            if session is not None
            else make_session(self._settings, governor=self._governor)
        )
        self._explicit_username = username
        self._explicit_password = password
        self._store = store
        self._clock = clock

    def is_available(self) -> bool:
        try:
            self._resolve_credentials()
        except AuthError:
            return False
        return True

    def authenticate(self) -> AuthSession:
        username, password = self._resolve_credentials()
        return self._login(username, password)

    def refresh(self, session: AuthSession) -> AuthSession:
        # Keycloak's confidential client + no refresh-token path (see
        # docs/PLAN.md's "Auth design") means there is no partial refresh --
        # a full form login is the only way to renew.
        return self.authenticate()

    def invalidate(self) -> None:
        # Nothing cached locally beyond the governed session's own cookie
        # jar, which a fresh authenticate() naturally overwrites.
        pass

    def _resolve_credentials(self) -> tuple[str, SecretStr]:
        if self._explicit_username is not None and self._explicit_password is not None:
            return self._explicit_username, self._explicit_password

        if self._settings.username is not None and self._settings.password is not None:
            return self._settings.username, self._settings.password

        if self._store is not None:
            username = self._store.get_username()
            if username is not None:
                password = self._store.get_password(username)
                if password is not None:
                    return username, password

        raise AuthError(
            "cddpt: no CDD credentials available. Pass --username (with a "
            "password prompt), set CDDPT_USERNAME/CDDPT_PASSWORD, or run "
            "`cddpt auth login` once to store them in the system keyring."
        )

    def _login(self, username: str, password: SecretStr) -> AuthSession:
        login_url = f"{self._settings.site_base_url}/auth/login"
        try:
            response = self._session.get(login_url)
        except requests.exceptions.RequestException as exc:
            raise AuthError(
                f"cddpt: network error reaching {urlsplit(login_url).hostname}: "
                f"{exc.__class__.__name__}"
            ) from exc

        parsed_form = parse_login_page(response.text, response.url)

        payload = dict(parsed_form.hidden_fields)
        payload["username"] = username
        payload["password"] = password.get_secret_value()
        payload.setdefault("credentialId", "")

        try:
            post_response = self._session.post(parsed_form.action_url, data=payload)
        except requests.exceptions.RequestException as exc:
            raise AuthError(
                f"cddpt: network error submitting the Keycloak login form: {exc.__class__.__name__}"
            ) from exc
        finally:
            # The payload (including the plaintext password) must not
            # outlive this call -- drop our only reference to it.
            del payload

        return self._session_from_response(post_response)

    def _session_from_response(self, response: requests.Response) -> AuthSession:
        site_host = urlsplit(self._settings.site_base_url).hostname
        landed_on_site = urlsplit(response.url).hostname == site_host
        cdd_cookies = self._extract_cdd_cookies(site_host)

        if landed_on_site and "connect.sid" in cdd_cookies:
            now = self._clock()
            return AuthSession(
                cookies=cdd_cookies, obtained_at=now, expires_at=now + SESSION_TTL, source="form"
            )

        # Not a successful landing -- figure out why, without ever
        # including the submitted credentials in the resulting message.
        if "text/html" in response.headers.get("Content-Type", ""):
            # This also raises AuthError itself for a recognised
            # required-action page (MFA/OTP/terms/password update).
            _check_for_unexpected_page(BeautifulSoup(response.text, "lxml"), response.url)
            error_message = _extract_login_error(response.text)
            if error_message:
                raise AuthError(f"cddpt: login failed: {error_message}")

        raise AuthError(
            "cddpt: login did not complete as expected -- did not land back on "
            f"{site_host} with a session cookie set (final page: "
            f"{urlsplit(response.url).path!r}, HTTP {response.status_code})."
        )

    def _extract_cdd_cookies(self, site_host: str | None) -> dict[str, str]:
        cookies: dict[str, str] = {}
        if site_host is None:
            return cookies
        for cookie in self._session.cookies:
            cookie_domain = (cookie.domain or "").lstrip(".")
            if cookie_domain == site_host:
                cookies[cookie.name] = cookie.value or ""
        return cookies


__all__ = ["KeycloakFormAuthProvider", "ParsedLoginForm", "parse_login_page"]
