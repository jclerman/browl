"""Tests for the page frame (P-14a, DESIGN.md §2.9): the base template's navigation bar and footer, and the CSP."""

import asyncio
from collections.abc import Iterator, Sequence

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from browl.derived import EntityType
from browl.jobs import JobContext, Stage
from browl.pages import CONTENT_SECURITY_POLICY, MAX_NAME_CHARS
from tests.helpers import derived_stage, make_app

pytestmark = pytest.mark.usefixtures("data_dir")

ONTOLOGY = "http://example.org/onto"  # native under BASE_URL http://example.org/, so at /onto
TREE_LINKS = [
    '<a href="/_browl/tree/classes">Classes</a>',
    '<a href="/_browl/tree/object-properties">Object properties</a>',
    '<a href="/_browl/tree/data-properties">Data properties</a>',
    '<a href="/_browl/tree/annotation-properties">Annotation properties</a>',
]
ADMIN_LINK = '<a href="/_browl/admin/upload">Admin</a>'
SIGN_IN = '<footer class="site-footer">\n    <a href="/_browl/admin/upload">Sign in</a>'


def run(
    client: TestClient,
    app: FastAPI,
    ontology: str | None = ONTOLOGY,
    title: str | None = "The <Onto>",
    entities: Sequence[tuple[str, EntityType]] = (("http://example.org/x", EntityType.CLASS),),
) -> None:
    """Run a load job whose derived database holds the ontology, if given, with a title, and the entities."""
    app.state.jobs.register(Stage.PRE_CACHING, derived_stage(entities, ontology, title=title))
    client.portal.call(app.state.jobs.start)
    client.portal.call(app.state.jobs.wait)


def nav(html: str) -> str:
    """Return a page's navigation bar."""
    start = html.index('<header class="site-bar">')
    return html[start : html.index("</header>", start)]


@pytest.fixture
def app() -> FastAPI:
    """Return an app with default settings."""
    return make_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Yield a test client for the app, with the ontology loaded."""
    with TestClient(app) as test_client:
        run(test_client, app)
        yield test_client


@pytest.mark.parametrize(
    "path",
    ["/onto", "/example.org/x", "/no/such/page", "/_browl/admin/status", "/_browl/admin/upload"],
)
def test_frame_on_every_page(client: TestClient, path: str) -> None:
    """Check that the home, entity, "not found" and admin pages have the navigation bar, its links, and the footer."""
    html = client.get(path).text
    bar = nav(html)
    assert '<a href="/onto"><span class="brand">browl</span>' in bar
    assert "<bdi>The &lt;Onto&gt;</bdi>" in bar
    for link in TREE_LINKS:
        assert link in bar
    assert ADMIN_LINK not in bar
    assert SIGN_IN in html


async def stall(context: JobContext) -> None:
    """Wait until the app shuts down: a load job stage that keeps the site loading."""
    await asyncio.Event().wait()


@pytest.mark.parametrize("loaded", [False, True])
def test_frame_on_unavailable_pages(app: FastAPI, loaded: bool) -> None:
    """Check that "not available" has the frame without the ontology, and that "loading" keeps the served one's."""
    with TestClient(app) as client:
        if loaded:
            run(client, app)
            app.state.jobs.register(Stage.LOADING, stall)
            client.portal.call(app.state.jobs.start)
        response = client.get("/onto")
        assert response.status_code == 503
        assert ("Retry-After" in response.headers) is loaded
        bar = nav(response.text)
        assert TREE_LINKS[0] in bar
        assert ("The &lt;Onto&gt;" in bar) is loaded
        assert SIGN_IN in response.text


def admin_marker(app: ASGIApp) -> ASGIApp:
    """Wrap an app so every request is marked as an admin's, as P-22 marks them."""

    async def marked(scope: Scope, receive: Receive, send: Send) -> None:
        scope.setdefault("state", {})["is_admin"] = True
        await app(scope, receive, send)

    return marked


def test_admin_link(app: FastAPI) -> None:
    """Check that the navigation bar has the admin link when the request is marked as an admin's."""
    app.add_middleware(admin_marker)  # type: ignore[arg-type]
    with TestClient(app) as client:
        run(client, app)
        assert ADMIN_LINK in nav(client.get("/example.org/x").text)


def test_no_ontology_iri(app: FastAPI) -> None:
    """Check that an ontology with neither IRI nor title gets just browl's name, linking nowhere."""
    with TestClient(app) as client:
        run(client, app, ontology=None, title=None)
        bar = nav(client.get("/example.org/x").text)
        assert '<span class="site-name"><span class="brand">browl</span></span>' in bar


def test_iri_as_name_and_hash_home(app: FastAPI) -> None:
    """Check that an untitled ontology is named by its IRI, and that a hash IRI's home link has its fragment."""
    with TestClient(app) as client:
        run(client, app, ontology="http://example.org/vocab#onto", title=None)
        bar = nav(client.get("/example.org/x").text)
        assert '<a href="/vocab#onto">' in bar
        assert "<bdi>http://example.org/vocab#onto</bdi>" in bar


def test_long_name_cut(app: FastAPI) -> None:
    """Check that a long ontology name is cut short."""
    with TestClient(app) as client:
        run(client, app, title="x" * (MAX_NAME_CHARS + 1))
        assert f"<bdi>{'x' * (MAX_NAME_CHARS - 1)}…</bdi>" in nav(client.get("/onto").text)


def test_new_generation_renamed(client: TestClient, app: FastAPI) -> None:
    """Check that the navigation bar shows the newly loaded ontology's name once a new load is ready."""
    assert "The &lt;Onto&gt;" in nav(client.get("/onto").text)
    run(client, app, title="Second")
    assert "<bdi>Second</bdi>" in nav(client.get("/onto").text)


def test_under_root_path(app: FastAPI) -> None:
    """Check that the frame's links are within the app's root path."""
    with TestClient(app, root_path="/site") as client:
        run(client, app)
        html = client.get("/site/example.org/x").text
        assert '<a href="/site/onto">' in nav(html)
        assert '<a href="/site/_browl/tree/classes">Classes</a>' in nav(html)
        assert '<a href="/site/_browl/admin/upload">Sign in</a>' in html


@pytest.mark.parametrize("path", ["/onto", "/no/such/page", "/_browl/admin/status", "/_browl/hash?path=/x&fragment=y"])
def test_content_security_policy(client: TestClient, path: str) -> None:
    """Check that browl's pages forbid inline scripts."""
    assert client.get(path).headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY


def test_content_security_policy_unavailable(app: FastAPI) -> None:
    """Check that the "not available" page, which the gate serves, forbids inline scripts too."""
    with TestClient(app) as client:
        assert client.get("/onto").headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY


@pytest.mark.parametrize("path", ["/_browl/admin/docs", "/_browl/static/browl.css", "/_browl/health"])
def test_no_content_security_policy(client: TestClient, path: str) -> None:
    """Check that Swagger UI, which has an inline script, and responses that aren't browl's pages get no policy."""
    assert "Content-Security-Policy" not in client.get(path).headers
