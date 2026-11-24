"""Tests for the routing of entity URLs: home, entity, disambiguation and hash pages, the fallback route, and 404."""

import html.parser
import sqlite3
import urllib.parse
from collections.abc import Iterator, Mapping, Sequence

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

import browl.lookup
import browl.pages
from browl.derived import EntityType, read_meta
from browl.jobs import Stage
from browl.lookup import request_path
from browl.pages import MAX_NAME_CHARS, STATIC_DIR
from browl.usage_tab import PageNotFoundError, UsageTab
from tests.helpers import derived_stage, iso, make_app

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
CODE = '<code dir="ltr">'
"""How pages open an IRI or CURIE, isolated left to right."""
ONTO_TITLE = iso("The &lt;Onto&gt;")
"""How titles end with the test ontology's title."""


def entity_heading(name: str) -> str:
    """Return an entity page's heading for an HTML-escaped name: the name, isolated, and whole in its tooltip."""
    return f'<h1 title="{iso(name)}" dir="auto">{name}</h1>'


def run(
    client: TestClient,
    app: FastAPI,
    entities: Sequence[tuple[str, EntityType]] = ENTITIES,
    ontology: str | None = ONTOLOGY,
    title: str | None = "The <Onto>",
    names: Mapping[str, tuple[str | None, str | None]] | None = None,
) -> None:
    """Run a load job whose derived database holds the ontology and the entities, by default `ENTITIES`.

    `names` gives IRIs' display names and CURIEs.
    """
    app.state.jobs.register(Stage.PRE_CACHING, derived_stage(entities, ontology, title, names))
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
    assert "<h1><bdi>The &lt;Onto&gt;</bdi></h1>" in response.text
    assert f'{HASH_SCRIPT} data-lookup="/_browl/hash"' in response.text
    assert 'data-path="/onto" data-prefix-only="false"' in response.text


def test_entity(client: TestClient) -> None:
    """Check that an entity's path serves its page, one page for all its types, without the hash script."""
    response = client.get("/other.org/y")
    assert response.status_code == 200
    assert f"{CODE}http://other.org/y</code>" in response.text
    assert "Class · Object property" in response.text
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
    assert f"{CODE}http://other.org/z</code>" in response.text
    assert 'data-path="/other.org/z" data-prefix-only="false"' in response.text


@pytest.mark.parametrize(
    ("path", "iri"), [("/other.org/caf%C3%A9", "http://other.org/café"), ("/other.org/a%2Fb", "http://other.org/a%2Fb")]
)
def test_path_in_uri_form(client: TestClient, path: str, iri: str) -> None:
    """Check that paths are looked up as sent, percent-encoded, not as the router decodes them."""
    response = client.get(path)
    assert response.status_code == 200
    assert f"{CODE}{iri}</code>" in response.text
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
        assert f"{CODE}{iri}</code>" in page.text


def test_hash_prefix(client: TestClient) -> None:
    """Check that a path only hash entities have serves the hash page, with status 200 (U-13)."""
    response = client.get("/other.org/vocab")
    assert response.status_code == 200
    assert 'data-path="/other.org/vocab" data-prefix-only="true"' in response.text
    assert "<noscript>" in response.text


@pytest.mark.parametrize(
    ("path", "fragment", "expected"),
    [
        ("/other.org/vocab", "C", entity_heading("http://other.org/vocab#C")),
        ("/onto", "A", entity_heading("http://example.org/onto#A")),
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
    assert f"{CODE}{iri}</code>" in response.text


def test_fallback_punned(client: TestClient) -> None:
    """Check that an IRI with several types and a fallback URL is served there as one entity."""
    response = client.get(iri_link("urn:example:p"))
    assert response.status_code == 200
    assert "Class · Object property" in response.text


@pytest.mark.parametrize("iri", ["http://other.org/vocab#E", "https://other.org/vocab#E"])
def test_fallback_route_shared_hash_url(client: TestClient, iri: str) -> None:
    """Check that the fallback route serves each hash IRI that shares a URL, rather than redirecting to it."""
    response = client.get(iri_link(iri), follow_redirects=False)
    assert response.status_code == 200
    assert f"{CODE}{iri}</code>" in response.text
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
    assert f"{CODE}http://other.org/a|b</code>" in response.text


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
        assert f"{CODE}http://_browl/x</code>" in client.get("/_browl/x").text  # no longer reserved
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
        assert client.get(iri_link(ONTOLOGY)).text.count("<h1><bdi>The &lt;Onto&gt;</bdi></h1>") == 1


def test_base_url_with_path() -> None:
    """Check that native entities are served under a base URL with a path, relative to it."""
    app = make_app(BASE_URL="http://example.org/base/")
    with TestClient(app) as client:
        run(client, app, [("http://example.org/base/A", EntityType.CLASS)], ontology="http://example.org/base/onto")
        assert f"{CODE}http://example.org/base/A</code>" in client.get("/A").text
        assert "<h1><bdi>The &lt;Onto&gt;</bdi></h1>" in client.get("/onto").text
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
        assert f"{CODE}http://other.org/y</code>" in client.get("/site/other.org/y").text
        assert f"{CODE}http://other.org/café</code>" in client.get("/site/other.org/caf%C3%A9").text
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
    assert "<h1><bdi>The &lt;Onto&gt;</bdi></h1>" in client.get("/onto").text
    assert len(reads) == 1


# Page tabs (DESIGN.md §2.9, U-28)


class _TabParser(html.parser.HTMLParser):
    """Collects a page's tabs' attributes and its tab panel's."""

    def __init__(self) -> None:
        super().__init__()
        self.tabs: list[dict[str, str | None]] = []
        self.panel: dict[str, str | None] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("role") == "tab":
            self.tabs.append(attributes)
        elif attributes.get("role") == "tabpanel":
            self.panel = attributes


def parse_tabs(text: str) -> _TabParser:
    """Return a page's tabs and tab panel, as parsed."""
    parser = _TabParser()
    parser.feed(text)
    return parser


def selected_tab(text: str) -> str | None:
    """Return the id of a page's selected tab; None if it has no tabs."""
    return next((tab["id"] for tab in parse_tabs(text).tabs if tab["aria-selected"] == "true"), None)


@pytest.mark.parametrize(
    ("query", "selected"), [("", "tab-overview"), ("?tab=overview", "tab-overview"), ("?tab=usage", "tab-usage")]
)
def test_entity_tabs(client: TestClient, query: str, selected: str) -> None:
    """Check that an entity page has tabs, the first by default, and shows the one its query names."""
    response = client.get(f"/other.org/y{query}")
    assert response.status_code == 200
    page = parse_tabs(response.text)
    assert [tab["id"] for tab in page.tabs] == ["tab-overview", "tab-usage"]
    assert selected_tab(response.text) == selected
    assert page.panel is not None
    assert page.panel["aria-labelledby"] == selected
    assert ("Logical axioms" in response.text) == (selected == "tab-overview")


@pytest.mark.parametrize(
    ("path", "hrefs", "panels"),
    [
        (
            "/other.org/y?x=1",
            ["/other.org/y", "/other.org/y?tab=usage"],
            ["/other.org/y?panel=1", "/other.org/y?tab=usage&panel=1"],
        ),
        (
            iri_link("urn:example:fallback"),
            [iri_link("urn:example:fallback"), iri_link("urn:example:fallback") + "&tab=usage"],
            [iri_link("urn:example:fallback") + "&panel=1", iri_link("urn:example:fallback") + "&tab=usage&panel=1"],
        ),
        (
            "/_browl/hash?path=/other.org/vocab&fragment=C",
            ["/other.org/vocab#C", "/other.org/vocab?tab=usage#C"],
            [
                "/_browl/hash?path=%2Fother.org%2Fvocab&fragment=C&panel=1",
                "/_browl/hash?path=%2Fother.org%2Fvocab&fragment=C&tab=usage&panel=1",
            ],
        ),
    ],
)
def test_tab_links(client: TestClient, path: str, hrefs: list[str], panels: list[str]) -> None:
    """Check that each tab links its page's URL, with a hash URL's fragment, and its panel's, dropping other params.

    The first tab's URL is the page's own, without `tab`. A hash URL's panels come from the hash route.
    """
    tabs = parse_tabs(client.get(path).text).tabs
    assert [tab["href"] for tab in tabs] == hrefs
    assert [tab["hx-get"] for tab in tabs] == panels


def test_tab_links_under_root_path(app: FastAPI) -> None:
    """Check that the tabs' links include the root path the app is served under."""
    with TestClient(app, root_path="/root") as client:
        run(client, app)
        page = client.get("/root/other.org/y").text
    tabs = parse_tabs(page).tabs
    assert [tab["href"] for tab in tabs] == ["/root/other.org/y", "/root/other.org/y?tab=usage"]
    assert tabs[1]["hx-get"] == "/root/other.org/y?tab=usage&panel=1"
    assert 'hx-get="/root/_browl/usage-count?iri=http%3A%2F%2Fother.org%2Fy"' in page  # the Usage label's count


@pytest.mark.parametrize(
    "path",
    ["/other.org/y?tab=usage&panel=1", "/_browl/hash?path=/other.org/vocab&fragment=C&tab=usage&panel=1"],
)
def test_panel_alone(client: TestClient, path: str) -> None:
    """Check that `panel=1` serves the tab panel's content alone, with no page frame, and not indexed."""
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["X-Robots-Tag"] == "noindex"
    assert response.headers["Content-Type"].startswith("text/html")
    assert '<section class="usage">' in response.text
    for frame in ["<html", "<main", 'role="tablist"', 'role="tabpanel"', "site-bar"]:
        assert frame not in response.text


def test_panel_alone_default_tab(client: TestClient) -> None:
    """Check that `panel=1` without `tab` serves the first tab's panel."""
    response = client.get("/other.org/y?panel=1")
    assert response.status_code == 200
    assert "Logical axioms" in response.text


HASH_C = "/_browl/hash?path=/other.org/vocab&fragment=C"
"""The hash route's URL for an entity page."""


@pytest.mark.parametrize(
    "path",
    [
        "/other.org/y?tab=nope",
        "/other.org/y?tab=nope&panel=1",
        "/other.org/y?tab=usage&tab=overview",
        "/other.org/y?panel=2",
        "/other.org/y?panel=1&panel=1",
        "/other.org/y?tab=",
        "/onto?panel=1",
        "/other.org/x?panel=1",
        "/other.org/z?tab=nope&panel=1",
        "/other.org/vocab?panel=1",
        "/other.org/vocab?tab=usage&tab=overview",
        f"{HASH_C}&tab=nope&panel=1",
        f"{HASH_C}&tab=usage&tab=overview",
        f"{HASH_C}&panel=1&panel=1",
        "/_browl/hash?path=/other.org/vocab&fragment=E&panel=1",
        iri_link("urn:example:fallback") + "&tab=nope",
        iri_link("urn:example:fallback") + "&panel=2",
        iri_link("http://other.org/y") + "&tab=usage&tab=overview",
    ],
)
def test_tab_not_found(client: TestClient, path: str) -> None:
    """Check that a tab the page lacks, a malformed tab query, or a panel of a page without tabs, is a 404."""
    response = client.get(path)
    assert response.status_code == 404
    assert "<h1>Not found</h1>" in response.text


@pytest.mark.parametrize(
    ("path", "selected"),
    [
        ("/onto?tab=nope", None),
        ("/other.org/z?tab=nope", "tab-overview"),
        ("/other.org/x?tab=usage", None),
        (f"{HASH_C}&tab=nope", "tab-overview"),
        ("/_browl/hash?path=/other.org/vocab&fragment=E&tab=usage", None),
    ],
)
def test_tab_ignored(client: TestClient, path: str, selected: str | None) -> None:
    """Check that a page without tabs, a hash entity's, or one with the hash script, serves a tab it lacks, not a 404.

    Their query may be meant for another fragment's entity, or carried on to the choose page.
    """
    response = client.get(path)
    assert response.status_code == 200
    assert selected_tab(response.text) == selected


def test_usage_page_numbers(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check the Usage tab's page numbers: `page=1` redirects to the URL without it, so each page has one URL.

    A page past the last is a 404, but on a page with the hash-IRI script, which its fragment's entity may have, the
    script is served, with the URL's own entity's first page.
    """

    async def read_usage_tab(request: object, state: object, iri: str, page: int) -> UsageTab:
        if page > 2:
            raise PageNotFoundError(f"page {page} of 2")
        return UsageTab(count=60, page=page, pages=2, groups=[])

    monkeypatch.setattr(browl.lookup, "read_usage_tab", read_usage_tab)
    first = client.get("/other.org/y?tab=usage&page=1", follow_redirects=False)
    assert (first.status_code, first.headers["Location"]) == (301, "/other.org/y?tab=usage")
    fallback = client.get(iri_link("urn:example:fallback") + "&tab=usage&page=1", follow_redirects=False)
    assert (fallback.status_code, fallback.headers["Location"]) == (
        301,
        iri_link("urn:example:fallback") + "&tab=usage",
    )
    assert client.get("/other.org/y?tab=usage&page=1&panel=1").status_code == 200  # a panel, which htmx fetches
    assert client.get("/other.org/y?tab=usage&page=2").status_code == 200
    assert client.get("/other.org/y?tab=usage&page=3").status_code == 404
    for page in ("3", "x"):
        hashed = client.get(f"/other.org/z?tab=usage&page={page}")  # z#D's URL too
        assert hashed.status_code == 200 and HASH_SCRIPT in hashed.text
        assert 'value="1"' in hashed.text
    params = {"path": "/other.org/z", "fragment": "D", "tab": "usage"}
    assert client.get("/_browl/hash", params={**params, "page": "3"}).status_code == 404  # the script's lookup
    assert client.get("/_browl/hash", params={**params, "page": "1"}).status_code == 200


@pytest.mark.parametrize(("tab", "query"), [("usage", "?tab=usage"), ("nope", "")])
def test_fallback_redirect_keeps_tab(client: TestClient, tab: str, query: str) -> None:
    """Check that the fallback route's redirect to an entity's own URL keeps a known tab, before any fragment."""
    response = client.get(iri_link("http://other.org/vocab#C") + f"&tab={tab}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"] == f"/other.org/vocab{query}#C"


NAMES = {
    "http://other.org/y": ("Why <y>", "oth:y"),
    "http://other.org/x": ("Ex", None),
    f"{ONTOLOGY}#A": ("a" * 300, None),
}


def test_entity_header(app: FastAPI) -> None:
    """Check the entity page's header (U-5): the display name, the types, and the IRI and CURIE in a <details>."""
    with TestClient(app) as client:
        run(client, app, names=NAMES)
        text = client.get("/other.org/y").text
    assert f"<title>{iso('Why &lt;y&gt;')} · {ONTO_TITLE}</title>" in text
    assert entity_heading("Why &lt;y&gt;") in text
    assert '<p class="entity-type" title="Class · Object property">Class · Object property</p>' in text
    start = text.index('<details class="entity-iri">')
    details = text[start : text.index("</details>", start)]
    assert f"<dd>{CODE}http://other.org/y</code></dd>" in details
    assert "<dt>CURIE</dt>" in details
    assert f"<dd>{CODE}oth:y</code></dd>" in details


def test_entity_header_without_names(client: TestClient) -> None:
    """Check that an entity without a display name is named by its IRI, and that one without a CURIE shows none."""
    text = client.get("/other.org/y").text
    assert entity_heading("http://other.org/y") in text
    assert f"<title>{iso('http://other.org/y')} · {ONTO_TITLE}</title>" in text
    assert f"<dd>{CODE}http://other.org/y</code></dd>" in text
    assert "CURIE" not in text


def test_entity_long_name(app: FastAPI) -> None:
    """Check that a long display name is cut short in the heading and the title."""
    with TestClient(app) as client:
        run(client, app, names=NAMES)
        text = client.get("/_browl/hash?path=/onto&fragment=A").text
    cut = "a" * (MAX_NAME_CHARS - 1) + "…"
    assert entity_heading(cut) in text
    assert f"<title>{iso(cut)} · {ONTO_TITLE}</title>" in text


@pytest.mark.parametrize(
    ("title", "path", "expected"),
    [
        ("The <Onto>", "/onto", f"<title>{iso('The &lt;Onto&gt;')} · browl</title>"),
        (None, "/onto", f"<title>{iso(ONTOLOGY)} · browl</title>"),
        (None, "/other.org/y", f"<title>{iso('http://other.org/y')} · browl</title>"),
        (None, "/no/such/page", "<title>Not found · browl</title>"),
    ],
)
def test_page_titles(app: FastAPI, title: str | None, path: str, expected: str) -> None:
    """Check that titles end with the ontology's title only if it has one, and the home page's doesn't repeat it."""
    with TestClient(app) as client:
        run(client, app, title=title)
        assert expected in client.get(path).text


def test_title_isolates_names(app: FastAPI) -> None:
    """Check that a name with a right-to-left override can't reorder the rest of the title (DESIGN.md §2.9)."""
    with TestClient(app) as client:
        run(client, app, title="Onto\u202e", names={"http://other.org/y": ("Heart\u202e", None)})
        text = client.get("/other.org/y").text
    assert "<title>\u2068Heart\u202e\u2069 · \u2068Onto\u202e\u2069</title>" in text


def test_home_long_title(app: FastAPI) -> None:
    """Check that the home page's heading and title cut a long ontology title, and isolate it."""
    with TestClient(app) as client:
        run(client, app, title="t" * 300)
        text = client.get("/onto").text
    cut = "t" * (MAX_NAME_CHARS - 1) + "…"
    assert f"<h1><bdi>{cut}</bdi></h1>" in text
    assert f"<title>{iso(cut)} · browl</title>" in text


def test_home_title_without_frame(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the home page names the ontology in its title even while the navigation bar can't read its name."""

    def unreadable(connection: object) -> None:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(browl.pages, "read_ontology_link", unreadable)
    with TestClient(app) as client:
        run(client, app)
        text = client.get("/onto").text
    assert f"<title>{ONTO_TITLE} · browl</title>" in text
    assert "<bdi>None</bdi>" not in text


def test_disambiguation_names(app: FastAPI) -> None:
    """Check that the disambiguation page links each IRI by its display name, else its IRI."""
    with TestClient(app) as client:
        run(client, app, names=NAMES)
        text = client.get("/other.org/x").text
    assert f'<a href="{iri_link("http://other.org/x")}"><bdi>Ex</bdi></a> {CODE}http://other.org/x</code>' in text
    link = iri_link("https://other.org/x")
    assert f'<a href="{link}"><bdi>https://other.org/x</bdi></a> {CODE}https://other.org/x</code>' in text


def test_disambiguation_long_name(app: FastAPI) -> None:
    """Check that the disambiguation page cuts a long display name."""
    with TestClient(app) as client:
        run(client, app, names={"https://other.org/x": ("b" * 300, None)})
        text = client.get("/other.org/x").text
    assert f"<bdi>{'b' * (MAX_NAME_CHARS - 1)}…</bdi></a>" in text
