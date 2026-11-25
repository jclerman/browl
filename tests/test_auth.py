"""Tests for admin sign-in and the admin routes' protection (P-22; A-1, O-6, O-7; DESIGN.md §2.11)."""

import importlib
import logging
import os
import pkgutil
import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey

import browl
import browl.auth
from browl import auth
from browl.admins import AdminStore
from browl.auth import (
    GOOGLE_METADATA_URL,
    SESSION_COOKIE,
    SESSION_KEY_CHARS,
    SESSION_KEY_FILE,
    ExchangeLimit,
    callback_url,
    safe_next,
    session_key,
)
from browl.derived import EntityType
from browl.jobs import STATE_DB, Stage
from tests.helpers import AUTH_ON, derived_stage, make_app, session_cookie

ISSUER = "https://accounts.google.com"
AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
JWKS = "https://www.googleapis.com/oauth2/v3/certs"
SIGN_IN = "/_browl/admin/sign-in"
CALLBACK = "/_browl/admin/oidc-callback"
SIGN_OUT = "/_browl/admin/sign-out"
UPLOAD = "/_browl/admin/upload"
STATUS = "/_browl/admin/status"
SEED = "seed@example.org"
VISITOR = "visitor@example.org"
ADMIN_LINK = 'class="admin-link"'
PUBLIC_PAGES = ["/onto", "/_browl/tree/classes", "/_browl/search"]
"""The ontology's home page, a tree page, and the search page, all served once the fixtures' job has run."""


@pytest.fixture(scope="session")
def signing_key() -> RSAKey:
    """Return the fake Google's key for signing ID tokens."""
    return RSAKey.generate_key(2048, parameters={"kid": "test"})


class FakeGoogle:
    """Google's OpenID Connect endpoints, as an httpx2 transport: it issues an ID token for whoever `user` says.

    Attributes:
        key: The key it signs ID tokens with.
        email: The email address the next ID token gives.
        verified: Whether that address is verified.
        nonce: The nonce the next ID token carries, which the sign-in redirect gave.
        down: Whether to fail every call, as if Google couldn't be reached.
        token_requests: The token requests' forms.
    """

    def __init__(self, key: RSAKey) -> None:
        """Start with no user."""
        self.key = key
        self.email = SEED
        self.verified = True
        self.nonce: str | None = None
        self.down = False
        self.token_requests: list[dict[str, list[str]]] = []
        self.transport = httpx2.MockTransport(self.handle)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        """Answer a call to Google."""
        if self.down:
            raise httpx2.ConnectError("unreachable", request=request)
        url = str(request.url)
        if url == GOOGLE_METADATA_URL:
            return httpx2.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": AUTHORIZE,
                    "token_endpoint": TOKEN,
                    "jwks_uri": JWKS,
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if url == JWKS:
            return httpx2.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if url == TOKEN:
            self.token_requests.append(parse_qs(request.content.decode()))
            now = int(time.time())
            claims = {
                "iss": ISSUER,
                "aud": AUTH_ON["OIDC_CLIENT_ID"],
                "sub": "1234",
                "email": self.email,
                "email_verified": self.verified,
                "nonce": self.nonce,
                "iat": now,
                "exp": now + 3600,
            }
            id_token = jwt.encode({"alg": "RS256", "kid": "test"}, claims, self.key)
            body = {"access_token": "at", "token_type": "Bearer", "expires_in": 3600, "id_token": id_token}
            return httpx2.Response(200, json=body)
        return httpx2.Response(404)


@pytest.fixture
def google(signing_key: RSAKey) -> FakeGoogle:
    """Return a fake Google."""
    return FakeGoogle(signing_key)


def auth_app(google: FakeGoogle, **overrides: object) -> FastAPI:
    """Return an app with authentication on, signing in with the fake Google."""
    return make_app(oidc_transport=google.transport, **(AUTH_ON | overrides))


def run_job(client: TestClient) -> None:
    """Run a load job that serves an ontology at `/onto`, with a class."""
    app: FastAPI = client.app  # type: ignore[assignment]
    stage = derived_stage(
        [("http://example.org/x", EntityType.CLASS)], "http://example.org/onto", title="Onto", edges=[]
    )
    app.state.jobs.register(Stage.PRE_CACHING, stage)
    client.portal.call(app.state.jobs.start)
    client.portal.call(app.state.jobs.wait)


@pytest.fixture
def client(data_dir: Path, google: FakeGoogle) -> Iterator[TestClient]:
    """Yield a client of an app with authentication on, with an ontology loaded; it doesn't follow redirects."""
    with TestClient(auth_app(google), follow_redirects=False) as test_client:
        run_job(test_client)
        yield test_client


def start_sign_in(client: TestClient, google: FakeGoogle, **params: str) -> dict[str, str]:
    """Start signing in, returning the authorization request's parameters, and give its nonce to Google."""
    response = client.get(SIGN_IN, params=params)
    assert response.status_code == 302
    location = urlsplit(response.headers["Location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == AUTHORIZE
    query = {name: values[0] for name, values in parse_qs(location.query).items()}
    google.nonce = query["nonce"]
    return query


def sign_in(client: TestClient, google: FakeGoogle, email: str = SEED, **params: str) -> httpx2.Response:
    """Sign in as an email address, returning the callback's response."""
    google.email = email
    query = start_sign_in(client, google, **params)
    return client.get(CALLBACK, params={"code": "the-code", "state": query["state"]})


def add_admin(data_dir: Path, email: str) -> None:
    """Add an admin to the `admin` table, as P-23's page will."""
    with sqlite3.connect(data_dir / STATE_DB) as connection:
        connection.execute("INSERT INTO admin (email, added_at) VALUES (?, '2026-11-24T00:00:00+00:00')", (email,))


@pytest.mark.parametrize(
    ("path", "next_path"),
    [
        (UPLOAD, UPLOAD),
        (f"{STATUS}?a=1&b=%2F", f"{STATUS}?a=1&b=%2F"),
        ("/_browl/admin/docs", "/_browl/admin/docs"),
        ("/_browl/admin/openapi.json", "/_browl/admin/openapi.json"),
    ],
)
def test_admin_get_needs_sign_in(client: TestClient, path: str, next_path: str) -> None:
    """Check that an admin page's GET, from someone not signed in, is sent to sign in and back (A-1)."""
    response = client.get(path)
    assert response.status_code == 303
    location = urlsplit(response.headers["Location"])
    assert location.path == SIGN_IN
    assert parse_qs(location.query) == {"next": [next_path]}
    assert response.headers["Cache-Control"] == "no-store"


def test_admin_post_and_poll_need_sign_in(client: TestClient) -> None:
    """Check that a POST from someone not signed in gets 401, and an htmx poll gets `HX-Refresh` to sign in."""
    response = client.post(UPLOAD, content=b"")
    assert response.status_code == 401
    assert "Sign in required" in response.text
    poll = client.get(STATUS, headers={"HX-Request": "true"})
    assert poll.status_code == 401
    assert poll.headers["HX-Refresh"] == "true"


def test_public_pages_need_no_sign_in(client: TestClient) -> None:
    """Check that only admin routes need sign-in: the site's other pages and routes are served, with "Sign in" (A-1)."""
    for path in [*PUBLIC_PAGES, "/_browl/health"]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert path == "/_browl/health" or f'href="{SIGN_IN}"' in response.text


@pytest.mark.parametrize(("email", "admin"), [(SEED, True), ("listed@example.org", True), (VISITOR, False)])
def test_public_pages_signed_in(
    client: TestClient, google: FakeGoogle, data_dir: Path, email: str, admin: bool
) -> None:
    """Check that a signed-in user's public pages have "Sign out", and an admin's the admin link (A-1)."""
    add_admin(data_dir, "listed@example.org")
    sign_in(client, google, email)
    for path in PUBLIC_PAGES:
        page = client.get(path)
        assert page.status_code == 200, path
        assert f'action="{SIGN_OUT}"' in page.text
        assert f'href="{SIGN_IN}"' not in page.text
        assert (ADMIN_LINK in page.text) is admin, path


def test_sign_in_seed_admin(client: TestClient, google: FakeGoogle) -> None:
    """Check that a seed admin signs in, whatever their address's case, returns to the page asked for, and is in."""
    query = start_sign_in(client, google, next=STATUS)
    assert query["redirect_uri"] == "http://example.org/_browl/admin/oidc-callback"
    assert query["scope"] == "openid email"
    assert query["prompt"] == "select_account"
    google.email = "Seed@Example.ORG"
    response = client.get(CALLBACK, params={"code": "the-code", "state": query["state"]})
    assert response.status_code == 303
    assert response.headers["Location"] == STATUS
    assert google.token_requests[0]["code"] == ["the-code"]
    page = client.get(UPLOAD)
    assert page.status_code == 200
    assert ADMIN_LINK in page.text
    assert f'action="{SIGN_OUT}"' in page.text
    assert f'href="{SIGN_IN}"' not in page.text


def test_sign_in_table_admin(client: TestClient, google: FakeGoogle, data_dir: Path) -> None:
    """Check that an admin in the `admin` table is let in, and refused as soon as they're removed."""
    add_admin(data_dir, "listed@example.org")
    assert sign_in(client, google, "listed@example.org").headers["Location"] == UPLOAD
    assert client.get(UPLOAD).status_code == 200
    with sqlite3.connect(data_dir / STATE_DB) as connection:
        connection.execute("DELETE FROM admin")
    assert client.get(UPLOAD).status_code == 403


def test_not_admin(client: TestClient, google: FakeGoogle) -> None:
    """Check that a signed-in user who isn't an admin gets 403 on admin routes, and no admin link elsewhere.

    They're offered sign-in with another account, which goes to Google, not back to the refusal. Their htmx poll, e.g.
    after being removed as an admin, gets `HX-Refresh`, so it doesn't poll on (review C6).
    """
    sign_in(client, google, VISITOR)
    for response in [client.get(UPLOAD), client.post(UPLOAD, content=b""), client.get("/_browl/admin/docs")]:
        assert response.status_code == 403
    page = client.get(UPLOAD)
    assert VISITOR in page.text
    assert ADMIN_LINK not in page.text
    assert f'action="{SIGN_OUT}"' in page.text
    assert f'<a href="{SIGN_IN}">Sign in</a> with another account' in page.text
    start_sign_in(client, google)  # 302 to Google
    poll = client.get(STATUS, headers={"HX-Request": "true"})
    assert poll.status_code == 403
    assert poll.headers["HX-Refresh"] == "true"
    assert poll.headers["Cache-Control"] == "no-store"


def test_unverified_email_refused(client: TestClient, google: FakeGoogle) -> None:
    """Check that an ID token whose email isn't verified doesn't sign anyone in, even a seed admin's address."""
    google.verified = False
    response = sign_in(client, google)
    assert response.status_code == 400
    assert "verified email" in response.text
    assert client.get(UPLOAD).status_code == 303


@pytest.mark.parametrize(
    "params", [{"code": "c", "state": "forged"}, {"error": "access_denied"}, {}], ids=["state", "denied", "empty"]
)
def test_callback_refused(client: TestClient, google: FakeGoogle, params: dict[str, str]) -> None:
    """Check that a callback with a state the sign-in didn't give, or Google's error, signs no one in."""
    start_sign_in(client, google)
    response = client.get(CALLBACK, params=params)
    assert response.status_code == 400
    assert "Sign-in failed" in response.text
    assert client.get(UPLOAD).status_code == 303


def test_callback_refused_logs_safely(client: TestClient, google: FakeGoogle, caplog: pytest.LogCaptureFixture) -> None:
    """Check that Google's error text, which anyone can send, is logged on one line, so it can't forge a log line."""
    start_sign_in(client, google)
    with caplog.at_level(logging.WARNING, logger="browl.auth"):
        response = client.get(CALLBACK, params={"error": "x\nFAKE", "error_description": "a\nFAKE: Signed in: x"})
    assert response.status_code == 400
    assert [record.getMessage().count("\n") for record in caplog.records] == [0]
    assert "FAKE" in caplog.records[0].getMessage()


def test_callback_when_signed_in(client: TestClient, google: FakeGoogle) -> None:
    """Check that an admin back at the callback from an older sign-in, e.g. a second tab, goes to where it leads.

    Authlib keeps only the latest sign-in's state, so the older one's would be refused (review C9).
    """
    first = start_sign_in(client, google, next=STATUS)
    second = start_sign_in(client, google, next=STATUS)
    assert client.get(CALLBACK, params={"code": "c", "state": second["state"]}).headers["Location"] == STATUS
    response = client.get(CALLBACK, params={"code": "c", "state": first["state"]})
    assert response.status_code == 303
    assert response.headers["Location"] == UPLOAD
    assert len(google.token_requests) == 1


def test_token_exchanges_limited(client: TestClient, google: FakeGoogle) -> None:
    """Check that past the app-wide limit, the callback answers 429 without calling Google (review C11)."""
    client.app.state.exchange_limit = ExchangeLimit(per_minute=1)  # type: ignore[attr-defined]
    assert sign_in(client, google, VISITOR).status_code == 303
    response = sign_in(client, google, VISITOR)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert "Too many sign-ins" in response.text
    assert len(google.token_requests) == 1
    assert client.get(CALLBACK, params={"error": "access_denied"}).status_code == 400  # Google isn't called


def test_exchange_limit_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the limit counts exchanges in the last 60 seconds only."""
    now = [1000.0]
    monkeypatch.setattr(browl.auth.time, "monotonic", lambda: now[0])
    limit = ExchangeLimit(per_minute=2)
    assert [limit.allow(), limit.allow(), limit.allow()] == [True, True, False]
    now[0] += 59.9
    assert not limit.allow()
    now[0] += 0.1
    assert [limit.allow(), limit.allow(), limit.allow()] == [True, True, False]


def test_wrong_nonce_refused(client: TestClient, google: FakeGoogle) -> None:
    """Check that an ID token without the sign-in's nonce, e.g. one replayed from another sign-in, is refused."""
    query = start_sign_in(client, google)
    google.nonce = "another"
    assert client.get(CALLBACK, params={"code": "c", "state": query["state"]}).status_code == 400
    assert client.get(UPLOAD).status_code == 303


def test_google_unreachable(client: TestClient, google: FakeGoogle) -> None:
    """Check that sign-in fails with 502 when Google can't be reached, at its start or its end."""
    google.down = True
    response = client.get(SIGN_IN)
    assert response.status_code == 502
    assert "reach Google" in response.text
    google.down = False
    query = start_sign_in(client, google)
    google.down = True
    assert client.get(CALLBACK, params={"code": "c", "state": query["state"]}).status_code == 502


@pytest.mark.parametrize("next_path", ["//evil.example/", "https://evil.example/", "/\\evil.example", "upload"])
def test_sign_in_only_returns_within_app(client: TestClient, google: FakeGoogle, next_path: str) -> None:
    """Check that sign-in returns only to a path of this app, else to the upload page (no open redirect)."""
    assert sign_in(client, google, next=next_path).headers["Location"] == UPLOAD


def test_signed_in_admin_skips_google(client: TestClient, google: FakeGoogle) -> None:
    """Check that an admin already signed in goes straight to the page sign-in would return to."""
    sign_in(client, google)
    response = client.get(SIGN_IN, params={"next": STATUS})
    assert response.status_code == 303
    assert response.headers["Location"] == STATUS


def test_sign_out(client: TestClient, google: FakeGoogle) -> None:
    """Check that signing out ends the session, and goes to the site's root; another site can't sign you out."""
    sign_in(client, google)
    assert client.post(SIGN_OUT, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get(UPLOAD).status_code == 200
    response = client.post(SIGN_OUT, headers={"Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 303
    assert response.headers["Location"] == "/"
    assert client.get(UPLOAD).status_code == 303


@pytest.mark.parametrize("email", [SEED, VISITOR])
def test_sign_out_everywhere(client: TestClient, google: FakeGoogle, email: str) -> None:
    """Check that signing out ends the account's other sessions, e.g. a copied cookie, and later sign-ins work (C1)."""
    sign_in(client, google, email)
    copied = client.cookies[SESSION_COOKIE]
    client.post(SIGN_OUT)
    client.cookies.set(SESSION_COOKIE, copied)
    response = client.get("/onto")
    assert f'href="{SIGN_IN}"' in response.text
    assert f"{SESSION_COOKIE}=null" in response.headers["Set-Cookie"]  # cleared
    assert client.get(UPLOAD).status_code == 303
    sign_in(client, google, email)
    assert f'action="{SIGN_OUT}"' in client.get("/onto").text


def test_session_without_sign_in_time(client: TestClient, data_dir: Path) -> None:
    """Check that a session without its sign-in time, which a sign-out can't be checked against, is cleared (C1)."""
    client.cookies.set(SESSION_COOKIE, session_cookie(data_dir, SEED, signed_in_at=False))
    response = client.get(UPLOAD)
    assert response.status_code == 303
    assert f"{SESSION_COOKIE}=null" in response.headers["Set-Cookie"]
    client.cookies.set(SESSION_COOKIE, session_cookie(data_dir, SEED))
    assert client.get(UPLOAD).status_code == 200


def test_admins_unreadable(
    client: TestClient, google: FakeGoogle, data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Check that if the admins can't be read, pages are still served, and the user counts as not an admin (S2)."""
    sign_in(client, google)
    with sqlite3.connect(data_dir / STATE_DB) as connection:
        connection.execute("DROP TABLE admin")
    with caplog.at_level(logging.ERROR, logger="browl.auth"):
        page = client.get("/onto")
        assert page.status_code == 200
        assert ADMIN_LINK not in page.text
        assert f'action="{SIGN_OUT}"' in page.text
        assert client.get(UPLOAD).status_code == 403
    assert "Couldn't read the admins" in caplog.text


def test_admin_store_missing_file(tmp_path: Path) -> None:
    """Check that the store doesn't create a missing database, which would hide that it's gone (S2)."""
    store = AdminStore(tmp_path / STATE_DB)
    with pytest.raises(sqlite3.OperationalError):
        store.account(SEED)
    with pytest.raises(sqlite3.OperationalError):
        store.sign_out(SEED, datetime.now(UTC))
    assert not (tmp_path / STATE_DB).exists()


def test_sign_out_unrecorded(client: TestClient, google: FakeGoogle, data_dir: Path) -> None:
    """Check that if the sign-out can't be stored, this browser is still signed out."""
    sign_in(client, google)
    with sqlite3.connect(data_dir / STATE_DB) as connection:
        connection.execute("DROP TABLE sign_out")
    assert client.post(SIGN_OUT).status_code == 303
    assert client.get(UPLOAD).status_code == 303


def test_signed_out_bar(client: TestClient) -> None:
    """Check that someone not signed in sees the sign-in link, and no sign-out or admin link."""
    page = client.get("/_browl/search").text
    assert f'href="{SIGN_IN}"' in page
    assert "Sign out" not in page
    assert ADMIN_LINK not in page


@pytest.mark.parametrize(("base_url", "secure"), [("http://example.org/", False), ("https://example.org/", True)])
def test_session_cookie(data_dir: Path, google: FakeGoogle, base_url: str, secure: bool) -> None:
    """Check the session cookie's flags: HttpOnly, SameSite=Lax, and Secure when the site is served over HTTPS.

    Lax, so the cookie comes back with Google's redirect to the callback.
    """
    with TestClient(auth_app(google, BASE_URL=base_url), follow_redirects=False) as client:
        cookie = client.get(SIGN_IN).headers["Set-Cookie"].lower()
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert ("secure" in cookie.split("; ")) is secure


def test_sign_in_survives_restart(data_dir: Path, google: FakeGoogle) -> None:
    """Check that the session key is kept in `DATA_DIR`, so a new app accepts the old one's sessions."""
    with TestClient(auth_app(google), follow_redirects=False) as client:
        sign_in(client, google)
        cookies = dict(client.cookies)
    with TestClient(auth_app(google), follow_redirects=False, cookies=cookies) as client:
        assert client.get(UPLOAD).status_code == 200


def test_session_key(tmp_path: Path) -> None:
    """Check that the session key is made once, readable only by its owner, then reused."""
    key = session_key(tmp_path / "data")
    path = tmp_path / "data" / SESSION_KEY_FILE
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(key) >= 32
    assert session_key(tmp_path / "data") == key
    assert [file.name for file in path.parent.iterdir()] == [SESSION_KEY_FILE]


@pytest.mark.parametrize("stored", ["", " \n", "x" * (SESSION_KEY_CHARS - 1)])
def test_session_key_too_short(tmp_path: Path, stored: str) -> None:
    """Check that a stored key that's empty or too short is refused, as anyone could sign a session with it (S1)."""
    (tmp_path / SESSION_KEY_FILE).write_text(stored)
    with pytest.raises(RuntimeError, match="delete the file"):
        session_key(tmp_path)


def test_session_key_synced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a new key is synced to disk before it's linked into place, so a crash can't leave it empty (S1)."""
    calls = []
    real_link = os.link
    monkeypatch.setattr(browl.auth.os, "fsync", lambda fd: calls.append("fsync"))
    monkeypatch.setattr(browl.auth.os, "link", lambda *args: (calls.append("link"), real_link(*args)))
    session_key(tmp_path)
    assert calls == ["fsync", "link"]


def test_callback_url_from_base_url(data_dir: Path) -> None:
    """Check that the callback URL comes from `BASE_URL`, its path included, and the route's path."""
    app = make_app(**AUTH_ON, BASE_URL="https://Example.org/onto/", RESERVED_PREFIX="_site")
    assert callback_url(app) == "https://example.org/onto/_site/admin/oidc-callback"


def test_every_admin_route_protected(client: TestClient, google: FakeGoogle) -> None:
    """Check that every route under the admin prefix, but signing in and out, needs an admin (S8)."""
    # Every module's routers, as FastAPI doesn't list an app's included routes; each is included under the prefix.
    routers = [
        router
        for info in pkgutil.walk_packages(browl.__path__, "browl.")
        for router in vars(importlib.import_module(info.name)).values()
        if isinstance(router, APIRouter) and router is not auth.router
    ]
    admin = sorted(
        {
            (method, f"/_browl{route.path}")
            for router in routers
            for route in router.routes
            if isinstance(route, APIRoute) and route.path.startswith("/admin/")
            for method in route.methods
        }
    )
    assert len(admin) >= 6  # upload (GET and POST), status, docs, its OAuth2 redirect, openapi.json
    assert ("GET", "/_browl/admin/docs/oauth2-redirect") in admin
    for method, path in admin:
        assert client.request(method, path).status_code == (303 if method == "GET" else 401), (method, path)
    sign_in(client, google, VISITOR)
    for method, path in admin:
        assert client.request(method, path).status_code == 403, (method, path)


def test_under_root_path(data_dir: Path, google: FakeGoogle) -> None:
    """Check sign-in's links and redirects under a root path, with another reserved prefix (S9)."""
    app = auth_app(google, BASE_URL="http://example.org/root/", RESERVED_PREFIX="_site")
    with TestClient(app, root_path="/root", follow_redirects=False) as client:
        response = client.get("/root/_site/admin/upload")
        location = urlsplit(response.headers["Location"])
        assert location.path == "/root/_site/admin/sign-in"
        assert parse_qs(location.query) == {"next": ["/_site/admin/upload"]}
        response = client.get("/root/_site/admin/sign-in", params={"next": "/_site/admin/status"})
        query = {name: values[0] for name, values in parse_qs(urlsplit(response.headers["Location"]).query).items()}
        assert query["redirect_uri"] == "http://example.org/root/_site/admin/oidc-callback"
        google.nonce = query["nonce"]
        response = client.get("/root/_site/admin/oidc-callback", params={"code": "c", "state": query["state"]})
        assert response.headers["Location"] == "/root/_site/admin/status"
        page = client.get("/root/_site/admin/upload").text
        assert 'action="/root/_site/admin/sign-out"' in page
        assert '<a href="/root/_site/admin/upload">Admin</a>' in page
        assert client.post("/root/_site/admin/sign-out").headers["Location"] == "/root/"
        assert 'href="/root/_site/admin/sign-in"' in client.get("/root/_site/search").text


def test_auth_disabled(data_dir: Path) -> None:
    """Check that with `AUTH_DISABLED` admin routes are open to everyone, and sign-in goes straight to them (O-7)."""
    with TestClient(make_app(), follow_redirects=False) as client:
        assert client.get(UPLOAD).status_code == 200
        assert client.get(STATUS).status_code == 200
        assert client.get(SIGN_IN, params={"next": STATUS}).headers["Location"] == STATUS
        assert client.get(CALLBACK).headers["Location"] == UPLOAD
        page = client.get("/_browl/search").text
        assert 'class="admin-link"' in page
        assert "Sign in" not in page
        assert "Sign out" not in page


def test_safe_next_examples() -> None:
    """Check `safe_next` beyond its doctest: empty and relative values are refused."""
    assert [safe_next(value) for value in [None, "", "x", "/a?b=c"]] == [None, None, None, "/a?b=c"]
