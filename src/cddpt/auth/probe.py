"""``cddpt auth doctor`` -- capability probe and regression detector.

Runs a battery of cheap, mostly read-only checks against the real CDD/
Keycloak hosts (TLS reachability, login-page parseability, credential/
keyring availability) plus a standing regression detector for one of
docs/PLAN.md's "Open items requiring a human": whether DGT has ever enabled
a native OAuth path for the ``aai-oidc-dgt`` client (it is confidential and
rejects loopback redirect URIs today -- see "Auth design"). Only the
``live=True`` path performs a real login; everything else is safe to run
often, including in CI against a mocked transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

from ..errors import AuthError
from ..http import make_session
from ..ratelimit import RequestGovernor
from ..settings import Settings
from .base import AuthManager
from .form_provider import KeycloakFormAuthProvider, parse_login_page
from .store import CredentialStore

#: Field names docs/PLAN.md's M4 pre-work found on Keycloak's real login form.
_EXPECTED_LOGIN_FIELDS = frozenset({"username", "password", "credentialId"})

#: The exact probe docs/PLAN.md's Deliverables section specifies: a loopback
#: redirect_uri against the real authorize endpoint, expected to keep being
#: rejected (400) since aai-oidc-dgt is a confidential client.
_REGRESSION_REDIRECT_URI = "http://127.0.0.1:8765/callback"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One named pass/fail check, with a human-readable (never secret)
    ``detail`` string."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[CheckResult, ...]

    @property
    def all_ok(self) -> bool:
        return all(check.ok for check in self.checks)


def _check_reachable(session: requests.Session, url: str, name: str) -> CheckResult:
    host = urlsplit(url).hostname or url
    try:
        response = session.get(url, allow_redirects=False)
    except requests.exceptions.RequestException as exc:
        return CheckResult(name, False, f"{exc.__class__.__name__} connecting to {host}")
    return CheckResult(name, True, f"HTTP {response.status_code} from {host}")


def _check_login_page(session: requests.Session, settings: Settings) -> CheckResult:
    login_url = f"{settings.site_base_url}/auth/login"
    try:
        response = session.get(login_url)
    except requests.exceptions.RequestException as exc:
        return CheckResult("login_page", False, f"{exc.__class__.__name__} fetching the login page")

    try:
        parsed = parse_login_page(response.text, response.url)
    except AuthError as exc:
        return CheckResult("login_page", False, str(exc))

    missing = _EXPECTED_LOGIN_FIELDS - parsed.field_names
    if missing:
        return CheckResult(
            "login_page",
            False,
            f"login form found, but missing expected field(s): {sorted(missing)}",
        )
    return CheckResult("login_page", True, f"kc-form-login parsed OK (HTTP {response.status_code})")


def _check_credentials(settings: Settings, store: CredentialStore) -> CheckResult:
    if settings.username is not None and settings.password is not None:
        return CheckResult("credentials", True, "source: explicit settings/environment variables")
    try:
        username = store.get_username()
    except AuthError as exc:
        return CheckResult("credentials", False, f"keyring unusable: {exc}")
    if username is not None:
        return CheckResult("credentials", True, "source: keyring")
    return CheckResult(
        "credentials",
        False,
        "no credentials found (set CDDPT_USERNAME/CDDPT_PASSWORD, or run `cddpt auth login`)",
    )


def _check_keyring_backend(store: CredentialStore) -> CheckResult:
    name = store.backend_name()
    if name.startswith("<unavailable"):
        return CheckResult("keyring_backend", False, name)
    return CheckResult("keyring_backend", True, f"backend: {name}")


def _check_oauth_regression(session: requests.Session, settings: Settings) -> CheckResult:
    url = f"{settings.auth_base_url}/realms/{settings.keycloak_realm}/protocol/openid-connect/auth"
    params = {
        "client_id": settings.keycloak_client_id,
        "response_type": "code",
        "redirect_uri": _REGRESSION_REDIRECT_URI,
        "scope": "openid",
    }
    try:
        response = session.get(url, params=params, allow_redirects=False)
    except requests.exceptions.RequestException as exc:
        return CheckResult(
            "oauth_regression",
            False,
            f"{exc.__class__.__name__} probing the Keycloak authorize endpoint",
        )
    if response.status_code == 400:
        return CheckResult(
            "oauth_regression",
            True,
            "loopback redirect_uri still rejected (HTTP 400), as expected",
        )
    return CheckResult(
        "oauth_regression",
        False,
        f"loopback redirect_uri returned HTTP {response.status_code} (expected 400) -- "
        "DGT may have enabled native OAuth for this client; please file an issue.",
    )


def _validate_session_download(session: requests.Session, settings: Settings) -> CheckResult:
    """Mint one fresh download token (via a tiny ``/search``) and confirm the
    live session authorizes the token -> pre-signed-URL exchange, WITHOUT
    ever following that redirect (never transfers any bytes).

    See docs/PLAN.md's M4 pre-work: "Authorized session + fresh token ->
    302 to a pre-signed S3 URL"; a download token is single-use, so this
    necessarily spends one on every ``--live`` run.
    """

    try:
        collections_response = session.get(f"{settings.api_base_url}/collections")
        collections_response.raise_for_status()
        payload = collections_response.json()
        collection_ids = [
            c["id"]
            for c in payload["data"]["collections"]
            if "show" in (c.get("summaries", {}).get("visibility") or [])
        ]
        if not collection_ids:
            return CheckResult(
                "live_download_gate", False, "no downloadable collections to probe with"
            )

        search_response = session.post(
            f"{settings.api_base_url}/search",
            json={"collections": collection_ids[:1], "limit": 1},
        )
        search_response.raise_for_status()
        features = search_response.json().get("features") or []
        if not features:
            return CheckResult(
                "live_download_gate",
                False,
                f"no items found in {collection_ids[0]!r} to probe with",
            )

        assets = features[0].get("assets") or {}
        asset = next(
            (a for a in assets.values() if "data" in (a.get("roles") or [])),
            next(iter(assets.values()), None),
        )
        if asset is None or "href" not in asset:
            return CheckResult("live_download_gate", False, "matched item has no asset href")

        site_host = urlsplit(settings.site_base_url).hostname
        download_response = session.get(asset["href"], allow_redirects=False)
        location = download_response.headers.get("Location", "")
        redirect_host = urlsplit(location).hostname
        if (
            300 <= download_response.status_code < 400
            and redirect_host
            and redirect_host != site_host
        ):
            return CheckResult(
                "live_download_gate",
                True,
                f"authorized session: token exchange returned HTTP "
                f"{download_response.status_code} to a non-CDD host, as expected "
                "(pre-signed URL not followed)",
            )
        return CheckResult(
            "live_download_gate",
            False,
            f"unexpected response validating the session: HTTP {download_response.status_code} "
            f"(Location host: {redirect_host!r})",
        )
    except (requests.exceptions.RequestException, KeyError, ValueError, TypeError) as exc:
        return CheckResult(
            "live_download_gate", False, f"{exc.__class__.__name__} while validating the session"
        )


def doctor(
    settings: Settings | None = None,
    *,
    store: CredentialStore | None = None,
    session: requests.Session | None = None,
    live: bool = False,
) -> DoctorReport:
    """Run every capability check, returning structured (never secret)
    results. Pass ``live=True`` to additionally perform one real login and
    validate the resulting session -- otherwise entirely safe to run often.
    """

    resolved_settings = settings if settings is not None else Settings()
    resolved_store = store if store is not None else CredentialStore()
    governor = RequestGovernor.from_settings(resolved_settings)
    http_session = (
        session if session is not None else make_session(resolved_settings, governor=governor)
    )

    checks = [
        _check_reachable(http_session, resolved_settings.site_base_url, "cdd_site_reachable"),
        _check_reachable(http_session, resolved_settings.auth_base_url, "keycloak_reachable"),
        _check_login_page(http_session, resolved_settings),
        _check_credentials(resolved_settings, resolved_store),
        _check_keyring_backend(resolved_store),
        _check_oauth_regression(http_session, resolved_settings),
    ]

    if live:
        provider = KeycloakFormAuthProvider(
            settings=resolved_settings,
            session=http_session,
            governor=governor,
            store=resolved_store,
        )
        manager = AuthManager(settings=resolved_settings, provider=provider)
        try:
            auth_session = manager.current()
        except AuthError as exc:
            checks.append(CheckResult("live_login", False, str(exc)))
            checks.append(CheckResult("live_download_gate", False, "skipped (login failed)"))
        else:
            manager.apply(http_session)
            checks.append(
                CheckResult(
                    "live_login",
                    True,
                    f"logged in; session valid until {auth_session.expires_at:%H:%M} UTC",
                )
            )
            checks.append(_validate_session_download(http_session, resolved_settings))

    return DoctorReport(checks=tuple(checks))


__all__ = ["CheckResult", "DoctorReport", "doctor"]
