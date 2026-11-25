"""Admin sign-in with Google through OpenID Connect, and who is an admin (DESIGN.md §2.11).

A signed-in user's verified email is kept in a signed session cookie (Starlette's `SessionMiddleware`). `AdminMark`
marks each request from an admin; `require_admin` guards the admin routes.
"""

import asyncio
import logging
import os
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx2
from authlib.integrations.starlette_client import OAuth, StarletteOAuth2App
from fastapi import Request, Response, status
from fastapi.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from browl.jobs import STATE_DB
from browl.pages import app_path, href, is_under_any, templates
from browl.settings import Settings

logger = logging.getLogger(__name__)

GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"
"""Google's OpenID Connect discovery document."""

SESSION_COOKIE = "browl_session"
"""The session cookie's name."""

SESSION_MAX_AGE_S = 12 * 60 * 60
"""How long a sign-in lasts: the session cookie's lifetime."""

SESSION_KEY_FILE = "session.key"
"""The file in `DATA_DIR` holding the key that signs session cookies, made on first start."""

CALLBACK_PATH = "oidc-callback"
"""Where Google sends the browser back after sign-in, under `/{RESERVED_PREFIX}/admin/`: register its URL with the
OAuth client (`callback_url`)."""

SESSION_EMAIL = "email"
"""The session's key for the signed-in user's email address."""

SESSION_NEXT = "next"
"""The session's key for the path to return to once signed in."""


class SignInRequiredError(Exception):
    """An admin route was requested by someone not signed in."""


class NotAdminError(Exception):
    """An admin route was requested by a signed-in user who isn't an admin."""


def session_key(data_dir: Path) -> str:
    """Return the key that signs session cookies, making and storing one if there's none yet.

    It's kept in `DATA_DIR`, so sign-ins survive restarts; deleting the file signs everyone out at the next start.
    """
    path = data_dir / SESSION_KEY_FILE
    if path.exists():
        return path.read_text().strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    # Written to a private temporary file, then linked into place, so the key is never readable by others or half
    # written, and a key another process stored first wins.
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
        file.write(key)
    try:
        os.link(temporary, path)
    except FileExistsError:
        key = path.read_text().strip()
    finally:
        temporary.unlink()
    return key


def oidc_client(settings: Settings, transport: httpx2.AsyncBaseTransport | None = None) -> StarletteOAuth2App | None:
    """Return the OpenID Connect client for Google sign-in; None if authentication is off.

    Args:
        settings: The settings, with the OAuth client's ID and secret.
        transport: The transport for its calls to Google, which tests replace; httpx2's default if not given.
    """
    if settings.auth_disabled:
        return None
    assert settings.oidc_client_secret is not None  # the settings require it unless auth is off
    client_kwargs: dict[str, Any] = {"scope": "openid email"}
    if transport is not None:
        client_kwargs["transport"] = transport
    client: StarletteOAuth2App = OAuth().register(
        "google",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret.get_secret_value(),
        server_metadata_url=GOOGLE_METADATA_URL,
        client_kwargs=client_kwargs,
    )
    return client


def callback_url(settings: Settings) -> str:
    """Return the sign-in callback's URL, from `BASE_URL`, not the request, which a proxy may change."""
    return f"{str(settings.base_url).rstrip('/')}/{settings.reserved_prefix}/admin/{CALLBACK_PATH}"


def is_admin(settings: Settings, email: str) -> bool:
    """Return whether a verified, lowercased email address is an admin's: a seed admin's or in the `admin` table."""
    if email in settings.seed_admins:
        return True
    with closing(sqlite3.connect(settings.data_dir / STATE_DB)) as connection:
        return connection.execute("SELECT 1 FROM admin WHERE email = ?", (email,)).fetchone() is not None


def safe_next(value: str | None) -> str | None:
    """Return a path to return to after signing in, if it's one of this app's: None for any other value.

    Examples:
        >>> safe_next("/_browl/admin/status"), safe_next("//evil.example/"), safe_next("https://evil.example/")
        ('/_browl/admin/status', None, None)
    """
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return None
    parts = urlsplit(value)
    return None if parts.scheme or parts.netloc else value


def sign_in_href(request: Request, next_path: str | None = None) -> str:
    """Return the link to sign in, then to return to a path relative to the app's root, if given."""
    link = href(request, f"/{request.app.state.settings.reserved_prefix}/admin/sign-in")
    return f"{link}?{urlencode({'next': next_path})}" if next_path else link


class AdminMark:
    """Record who's signed in (`request.state.user`), and mark requests from admins (`request.state.is_admin`).

    Every page needs the mark, for the navigation bar's admin link. With `AUTH_DISABLED`, everyone is an admin. It
    must run inside `SessionMiddleware`.

    Args:
        app: The application it wraps.
        passthrough: Path prefixes that serve no pages, e.g. static files, so need no mark.
    """

    def __init__(self, app: ASGIApp, passthrough: list[str]) -> None:
        """Wrap the app."""
        self.app = app
        self.passthrough = passthrough

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the request, marked."""
        if scope["type"] == "http" and not is_under_any(app_path(scope), self.passthrough):
            request = Request(scope)
            settings: Settings = request.app.state.settings
            email = request.session.get(SESSION_EMAIL)
            request.state.user = email
            if settings.auth_disabled:
                request.state.is_admin = True
            else:
                # Checked on every request, so removing an admin takes effect at once (P-23).
                request.state.is_admin = email is not None and await asyncio.to_thread(is_admin, settings, email)
        await self.app(scope, receive, send)


async def require_admin(request: Request) -> None:
    """Let only admins through to the admin routes (A-1).

    Raises:
        SignInRequiredError: No one is signed in.
        NotAdminError: The user signed in isn't an admin.
    """
    if getattr(request.state, "is_admin", False) is True:
        return
    if getattr(request.state, "user", None) is not None:
        raise NotAdminError
    raise SignInRequiredError


async def sign_in_required(request: Request, error: Exception) -> Response:
    """Send someone not signed in to sign in, then back to the page they asked for.

    A GET is redirected to sign in; an htmx request, such as the status poll after the session expired, gets
    `HX-Refresh`, so the page reloads and its GET is redirected; any other request gets 401 and a link to sign in.
    """
    headers = {"Cache-Control": "no-store"}
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=status.HTTP_401_UNAUTHORIZED, headers=headers | {"HX-Refresh": "true"})
    if request.method in {"GET", "HEAD"}:
        path = quote(app_path(request.scope))
        if request.url.query:
            path = f"{path}?{request.url.query}"
        return RedirectResponse(sign_in_href(request, path), status.HTTP_303_SEE_OTHER, headers=headers)
    return auth_page(request, "Sign in required", "Sign in to use the admin pages.", status.HTTP_401_UNAUTHORIZED)


async def not_admin(request: Request, error: Exception) -> Response:
    """Refuse a signed-in user who isn't an admin, with 403."""
    message = f"You're signed in as {request.state.user}, which isn't an admin's account."
    return auth_page(request, "Not an admin", message, status.HTTP_403_FORBIDDEN)


def auth_page(request: Request, title: str, message: str, status_code: int) -> Response:
    """Return a page saying why sign-in failed or an admin page was refused, with a link to sign in."""
    return templates.TemplateResponse(
        request,
        "admin/auth.html",
        {"title": title, "message": message, "sign_in": sign_in_href(request)},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )
