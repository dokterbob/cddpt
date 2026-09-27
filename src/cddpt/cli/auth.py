"""``cddpt auth login|status|logout|doctor``.

Thin formatting/prompting layer over :mod:`cddpt.auth` -- no HTTP/keyring
logic lives here. Every command is wrapped in ``_common.handle_errors``,
which maps an :class:`~cddpt.errors.AuthError` (raised by login/status/
logout on a real failure) to exit code 3 -- see cli/_common.py.

Nothing here ever prints a username, password, or cookie value -- login
only reports "logged in (session valid until HH:MM)", and status reports
*sources* (keyring/env/explicit), never the values themselves.
"""

from __future__ import annotations

import getpass
from datetime import datetime, timezone
from typing import Annotated

import typer
from pydantic import SecretStr
from rich.console import Console
from rich.table import Table

from ..auth.base import AuthManager, AuthProvider, SessionStore
from ..auth.form_provider import KeycloakFormAuthProvider
from ..auth.manual_provider import ManualCookieAuthProvider
from ..auth.probe import doctor as run_doctor
from ..auth.store import CredentialStore
from ..errors import AuthError
from . import _common

app = typer.Typer(name="auth", help="Manage CDD authentication.", no_args_is_help=True)

_stdout = Console()


@app.command("login", help="Log in to CDD and cache the session.")
@_common.handle_errors
def login(
    username: Annotated[
        str | None,
        typer.Option(
            "--username",
            help="CDD username/email (prompted if omitted and CDDPT_USERNAME is unset). "
            "Never pass a password this way -- it is always prompted, never a CLI argument.",
        ),
    ] = None,
    cookie: Annotated[
        bool,
        typer.Option(
            "--cookie",
            help="Use a pasted browser 'connect.sid' cookie instead of username/password "
            "(the value is always prompted, hidden -- never a CLI argument).",
        ),
    ] = False,
    no_save: Annotated[
        bool,
        typer.Option(
            "--no-save",
            help="Don't persist credentials/cookie/session in the system keyring -- only "
            "cache the session in memory for this process.",
        ),
    ] = False,
) -> None:
    settings = _common.build_settings()
    store: CredentialStore | None = None if no_save else CredentialStore()
    provider: AuthProvider

    if cookie:
        raw_cookie = getpass.getpass("Paste your 'connect.sid' cookie value (input hidden): ")
        if not raw_cookie:
            raise AuthError("cddpt: no cookie value entered.")
        cookie_value = SecretStr(raw_cookie)
        if store is not None:
            store.set_manual_cookie(cookie_value)
        provider = ManualCookieAuthProvider(cookie_value=cookie_value, store=store)
    else:
        resolved_username = username if username is not None else settings.username
        if resolved_username is None:
            resolved_username = typer.prompt("CDD username/email")

        password = settings.password
        if password is None:
            entered_password = getpass.getpass("CDD password (input hidden): ")
            password = SecretStr(entered_password)

        if store is not None:
            store.set_username(resolved_username)
            store.set_password(resolved_username, password)

        provider = KeycloakFormAuthProvider(
            settings=settings, username=resolved_username, password=password, store=store
        )

    session_store: SessionStore | None = store
    manager = AuthManager(settings=settings, provider=provider, store=session_store)
    session = manager.current()
    _stdout.print(f"[green]logged in[/green] (session valid until {session.expires_at:%H:%M} UTC)")


@app.command("status", help="Show the current auth session's status.")
@_common.handle_errors
def status() -> None:
    settings = _common.build_settings()
    store = CredentialStore()

    table = Table(show_header=False, title="cddpt auth status")
    table.add_column("Field", style="bold")
    table.add_column("Value")

    session = store.load_session()
    if session is None:
        table.add_row("Session", "none cached")
    else:
        now = datetime.now(timezone.utc)
        if session.is_expired(now=now):
            table.add_row("Session", "expired")
        else:
            remaining_minutes = (session.expires_at - now).total_seconds() / 60
            table.add_row("Session", f"valid ({remaining_minutes:.0f} min remaining)")
        table.add_row("Session source", session.source)

    table.add_row("Keyring backend", store.backend_name())

    if settings.username is not None:
        table.add_row("Credentials", "source: explicit settings/environment variables")
    else:
        try:
            stored_username = store.get_username()
        except AuthError as exc:
            table.add_row("Credentials", f"unavailable ({exc})")
        else:
            table.add_row(
                "Credentials",
                "source: keyring" if stored_username is not None else "none configured",
            )

    _stdout.print(table)


@app.command(
    "logout",
    help="Purge every cddpt keyring entry (password, username, session).",
)
@_common.handle_errors
def logout() -> None:
    store = CredentialStore()
    store.clear_all()
    _stdout.print("[green]logged out[/green] (all cddpt keyring entries purged)")


@app.command(
    "doctor",
    help="Run auth capability checks (TLS, login page, credentials, keyring). ",
)
@_common.handle_errors
def doctor(
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Also perform one real login and validate the resulting session against a "
            "live download-token exchange. Spends one real login and one search; never "
            "downloads or follows the pre-signed URL.",
        ),
    ] = False,
) -> None:
    settings = _common.build_settings()
    report = run_doctor(settings, live=live)

    table = Table(title="cddpt auth doctor")
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("Detail")
    for check in report.checks:
        result = "[green]OK[/green]" if check.ok else "[red]FAIL[/red]"
        table.add_row(check.name, result, check.detail)
    _stdout.print(table)

    if not report.all_ok:
        raise typer.Exit(code=_common.EXIT_ERROR)


__all__ = ["app"]
