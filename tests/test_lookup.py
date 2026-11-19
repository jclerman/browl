"""Tests for the routing of entity URLs: home, entity, disambiguation and hash pages, the fallback route, and 404."""

import urllib.parse
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from browl.derived import EntityType
from browl.jobs import Stage
from browl.lookup import request_path
from tests.helpers import derived_stage, make_app

pytestmark = pytest.mark.usefixtures("data_dir")

ONTOLOGY = "http://example.org/onto"  # native under BASE_URL http://example.org/, so at /onto
ENTITIES = [
    (f"{ONTOLOGY}#A", EntityType.CLASS),
    ("http://other.org/x", EntityType.CLASS),
    ("https://other.org/x", EntityType.CLASS),
    ("http://other.org/y", EntityType.CLASS),
    ("http://other.org/y", EntityType.OBJECT_PROPERTY),
    ("http://other.org/vocab#C", EntityType.CLASS),
    ("http://other.org/vocab#E", EntityType.CLASS),
    ("https://other.org/vocab#E", EntityType.CLASS),
    ("http://other.org/z", EntityType.CLASS),
    ("http://other.org/z#D", EntityType.CLASS),
    ("http://other.org/café", EntityType.CLASS),
    ("http://other.org/a%2Fb", EntityType.CLASS),
    ("urn:example:fallback", EntityType.DATA_PROPERTY),
    ("http://_browl/x", EntityType.CLASS),
]
"""Proxied entities, under http://other.org/; one with a fallback URN; one whose URL would be under the prefix."""

HASH_SCRIPT = '<script src="/_browl/static/hash.js"'


def run(client: TestClient, app: FastAPI) -> None:
    """Run a load job whose derived database holds the ontology and `ENTITIES`."""
    app.state.jobs.register(Stage.PRE_CACHING, derived_stage(ENTITIES, ONTOLOGY, title="The <Onto>"))
    client.portal.call(app.state.jobs.start)
    client.portal.call(app.state.jobs.wait)


@pytest.fixture
def app() -> FastAPI:
    """Return an app with default settings."""
    return make_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Yield a test client for the app, with the ontology and `ENTITIES` loaded."""
    with TestClient(app) as test_client:
        run(test_client, app)
        yield test_client


def iri_link(iri: str, root: str = "") -> str:
    """Return the fallback route's link for an IRI."""
    return f"{root}/_browl/iri?u={urllib.parse.quote(iri, safe='')}"


def test_home(client: TestClient) -> None:
    """Check that the ontology IRI's path serves the home page, with the hash script, as hash entities share it."""
    response = client.get("/onto")
    assert response.status_code == 200
    assert "<h1>The &lt;Onto&gt;</h1>" in response.text
    assert f'{HASH_SCRIPT} data-lookup="/_browl/hash"' in response.text
    assert 'data-path="/onto" data-prefix-only="false"' in response.text


def test_entity(client: TestClient) -> None:
    """Check that an entity's path serves its page, one page for all its types, without the hash script."""
    response = client.get("/other.org/y")
    assert response.status_code == 200
    assert "<code>http://other.org/y</code>" in response.text
    assert "class, object_property" in response.text
    assert HASH_SCRIPT not in response.text
    assert "noindex" not in response.text


def test_head(client: TestClient) -> None:
    """Check that entity URLs answer HEAD as GET, without a body."""
    response = client.head("/other.org/y")
    assert (response.status_code, response.text) == (200, "")
    assert client.head("/no/such/page").status_code == 404


def test_entity_and_hash_prefix(client: TestClient) -> None:
    """Check that an entity's path that's also a hash prefix serves the entity's page with the hash script."""
    response = client.get("/other.org/z")
    assert response.status_code == 200
    assert "<code>http://other.org/z</code>" in response.text
    assert 'data-path="/other.org/z" data-prefix-only="false"' in response.text


@pytest.mark.parametrize(
    ("path", "iri"), [("/other.org/caf%C3%A9", "http://other.org/café"), ("/other.org/a%2Fb", "http://other.org/a%2Fb")]
)
def test_path_in_uri_form(client: TestClient, path: str, iri: str) -> None:
    """Check that paths are looked up as sent, percent-encoded, not as the router decodes them."""
    response = client.get(path)
    assert response.status_code == 200
    assert f"<code>{iri}</code>" in response.text
    assert client.get("/other.org/a/b").status_code == 404


def test_disambiguation(client: TestClient) -> None:
    """Check that a path the http and https forms of an IRI share serves a page linking each by its IRI (U-15)."""
    response = client.get("/other.org/x")
    assert response.status_code == 200
    assert "<h1>Choose an entity</h1>" in response.text
    for iri in ("http://other.org/x", "https://other.org/x"):
        assert f'href="{iri_link(iri)}"' in response.text
        page = client.get(iri_link(iri), follow_redirects=False)
        assert page.status_code == 200
        assert f"<code>{iri}</code>" in page.text


def test_hash_prefix(client: TestClient) -> None:
    """Check that a path only hash entities have serves the hash page, with status 200 (U-13)."""
    response = client.get("/other.org/vocab")
    assert response.status_code == 200
    assert 'data-path="/other.org/vocab" data-prefix-only="true"' in response.text
    assert "<noscript>" in response.text


@pytest.mark.parametrize(
    ("path", "fragment", "expected"),
    [
        ("/other.org/vocab", "C", "<code>http://other.org/vocab#C</code>"),
        ("/onto", "A", "<code>http://example.org/onto#A</code>"),
        ("/other.org/vocab", "E", "<h1>Choose an entity</h1>"),
    ],
)
def test_hash_route(client: TestClient, path: str, fragment: str, expected: str) -> None:
    """Check that the hash script's route serves the page for a path and fragment."""
    response = client.get("/_browl/hash", params={"path": path, "fragment": fragment, "tab": "usage"})
    assert response.status_code == 200
    assert expected in response.text
    assert HASH_SCRIPT not in response.text
    assert '<meta name="robots" content="noindex">' in response.text


@pytest.mark.parametrize(
    "params",
    [{}, {"path": "/other.org/vocab"}, {"path": "/other.org/vocab", "fragment": "Z"}, {"path": "/x", "fragment": "C"}],
)
def test_hash_route_not_found(client: TestClient, params: dict[str, str]) -> None:
    """Check that the hash route serves "not found" with no fragment (U-14), or one no entity has."""
    response = client.get("/_browl/hash", params=params)
    assert response.status_code == 404
    assert "<h1>Not found</h1>" in response.text


@pytest.mark.parametrize("iri", ["urn:example:fallback", "http://_browl/x"])
def test_fallback(client: TestClient, iri: str) -> None:
    """Check that an entity with a fallback URL is served there."""
    response = client.get(iri_link(iri))
    assert response.status_code == 200
    assert f"<code>{iri}</code>" in response.text


@pytest.mark.parametrize(
    ("iri", "location"),
    [("http://other.org/y", "/other.org/y"), ("http://other.org/vocab#C", "/other.org/vocab#C"), (ONTOLOGY, "/onto")],
)
def test_fallback_route_redirects(client: TestClient, iri: str, location: str) -> None:
    """Check that the fallback route redirects an entity that has a URL of its own there."""
    response = client.get(iri_link(iri), follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"] == location


@pytest.mark.parametrize("path", ["/_browl/iri", iri_link("http://other.org/nothing")])
def test_fallback_route_not_found(client: TestClient, path: str) -> None:
    """Check that the fallback route serves "not found" for an IRI no entity has."""
    assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/no/such/page",
        "/onto/",
        "/other.org/x/",
        "/other.org",
        "/example.org/onto",
        "/x",  # http://_browl/x's proxied URL, under the reserved prefix
        "/_browl/x",
        "/_browl/admin%2F..%2F..%2Fother.org%2Fy",
        "/%5Fbrowl/x",
        "/_browl",
    ],
)
def test_not_found(client: TestClient, path: str) -> None:
    """Check that a path no entity has, or one under the reserved prefix, serves "not found" with status 404 (U-14)."""
    response = client.get(path)
    assert response.status_code == 404
    assert "<h1>Not found</h1>" in response.text


def test_reserved_routes_come_first(client: TestClient) -> None:
    """Check that the service's own routes take precedence over the catch-all route."""
    assert client.get("/_browl/health").json()["status"] == "ok"
    response = client.get("/_browl/static/hash.js")
    assert response.status_code == 200
    assert "hashchange" in response.text


def test_under_root_path(app: FastAPI) -> None:
    """Check that paths are looked up, and links made, within the app's root path, whether or not the path has it."""
    with TestClient(app, root_path="/site") as client:
        run(client, app)
        assert "<code>http://other.org/y</code>" in client.get("/site/other.org/y").text
        assert "<code>http://other.org/café</code>" in client.get("/site/other.org/caf%C3%A9").text
        choose = client.get("/site/other.org/x").text
        assert f'href="{iri_link("http://other.org/x", "/site")}"' in choose
        redirect = client.get(iri_link("http://other.org/y", "/site"), follow_redirects=False)
        assert redirect.headers["Location"] == "/site/other.org/y"
        assert 'data-lookup="/site/_browl/hash"' in client.get("/site/onto").text


@pytest.mark.parametrize("raw_path", [b"/site/other.org/caf%C3%A9", b"/other.org/caf%C3%A9"])
def test_request_path_root_path(raw_path: bytes) -> None:
    """Check that a request's path is taken from its raw path, less the root path whether a proxy stripped it or not."""
    scope = {"type": "http", "path": "/site/other.org/caf\u00e9", "raw_path": raw_path, "root_path": "/site"}
    assert request_path(Request(scope)) == "/other.org/caf%C3%A9"
