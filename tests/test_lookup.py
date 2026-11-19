"""Tests for the routing of entity URLs: home, entity, disambiguation and hash pages, the fallback route, and 404."""

import urllib.parse
from collections.abc import Iterator, Sequence

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

import browl.lookup
from browl.derived import EntityType, read_meta
from browl.jobs import Stage
from browl.lookup import request_path
from browl.pages import STATIC_DIR
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
    ("http://other.org/a|b", EntityType.CLASS),
    ("http://other.org/vocab#f{g}", EntityType.CLASS),
    ("urn:example:fallback", EntityType.DATA_PROPERTY),
    ("urn:example:p", EntityType.CLASS),
    ("urn:example:p", EntityType.OBJECT_PROPERTY),
    ("http://_browl/x", EntityType.CLASS),
]
"""Proxied entities, under http://other.org/; fallback URNs, one punned; one whose URL would be under the prefix."""

HASH_SCRIPT = '<script src="/_browl/static/hash.js"'


def run(
    client: TestClient,
    app: FastAPI,
    entities: Sequence[tuple[str, EntityType]] = ENTITIES,
    ontology: str | None = ONTOLOGY,
) -> None:
    """Run a load job whose derived database holds the ontology and the entities, by default `ENTITIES`."""
    app.state.jobs.register(Stage.PRE_CACHING, derived_stage(entities, ontology, title="The <Onto>"))
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
    return f"{root}/_browl/iri?iri={urllib.parse.quote(iri, safe='')}"


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


@pytest.mark.parametrize(
    "path", ["/other.org/y", iri_link("urn:example:fallback"), "/_browl/hash?path=/other.org/vocab&fragment=C"]
)
def test_head(client: TestClient, path: str) -> None:
    """Check that entity URLs and the lookup routes answer HEAD as GET, without a body."""
    response = client.head(path)
    assert (response.status_code, response.text) == (200, "")


def test_head_not_found(client: TestClient) -> None:
    """Check that HEAD is 404 for a path no entity has, and 405 for a reserved route that only answers GET."""
    assert client.head("/no/such/page").status_code == 404
    assert client.head("/_browl/health").status_code == 405


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
    assert response.text.count('href="/_browl/iri?') == 2
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


def test_fallback_punned(client: TestClient) -> None:
    """Check that an IRI with several types and a fallback URL is served there as one entity."""
    response = client.get(iri_link("urn:example:p"))
    assert response.status_code == 200
    assert "class, object_property" in response.text


@pytest.mark.parametrize("iri", ["http://other.org/vocab#E", "https://other.org/vocab#E"])
def test_fallback_route_shared_hash_url(client: TestClient, iri: str) -> None:
    """Check that the fallback route serves each hash IRI that shares a URL, rather than redirecting to it."""
    response = client.get(iri_link(iri), follow_redirects=False)
    assert response.status_code == 200
    assert f"<code>{iri}</code>" in response.text
    assert "Choose an entity" not in response.text


@pytest.mark.parametrize(
    ("iri", "location"),
    [
        ("http://other.org/y", "/other.org/y"),
        ("http://other.org/vocab#C", "/other.org/vocab#C"),
        (ONTOLOGY, "/onto"),
        ("http://other.org/a|b", "/other.org/a|b"),
        ("http://other.org/vocab#f{g}", "/other.org/vocab#f{g}"),
    ],
)
def test_fallback_route_redirects(client: TestClient, iri: str, location: str) -> None:
    """Check that the fallback route redirects an entity that has a URL of its own there, as stored, unescaped."""
    response = client.get(iri_link(iri), follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"] == location


def test_unescaped_path(client: TestClient) -> None:
    """Check that a path with a character stored unescaped, as the fallback route redirects to it, is served."""
    response = client.get("/other.org/a|b")
    assert response.status_code == 200
    assert "<code>http://other.org/a|b</code>" in response.text


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
    ],
)
def test_not_found(client: TestClient, path: str) -> None:
    """Check that a path no entity has serves "not found" with status 404 (U-14)."""
    response = client.get(path)
    assert response.status_code == 404
    assert "<h1>Not found</h1>" in response.text


@pytest.mark.parametrize(
    "path", ["/_browl/x", "/%5Fbrowl/x", "/_browl/admin%2F..%2F..%2Fother.org%2Fy", "/_browl", "/_browl/"]
)
def test_reserved_not_found(client: TestClient, path: str) -> None:
    """Check that a path under the reserved prefix no route has is the router's 404, never looked up (O-3)."""
    response = client.get(path)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_reserved_prefix_setting() -> None:
    """Check that the reserved prefix comes from the settings: in 404s, the choose page's links and the hash script."""
    app = make_app(RESERVED_PREFIX="_site")
    with TestClient(app) as client:
        run(client, app)
        for path in ("/_site/x", "/%5Fsite/x"):
            assert client.get(path).json() == {"detail": "Not Found"}
        assert "<code>http://_browl/x</code>" in client.get("/_browl/x").text  # no longer reserved
        response = client.get("/_browl/nothing")
        assert response.status_code == 404
        assert "<h1>Not found</h1>" in response.text
        choose = client.get("/other.org/x").text
        assert f'href="/_site/iri?iri={urllib.parse.quote("http://other.org/x", safe="")}"' in choose
        assert 'data-lookup="/_site/hash"' in client.get("/onto").text


def test_ontology_iri_shared() -> None:
    """Check that an ontology IRI whose URL another IRI shares serves the disambiguation page, not the home page."""
    app = make_app()
    with TestClient(app) as client:
        run(client, app, [("https://example.org/onto", EntityType.CLASS)])
        response = client.get("/onto")
        assert response.status_code == 200
        assert "<h1>Choose an entity</h1>" in response.text
        assert client.get(iri_link(ONTOLOGY)).text.count("<h1>The &lt;Onto&gt;</h1>") == 1


def test_base_url_with_path() -> None:
    """Check that native entities are served under a base URL with a path, relative to it."""
    app = make_app(BASE_URL="http://example.org/base/")
    with TestClient(app) as client:
        run(client, app, [("http://example.org/base/A", EntityType.CLASS)], ontology="http://example.org/base/onto")
        assert "<code>http://example.org/base/A</code>" in client.get("/A").text
        assert "<h1>The &lt;Onto&gt;</h1>" in client.get("/onto").text
        assert client.get("/base/A").status_code == 404


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


@pytest.mark.parametrize(
    ("path", "raw_path", "expected"),
    [
        ("/site/other.org/caf\u00e9", b"/site/other.org/caf%C3%A9", "/other.org/caf%C3%A9"),
        ("/site/other.org/caf\u00e9", b"/other.org/caf%C3%A9", "/other.org/caf%C3%A9"),
        ("/site/site/x", b"/site/site/x", "/site/x"),
        ("/site/site/x", b"/site/x", "/site/x"),
    ],
)
def test_request_path_root_path(path: str, raw_path: bytes, expected: str) -> None:
    """Check that a request's path is its raw path, less the root path only when the raw path has it."""
    scope = {"type": "http", "path": path, "raw_path": raw_path, "root_path": "/site"}
    assert request_path(Request(scope)) == expected


def test_hash_script_contract(client: TestClient) -> None:
    """Check what the hash script and the server agree on: its data attributes, and one main and title per page."""
    script = (STATIC_DIR / "hash.js").read_text()
    page = client.get("/other.org/z").text
    for name, attribute in [("lookup", "data-lookup"), ("path", "data-path"), ("prefixOnly", "data-prefix-only")]:
        assert f"script.dataset.{name}" in script
        assert f'{attribute}="' in page
    response = client.get("/_browl/hash", params={"path": "/other.org/vocab", "fragment": "C"})
    assert (response.text.count("<main"), response.text.count("<title>")) == (1, 1)


def test_meta_read_for_home_only(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the meta is read only for the home page, the one page that shows it."""
    reads: list[None] = []
    monkeypatch.setattr(browl.lookup, "read_meta", lambda connection: reads.append(None) or read_meta(connection))
    for path in ("/other.org/y", "/other.org/x", iri_link("urn:example:fallback"), "/other.org/vocab"):
        assert client.get(path).status_code == 200
    assert reads == []
    assert "<h1>The &lt;Onto&gt;</h1>" in client.get("/onto").text
    assert len(reads) == 1
