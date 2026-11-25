"""Tests for admin sign-in and the admin routes' protection (P-22; A-1, O-6, O-7; DESIGN.md §2.11)."""

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey

from browl.auth import GOOGLE_METADATA_URL, SESSION_COOKIE, SESSION_KEY_FILE, callback_url, safe_next, session_key
from browl.jobs import STATE_DB
from tests.helpers import AUTH_ON, make_app, make_settings

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


@pytest.fixture
def client(data_dir: Path, google: FakeGoogle) -> Iterator[TestClient]:
    """Yield a client of an app with authentication on; it doesn't follow redirects."""
    with TestClient(auth_app(google), follow_redirects=False) as test_client:
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
    """Check that only admin routes need sign-in: the site's other pages and routes don't redirect (A-1)."""
    for path in ["/onto", "/_browl/search", "/_browl/health"]:
        assert client.get(path).status_code in {200, 503}


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
    assert 'class="admin-link"' in page.text
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
    """Check that a signed-in user who isn't an admin gets 403 on admin routes, and no admin link elsewhere."""
    sign_in(client, google, "visitor@example.org")
    for response in [client.get(UPLOAD), client.post(UPLOAD, content=b""), client.get("/_browl/admin/docs")]:
        assert response.status_code == 403
    page = client.get(UPLOAD)
    assert "visitor@example.org" in page.text
    assert 'class="admin-link"' not in page.text
    assert f'action="{SIGN_OUT}"' in page.text


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


def test_signed_out_bar(client: TestClient) -> None:
    """Check that someone not signed in sees the sign-in link, and no sign-out or admin link."""
    page = client.get("/_browl/search").text
    assert f'href="{SIGN_IN}"' in page
    assert "Sign out" not in page
    assert 'class="admin-link"' not in page


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


def test_callback_url_from_base_url(data_dir: Path) -> None:
    """Check that the callback URL comes from `BASE_URL`, its path included, and the reserved prefix."""
    settings = make_settings(**AUTH_ON, BASE_URL="https://Example.org/onto/", RESERVED_PREFIX="_site")
    assert callback_url(settings) == "https://example.org/onto/_site/admin/oidc-callback"


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
