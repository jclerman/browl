"""Tests for the admins page: listing, adding and removing admins (P-23; A-7; DESIGN.md §2.11)."""

import html
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from browl.admins import Admin, AdminStore, canonical_email, parse_email
from browl.admins_page import MAX_FORM_BYTES
from browl.auth import SESSION_COOKIE
from browl.jobs import STATE_DB, JobStore
from tests.helpers import AUTH_ON, make_app, session_cookie

ADMINS = "/_browl/admin/admins"
REMOVE = "/_browl/admin/admins/remove"
UPLOAD = "/_browl/admin/upload"
SEED = "seed@example.org"
SAME_SITE = {"Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def client(data_dir: Path) -> Iterator[TestClient]:
    """Yield a client of an app with authentication on, signed in as the seed admin; it doesn't follow redirects."""
    with TestClient(make_app(**AUTH_ON), follow_redirects=False) as test_client:
        sign_in_as(test_client, data_dir, SEED)
        yield test_client


def sign_in_as(client: TestClient, data_dir: Path, email: str) -> None:
    """Give the client a session signed in as an email address."""
    client.cookies.set(SESSION_COOKIE, session_cookie(data_dir, email))


def store(client: TestClient) -> AdminStore:
    """Return the app's admin store."""
    app: FastAPI = client.app  # type: ignore[assignment]
    result: AdminStore = app.state.admins
    return result


def add(client: TestClient, email: str) -> None:
    """Add an admin through the page, checking it's sent back to the list."""
    response = client.post(ADMINS, data={"email": email}, headers=SAME_SITE)
    assert response.status_code == 303, response.text
    assert response.headers["Location"] == ADMINS


def test_lists_seed_admins(client: TestClient) -> None:
    """Check that the page lists the seed admins, marked as such, with no button to remove them (A-7)."""
    page = client.get(ADMINS)
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert f'{SEED} <span class="note">(you)</span>' in page.text
    assert "Seed admin" in page.text
    assert f'aria-label="Remove {SEED}"' not in page.text


def test_add_and_remove(client: TestClient, data_dir: Path) -> None:
    """Check that an admin added is listed and stored, with who added them, and can sign in; then is removed (A-7)."""
    add(client, " New.Admin@Example.org ")
    [admin] = store(client).admins()
    assert (admin.email, admin.added_by) == ("new.admin@example.org", SEED)
    page = client.get(ADMINS).text
    assert 'aria-label="Remove new.admin@example.org"' in page
    assert f"by {SEED}" in page
    assert 'data-confirm="Remove new.admin@example.org as an admin?"' in page
    sign_in_as(client, data_dir, "new.admin@example.org")
    assert client.get(UPLOAD).status_code == 200
    sign_in_as(client, data_dir, SEED)
    response = client.post(REMOVE, data={"email": "new.admin@example.org"}, headers=SAME_SITE)
    assert (response.status_code, response.headers["Location"]) == (303, ADMINS)
    assert store(client).admins() == []
    sign_in_as(client, data_dir, "new.admin@example.org")
    assert client.get(UPLOAD).status_code == 403


def test_survives_restart(client: TestClient, data_dir: Path) -> None:
    """Check that an admin added is still one after a restart: they're in the database on the data volume (O-9)."""
    add(client, "kept@example.org")
    with TestClient(make_app(**AUTH_ON), follow_redirects=False) as restarted:
        sign_in_as(restarted, data_dir, "kept@example.org")
        assert restarted.get(UPLOAD).status_code == 200
        assert "kept@example.org" in restarted.get(ADMINS).text


@pytest.mark.parametrize(
    ("entered", "status", "message"),
    [
        ("not-an-address", 422, "“not-an-address” isn't an email address."),
        ("a@localhost", 422, "isn't an email address."),
        ("a b@example.org", 422, "isn't an email address."),
        ("", 422, "Enter an email address."),
        (f"{'x' * 250}@example.org", 422, "isn't an email address."),
        ("\x1b[2Jx@example.org", 422, "isn't an email address."),
        ("Seed@Example.org", 409, "seed@example.org is a seed admin already."),
    ],
)
def test_add_refused(client: TestClient, entered: str, status: int, message: str) -> None:
    """Check that an invalid address, or a seed admin's, is refused, the page keeping what was entered (A-7)."""
    response = client.post(ADMINS, data={"email": entered}, headers=SAME_SITE)
    assert response.status_code == status
    assert message in html.unescape(response.text)
    assert 'role="alert"' in response.text
    assert f'value="{html.escape(entered)}"' in response.text
    assert 'aria-describedby="admins-error"' in response.text
    assert store(client).admins() == []


def test_add_duplicate(client: TestClient) -> None:
    """Check that an admin already added is refused, also as a Gmail address with other dots or case (A-7)."""
    add(client, "j.doe@gmail.com")
    for entered in ["j.doe@gmail.com", "JDoe@Gmail.com", "j.d.o.e@gmail.com"]:
        response = client.post(ADMINS, data={"email": entered}, headers=SAME_SITE)
        assert response.status_code == 409
        assert f"{entered.lower()} is an admin already." in response.text
    assert [admin.email for admin in store(client).admins()] == ["j.doe@gmail.com"]


def test_gmail_dots_shown_but_ignored(client: TestClient, data_dir: Path) -> None:
    """Check that a Gmail admin is shown with the dots they were added with, and is one however they sign in."""
    add(client, "J.Doe@Gmail.com")
    page = client.get(ADMINS).text
    assert 'aria-label="Remove j.doe@gmail.com"' in page
    for signed_in in ["jdoe@gmail.com", "j.doe@gmail.com", "j.d.o.e@gmail.com"]:
        sign_in_as(client, data_dir, signed_in)
        assert client.get(UPLOAD).status_code == 200, signed_in
        assert 'j.doe@gmail.com <span class="note">(you)</span>' in client.get(ADMINS).text
    response = client.post(REMOVE, data={"email": "jdoe@gmail.com"}, headers=SAME_SITE)
    assert (response.status_code, store(client).admins()) == (303, [])


def test_gmail_seed_dots_ignored(data_dir: Path) -> None:
    """Check that a seed admin's Gmail address is shown as configured, and matches however they sign in."""
    with TestClient(make_app(**(AUTH_ON | {"SEED_ADMINS": ["J.Doe@gmail.com"]})), follow_redirects=False) as client:
        sign_in_as(client, data_dir, "jdoe@gmail.com")
        page = client.get(ADMINS)
        assert page.status_code == 200
        assert 'j.doe@gmail.com <span class="note">(you)</span>' in page.text
        response = client.post(ADMINS, data={"email": "jdo.e@gmail.com"}, headers=SAME_SITE)
        assert (response.status_code, store(client).admins()) == (409, [])
        response = client.post(REMOVE, data={"email": "jdoe@gmail.com"}, headers=SAME_SITE)
        assert response.status_code == 409


def test_remove_seed_refused(client: TestClient) -> None:
    """Check that a seed admin can't be removed, even by a request the page doesn't offer (A-7)."""
    response = client.post(REMOVE, data={"email": SEED}, headers=SAME_SITE)
    assert response.status_code == 409
    assert "can't be removed here" in html.unescape(response.text)
    assert client.get(UPLOAD).status_code == 200


def test_remove_absent(client: TestClient) -> None:
    """Check that removing an admin who's gone already, e.g. removed by another admin meanwhile, is no error."""
    response = client.post(REMOVE, data={"email": "gone@example.org"}, headers=SAME_SITE)
    assert (response.status_code, response.headers["Location"]) == (303, ADMINS)


def test_remove_yourself(client: TestClient, data_dir: Path) -> None:
    """Check that an admin removing themselves is warned, and refused the admin pages from then on."""
    add(client, "self@example.org")
    sign_in_as(client, data_dir, "self@example.org")
    page = client.get(ADMINS).text
    assert "Remove yourself as an admin? You'll lose access to the admin pages at once." in html.unescape(page)
    client.post(REMOVE, data={"email": "self@example.org"}, headers=SAME_SITE)
    assert client.get(ADMINS).status_code == 403


def test_seed_also_in_table(client: TestClient, data_dir: Path) -> None:
    """Check that a seed admin also in the `admin` table, e.g. added before they were seeded, is listed once."""
    store(client).add(SEED, None, datetime.now(UTC))
    page = client.get(ADMINS).text
    assert page.count(f"<td>{SEED}") == 1
    assert f'aria-label="Remove {SEED}"' not in page


def test_cross_site_refused(client: TestClient) -> None:
    """Check that another site's page can't add or remove admins (CSRF)."""
    cross = {"Sec-Fetch-Site": "cross-site"}
    assert client.post(ADMINS, data={"email": "x@example.org"}, headers=cross).status_code == 403
    add(client, "y@example.org")
    assert client.post(REMOVE, data={"email": "y@example.org"}, headers=cross).status_code == 403
    assert [admin.email for admin in store(client).admins()] == ["y@example.org"]


def test_write_fails(client: TestClient, data_dir: Path) -> None:
    """Check that if the database can't be written, the page says so rather than failing."""
    add(client, "y@example.org")
    with sqlite3.connect(data_dir / STATE_DB) as connection:
        connection.execute("CREATE TRIGGER no_insert BEFORE INSERT ON admin BEGIN SELECT RAISE(ABORT, 'no'); END")
        connection.execute("CREATE TRIGGER no_delete BEFORE DELETE ON admin BEGIN SELECT RAISE(ABORT, 'no'); END")
    response = client.post(ADMINS, data={"email": "x@example.org"}, headers=SAME_SITE)
    assert response.status_code == 500
    assert "Couldn't add x@example.org" in html.unescape(response.text)
    response = client.post(REMOVE, data={"email": "y@example.org"}, headers=SAME_SITE)
    assert response.status_code == 500
    assert "Couldn't remove y@example.org" in html.unescape(response.text)


def test_list_unreadable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that if the admins added here can't be read, the page still lists the seed admins, and says so (C2).

    It does so also after a write fails.
    """

    def fail() -> list[Admin]:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store(client), "admins", fail)
    page = client.get(ADMINS)
    assert page.status_code == 500
    assert "The admins added here couldn&#39;t be read." in page.text
    assert f"<td>{SEED}" in page.text
    monkeypatch.setattr(store(client), "remove", lambda email: fail())
    response = client.post(REMOVE, data={"email": "y@example.org"}, headers=SAME_SITE)
    assert response.status_code == 500
    text = html.unescape(response.text)
    assert "Couldn't remove y@example.org: the database couldn't be written." in text
    assert "The admins added here couldn't be read." in text


def test_list_order(data_dir: Path) -> None:
    """Check that the seed admins come first, as configured, then the admins added here, by address."""
    seeds = ["zed@example.org", "amy@example.org"]
    with TestClient(make_app(**(AUTH_ON | {"SEED_ADMINS": seeds})), follow_redirects=False) as client:
        sign_in_as(client, data_dir, "amy@example.org")
        for email in ["mo@example.org", "bo@example.org"]:
            add(client, email)
        page = client.get(ADMINS).text
        positions = [page.index(f"<td>{email}") for email in [*seeds, "bo@example.org", "mo@example.org"]]
        assert positions == sorted(positions)


def test_not_admin_cant_change(client: TestClient, data_dir: Path) -> None:
    """Check that a signed-in user who isn't an admin can't add or remove admins (review S3)."""
    add(client, "kept@example.org")
    sign_in_as(client, data_dir, "visitor@example.org")
    assert client.post(ADMINS, data={"email": "visitor@example.org"}, headers=SAME_SITE).status_code == 403
    assert client.post(REMOVE, data={"email": "kept@example.org"}, headers=SAME_SITE).status_code == 403
    assert [admin.email for admin in store(client).admins()] == ["kept@example.org"]


def test_auth_disabled(data_dir: Path) -> None:
    """Check that with `AUTH_DISABLED` the page works, says the list has no effect yet, and records no adder."""
    with TestClient(make_app(), follow_redirects=False) as client:
        assert "Sign-in is off" in client.get(ADMINS).text
        add(client, "x@example.org")
        assert [(admin.email, admin.added_by) for admin in store(client).admins()] == [("x@example.org", None)]


def test_admin_nav(client: TestClient) -> None:
    """Check that each admin page links to the others, marking itself."""
    for path in [UPLOAD, "/_browl/admin/status", ADMINS]:
        page = client.get(path).text
        assert 'aria-label="Admin"' in page
        assert f'<a href="{path}" aria-current="page">' in page
        assert page.count('aria-current="page"') == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("J.Doe@Gmail.com", "jdoe@gmail.com"),
        ("j.doe+tag@gmail.com", "jdoe+tag@gmail.com"),  # + tags kept
        ("j.doe@googlemail.com", "j.doe@googlemail.com"),  # only gmail.com
        ("j.doe@mail.gmail.com", "j.doe@mail.gmail.com"),
        ("no-at-sign", "no-at-sign"),
    ],
)
def test_canonical_email(text: str, expected: str) -> None:
    """Check `canonical_email` beyond its doctest."""
    assert canonical_email(text) == expected


@pytest.mark.parametrize(
    "text", ["...@gmail.com", "@example.org", "a@b", "a@b.", "a@@b.org", "a@b .org", "a\x00@b.org", "a\x85@b.org"]
)
def test_parse_email_refused(text: str) -> None:
    """Check that `parse_email` refuses what isn't an address, also once a Gmail address's dots are dropped.

    Control characters, which would reach the logs, are refused too (review C1).
    """
    with pytest.raises(ValueError):
        parse_email(text)


def test_store_admins(tmp_path: Path) -> None:
    """Check the store's admin list: ordered by address, with when and by whom each was added."""
    path = tmp_path / STATE_DB
    JobStore(path)
    admins = AdminStore(path)
    at = datetime(2026, 11, 25, tzinfo=UTC)
    assert admins.add("b@example.org", "a@example.org", at)
    assert admins.add("a@example.org", None, at)
    assert not admins.add("a@example.org", "x@example.org", at)
    assert admins.admins() == [Admin("a@example.org", at, None), Admin("b@example.org", at, "a@example.org")]
    assert admins.remove("a@example.org")
    assert not admins.remove("a@example.org")


@pytest.mark.parametrize("path", [ADMINS, REMOVE])
def test_body_capped_before_sign_in(data_dir: Path, path: str) -> None:
    """Check that the forms' bodies are capped, since FastAPI parses them before the admin and CSRF checks (review M1).

    Someone not signed in, from another site, gets 413 for a body over the cap, with or without a Content-Length, and
    400 for a file part, rather than the server reading it all and then refusing them.
    """
    big = b"email=" + b"x" * MAX_FORM_BYTES
    form = {"Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "cross-site"}
    with TestClient(make_app(**AUTH_ON), follow_redirects=False) as client:
        assert client.post(path, content=big, headers=form).status_code == 413
        assert client.post(path, content=iter([big]), headers=form).status_code == 413
        assert client.post(path, files={"f": ("f", b"x")}).status_code == 400
        assert client.post(path, data={"email": "a@example.org"}).status_code == 401
