"""Admin sign-in with Google through OpenID Connect, and who is an admin (DESIGN.md §2.11).

A signed-in user's verified email is kept in a signed session cookie (Starlette's `SessionMiddleware`), with when they
signed in. `AdminMark` marks each request from an admin; `require_admin` guards the admin routes. The routes here,
signing in and out, are open to everyone.
"""

import asyncio
import logging
import os
import secrets
import sqlite3
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx2
from authlib.integrations.starlette_client import OAuth, OAuthError, StarletteOAuth2App
from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from joserfc.errors import JoseError
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

from browl.admins import AdminStore
from browl.pages import app_path, href, is_htmx, is_under_any, route_href, same_origin, templates
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

SESSION_KEY_CHARS = 43
"""The length of the session key `session_key` makes, `secrets.token_urlsafe(32)`; a shorter stored key is refused."""

TOKEN_EXCHANGES_PER_MINUTE = 30
"""How many token exchanges with Google the sign-in callback may make in any minute, app-wide (`ExchangeLimit`)."""

_SESSION_EMAIL = "email"
"""The session's key for the signed-in user's email address."""

_SESSION_SIGNED_IN_AT = "signed_in_at"
"""The session's key for when the user signed in, in ISO 8601: a sign-out since then ends the session."""

_SESSION_NEXT = "next"
"""The session's key for the path to return to once signed in."""


class SignInRequiredError(Exception):
    """An admin route was requested by someone not signed in."""


class NotAdminError(Exception):
    """An admin route was requested by a signed-in user who isn't an admin."""


def session_key(data_dir: Path) -> str:
    """Return the key that signs session cookies, making and storing one if there's none yet.

    It's kept in `DATA_DIR`, so sign-ins survive restarts; deleting the file signs everyone out at the next start.

    Raises:
        RuntimeError: The stored key is shorter than `SESSION_KEY_CHARS`, e.g. an emptied file, which would let anyone
            sign a session.
    """
    path = data_dir / SESSION_KEY_FILE
    if path.exists():
        return _read_key(path)
    data_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    # Written to a private temporary file, synced, then linked into place, so the key is never readable by others or
    # half written, even after a crash, and a key another process stored first wins.
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
        file.write(key)
        file.flush()
        os.fsync(file.fileno())
    try:
        os.link(temporary, path)
    except FileExistsError:
        key = _read_key(path)
    finally:
        temporary.unlink()
    return key


def _read_key(path: Path) -> str:
    key = path.read_text().strip()
    if len(key) < SESSION_KEY_CHARS:
        raise RuntimeError(
            f"The session key in {path} is {len(key)} characters, under {SESSION_KEY_CHARS}: delete the file, and"
            " browl makes a new one at its next start (everyone signed in is signed out)"
        )
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


class ExchangeLimit:
    """An app-wide cap on the callback's token exchanges with Google, so anonymous requests can't make many (C11).

    A sliding window, kept in memory: the app runs one worker. There's no per-client tracking, so a flood also holds
    back real sign-ins, for a minute at most.

    Args:
        per_minute: How many exchanges any 60 seconds may have.
    """

    def __init__(self, per_minute: int = TOKEN_EXCHANGES_PER_MINUTE) -> None:
        """Start with no exchanges."""
        self.per_minute = per_minute
        self._times: deque[float] = deque()

    def allow(self) -> bool:
        """Return whether an exchange may go ahead now, counting it if so."""
        now = time.monotonic()
        while self._times and now - self._times[0] >= 60:
            self._times.popleft()
        if len(self._times) >= self.per_minute:
            return False
        self._times.append(now)
        return True


def callback_url(app: Starlette) -> str:
    """Return the sign-in callback's URL, from `BASE_URL`, not the request, which a proxy may change."""
    settings: Settings = app.state.settings
    return f"{str(settings.base_url).rstrip('/')}{app.url_path_for('oidc_callback')}"


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
    return route_href(request, "sign_in", {"next": next_path} if next_path else None)


def _upload_path(request: Request) -> str:
    """Return the upload page's path relative to the app's root, where signing in leads by default."""
    return str(request.app.url_path_for("upload_page"))


class AdminMark:
    """Record who's signed in (`request.state.user`), and mark requests from admins (`request.state.is_admin`).

    Every page needs the mark, for the navigation bar's admin link. With `AUTH_DISABLED`, everyone is an admin. It
    must run inside `SessionMiddleware`. A session from before its account last signed out is cleared, so signing out
    ends the account's sessions on every device. If the database can't be read, the user counts as not an admin.

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
            if settings.auth_disabled:
                request.state.user = request.session.get(_SESSION_EMAIL)
                request.state.is_admin = True
            else:
                await self._mark(request, settings)
        await self.app(scope, receive, send)

    @staticmethod
    async def _mark(request: Request, settings: Settings) -> None:
        request.state.user, request.state.is_admin = None, False
        email = request.session.get(_SESSION_EMAIL)
        if email is None:
            return  # no database read
        signed_in_at = request.session.get(_SESSION_SIGNED_IN_AT)
        if signed_in_at is None:  # a session from before sign-in times were kept
            request.session.clear()
            return
        store: AdminStore = request.app.state.admins
        try:
            # Read on every request, so removing an admin (P-23), or signing out elsewhere, takes effect at once.
            account = await asyncio.to_thread(store.account, email)
        except sqlite3.Error:
            logger.exception("Couldn't read the admins from %s; treating the user as not an admin", store.path)
            request.state.user = email
            return
        if account.signed_out_at is not None and datetime.fromisoformat(signed_in_at) <= account.signed_out_at:
            request.session.clear()
            return
        request.state.user = email
        request.state.is_admin = email in settings.seed_admins or account.listed


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
    if is_htmx(request):
        return Response(status_code=status.HTTP_401_UNAUTHORIZED, headers=headers | {"HX-Refresh": "true"})
    if request.method == "GET":
        path = quote(app_path(request.scope))
        if request.url.query:
            path = f"{path}?{request.url.query}"
        return RedirectResponse(sign_in_href(request, path), status.HTTP_303_SEE_OTHER, headers=headers)
    return auth_page(request, "Sign in required", "Sign in to use the admin pages.", status.HTTP_401_UNAUTHORIZED)


async def not_admin(request: Request, error: Exception) -> Response:
    """Refuse a signed-in user who isn't an admin, with 403.

    An htmx request, such as the status poll of an admin removed meanwhile, gets `HX-Refresh`, so the page reloads
    and shows the refusal, rather than polling on with stale content.
    """
    if is_htmx(request):
        headers = {"Cache-Control": "no-store", "HX-Refresh": "true"}
        return Response(status_code=status.HTTP_403_FORBIDDEN, headers=headers)
    message = f"You're signed in as {request.state.user}, which isn't an admin's account."
    return auth_page(request, "Not an admin", message, status.HTTP_403_FORBIDDEN)


def auth_page(
    request: Request, title: str, message: str, status_code: int, headers: dict[str, str] | None = None
) -> Response:
    """Return a page saying why sign-in failed or an admin page was refused, with a link to sign in."""
    return templates.TemplateResponse(
        request,
        "admin/auth.html",
        {"title": title, "message": message, "sign_in": sign_in_href(request)},
        status_code=status_code,
        headers={"Cache-Control": "no-store"} | (headers or {}),
    )


router = APIRouter(prefix="/admin", include_in_schema=False)
"""Signing in and out, under `/{RESERVED_PREFIX}/admin` with the admin routes, but open to everyone."""


@router.get("/sign-in")
async def sign_in(request: Request, next: str | None = None) -> Response:
    """Send the browser to Google to sign in, then back to `next`, a path relative to the app's root (A-1).

    Without `next`, or with one that isn't a path of this app, it returns to the upload page. An admin already
    signed in, or anyone with `AUTH_DISABLED`, goes straight there.
    """
    target = safe_next(next) or _upload_path(request)
    if request.state.is_admin:
        return RedirectResponse(href(request, target), status.HTTP_303_SEE_OTHER)
    client: StarletteOAuth2App = request.app.state.oidc
    request.session[_SESSION_NEXT] = target
    try:
        # select_account, so someone signed in to Google with an account that isn't an admin's can pick another.
        response: Response = await client.authorize_redirect(
            request, callback_url(request.app), prompt="select_account"
        )
    except httpx2.HTTPError:
        logger.exception("Couldn't reach Google to start signing in")
        return auth_page(
            request, "Sign-in failed", "Couldn't reach Google. Try again later.", status.HTTP_502_BAD_GATEWAY
        )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/oidc-callback")
async def oidc_callback(request: Request) -> Response:
    """Finish signing in: Google sends the browser back here with an authorization code (A-1).

    The ID token Google returns for the code gives the user's email address, which must be verified. Then the browser
    returns to the path the sign-in started with; it's refused there if the address isn't an admin's. An admin
    already signed in, e.g. back from a second sign-in tab, or with Back, goes straight there, as Authlib keeps only
    the latest sign-in's state.
    """
    headers = {"Cache-Control": "no-store"}
    if request.state.is_admin:  # always, with AUTH_DISABLED
        target = safe_next(request.session.pop(_SESSION_NEXT, None)) or _upload_path(request)
        return RedirectResponse(href(request, target), status.HTTP_303_SEE_OTHER, headers=headers)
    # Only a callback with a code makes Authlib call Google; one with `error`, or without a code, is refused first.
    limit: ExchangeLimit = request.app.state.exchange_limit
    if "code" in request.query_params and "error" not in request.query_params and not limit.allow():
        logger.warning("Refused a sign-in: over %d a minute", limit.per_minute)
        message = "Too many sign-ins right now. Try again in a minute."
        return auth_page(request, "Sign-in failed", message, status.HTTP_429_TOO_MANY_REQUESTS, {"Retry-After": "60"})
    client: StarletteOAuth2App = request.app.state.oidc
    try:
        token: dict[str, Any] = await client.authorize_access_token(request)
    except (OAuthError, JoseError) as error:  # JoseError: an ID token that's invalid, e.g. with the wrong nonce
        logger.warning("Sign-in failed: %r", str(error))  # repr, as anyone can send Google's error text, e.g. newlines
        message = "Google didn't confirm the sign-in. Try again."
        return auth_page(request, "Sign-in failed", message, status.HTTP_400_BAD_REQUEST)
    except httpx2.HTTPError:
        logger.exception("Couldn't reach Google to finish signing in")
        return auth_page(
            request, "Sign-in failed", "Couldn't reach Google. Try again later.", status.HTTP_502_BAD_GATEWAY
        )
    claims: dict[str, Any] = token.get("userinfo") or {}
    email = claims.get("email")
    if not isinstance(email, str) or claims.get("email_verified") is not True:
        message = "Google didn't give a verified email address."
        return auth_page(request, "Sign-in failed", message, status.HTTP_400_BAD_REQUEST)
    email = email.lower()
    target = safe_next(request.session.pop(_SESSION_NEXT, None)) or _upload_path(request)
    request.session[_SESSION_EMAIL] = email
    request.session[_SESSION_SIGNED_IN_AT] = datetime.now(UTC).isoformat()
    logger.info("Signed in: %s", email)
    return RedirectResponse(href(request, target), status.HTTP_303_SEE_OTHER, headers=headers)


@router.post("/sign-out", dependencies=[Depends(same_origin)])
async def sign_out(request: Request) -> RedirectResponse:
    """Sign out, on every device, and go to the site's root.

    The time is stored for the account, so its other sessions, and any copy of this one's cookie, are refused from
    now on (`AdminMark`). If it can't be stored, this browser is still signed out.
    """
    email = request.state.user
    request.session.clear()
    if email is not None and not request.app.state.settings.auth_disabled:
        store: AdminStore = request.app.state.admins
        try:
            await asyncio.to_thread(store.sign_out, email, datetime.now(UTC))
        except sqlite3.Error:
            logger.exception("Couldn't record a sign-out in %s; other sessions stay signed in", store.path)
    return RedirectResponse(href(request, "/"), status.HTTP_303_SEE_OTHER)
