"""Tests for the "loading" and "not available" pages, the routes exempt from them, and the admin status route."""

import asyncio
import re
import threading
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from browl.jobs import JobContext, Stage, StageError
from browl.pages import RETRY_AFTER_S, SiteGate, templates
from tests.test_app import make_app

pytestmark = pytest.mark.usefixtures("data_dir")

SECRET_REASON = "<script>alert('reason')</script>"
"""A failure reason that must reach only the admin status, escaped."""

GATED = ["/", "/example.org/onto", "/no/such/page", "/_browl/search", "/_browl/tree/classes", "/_browl/healthz"]
"""Paths gated on the job state: entity paths, the service's own non-admin routes, and paths no route matches."""

EXEMPT = [
    "/_browl/health",
    "/_browl/static/browl.css",
    "/_browl/admin/status",
    "/_browl/admin/openapi.json",
    "/_browl/admin/docs",
]

STATUS = "/_browl/admin/status"
HTMX = {"HX-Request": "true"}


@pytest.fixture
def app() -> FastAPI:
    """Return an app with default settings."""
    return make_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Yield a test client for the app, its lifespan running, with no job run yet."""
    with TestClient(app) as test_client:
        yield test_client


def run_job(client: TestClient, app: FastAPI, reasoning: str | None = None) -> None:
    """Run a load job to its end, whose reasoning stage fails with this reason if given."""

    async def fail(context: JobContext) -> None:
        raise StageError(reasoning)

    if reasoning is not None:
        app.state.jobs.register(Stage.REASONING, fail)
    client.portal.call(app.state.jobs.start)
    client.portal.call(app.state.jobs.wait)


def start_job(client: TestClient, app: FastAPI) -> None:
    """Start a load job, and return once it's in its loading stage, where it stays until the app shuts down."""
    loading = threading.Event()

    async def forever(context: JobContext) -> None:
        loading.set()
        await asyncio.Event().wait()

    app.state.jobs.register(Stage.LOADING, forever)
    client.portal.call(app.state.jobs.start)
    assert loading.wait(timeout=5)


def fragment(client: TestClient) -> str:
    """Return the status route's fragment, as htmx asks for it, with whitespace collapsed."""
    response = client.get(STATUS, headers=HTMX)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "<html" not in response.text
    return " ".join(response.text.split())


@pytest.mark.parametrize("path", GATED)
def test_not_available_without_ontology(client: TestClient, path: str) -> None:
    """Check that every gated path shows the "not available" page when no ontology has been loaded."""
    response = client.get(path)
    assert response.status_code == 503
    assert "Retry-After" not in response.headers
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Type"].startswith("text/html")
    assert "<h1>Not available</h1>" in response.text
    assert "/_browl/static/browl.css" in response.text


def test_not_available_after_failure(client: TestClient, app: FastAPI) -> None:
    """Check that a failed load shows the "not available" page, without the failure reason."""
    run_job(client, app)
    run_job(client, app, reasoning=SECRET_REASON)
    assert app.state.jobs.state.site == "not available"
    response = client.get("/example.org/onto")
    assert response.status_code == 503
    assert "<h1>Not available</h1>" in response.text
    assert "alert" not in response.text
    assert "reasoning" not in response.text


@pytest.mark.parametrize("path", GATED)
def test_loading(client: TestClient, app: FastAPI, path: str) -> None:
    """Check that every gated path shows the "loading" page while a job runs, telling clients when to retry."""
    start_job(client, app)
    response = client.get(path)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(RETRY_AFTER_S)
    assert response.headers["Cache-Control"] == "no-store"
    assert "<h1>Loading the ontology</h1>" in response.text
    assert f'<meta http-equiv="refresh" content="{RETRY_AFTER_S}">' in response.text


def test_loading_other_methods(client: TestClient, app: FastAPI) -> None:
    """Check that the gate covers every method, not just GET."""
    start_job(client, app)
    assert client.post("/example.org/onto").status_code == 503
    assert client.head("/example.org/onto").status_code == 503


def test_available(client: TestClient, app: FastAPI) -> None:
    """Check that once a load is ready the gate passes requests through to the routes."""
    run_job(client, app)
    assert app.state.jobs.state.site == "available"
    response = client.get("/no/such/page")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("path", EXEMPT)
@pytest.mark.parametrize("site", ["loading", "not available"])
def test_exempt(client: TestClient, app: FastAPI, path: str, site: str) -> None:
    """Check that the health, static and admin routes, which include the API docs, are served whatever the job state."""
    if site == "loading":
        start_job(client, app)
    assert app.state.jobs.state.site == site
    assert client.get(path).status_code == 200


@pytest.mark.parametrize(
    ("path", "exempt"),
    [
        ("/_browl/admin", True),
        ("/_browl/admin/upload", True),
        ("/_browl/static", True),
        ("/_browl/administrator", False),
        ("/_browl/statics/x", False),
        ("/x/_browl/admin", False),
    ],
)
def test_exempt_whole_segments(path: str, exempt: bool) -> None:
    """Check that an exempt prefix matches whole path segments only."""
    gate = SiteGate(FastAPI(), ["/_browl/admin", "/_browl/static"])
    assert gate.is_exempt(path) is exempt


def test_exempt_under_root_path(app: FastAPI) -> None:
    """Check that exemption is judged on the path within the app when it's served under a root path."""
    with TestClient(app, root_path="/site") as client:
        assert client.get("/site/_browl/health").status_code == 200
        assert client.get("/site/example.org/onto").status_code == 503


def test_static_css(client: TestClient) -> None:
    """Check that the stylesheet is served, with its palette as custom properties and a dark theme."""
    response = client.get("/_browl/static/browl.css")
    assert response.headers["Content-Type"].startswith("text/css")
    assert "--bg:" in response.text
    assert "prefers-color-scheme: dark" in response.text


def test_status_without_job(client: TestClient) -> None:
    """Check that the status says no ontology has been loaded, and doesn't poll."""
    html = fragment(client)
    assert "No ontology has been loaded yet." in html
    assert "hx-trigger" not in html


def test_status_running(client: TestClient, app: FastAPI) -> None:
    """Check that while a job runs the status shows its stage and start time, and polls itself."""
    start_job(client, app)
    started = app.state.jobs.state.started_at
    html = fragment(client)
    assert "Load 1 is running, started" in html
    assert f'<time datetime="{started.isoformat()}">{started:%Y-%m-%d %H:%M:%S} UTC</time>' in html
    assert 'hx-get="http://testserver/_browl/admin/status" hx-trigger="every 2s" hx-swap="outerHTML"' in html
    stages = re.findall(r'<li class="(\w+)"( aria-current="step")?>([\w-]+)', html)
    assert stages == [
        ("done", "", "uploading"),
        ("done", "", "imports"),
        ("done", "", "reasoning"),
        ("current", ' aria-current="step"', "loading"),
        ("pending", "", "pre-caching"),
        ("pending", "", "ready"),
    ]


def test_status_ready(client: TestClient, app: FastAPI) -> None:
    """Check that after a load the status shows it ready, with every stage done, and stops polling."""
    run_job(client, app)
    html = fragment(client)
    assert "Load 1 is ready, started" in html
    assert "hx-trigger" not in html
    assert re.findall(r'<li class="(\w+)"', html) == ["done"] * 6
    assert 'aria-current="step">ready' in html


def test_status_failed(client: TestClient, app: FastAPI) -> None:
    """Check that after a failure the status shows the failed stage and the reason, escaped."""
    run_job(client, app, reasoning=SECRET_REASON)
    html = fragment(client)
    assert "Load 1 failed, started" in html
    assert "hx-trigger" not in html
    assert '<li class="failed" aria-current="step">reasoning (failed)</li>' in html
    assert re.findall(r'<li class="(\w+)"', html) == ["done", "done", "failed", "pending", "pending", "pending"]
    assert (
        "Failed in stage <strong>reasoning</strong>: &lt;script&gt;alert(&#39;reason&#39;)&lt;/script&gt;</p>" in html
    )
    assert SECRET_REASON not in html


def test_status_page(client: TestClient) -> None:
    """Check that a request not from htmx gets the status in a whole page."""
    response = client.get(STATUS)
    assert response.status_code == 200
    assert "<title>Load status · browl</title>" in response.text
    assert '<section id="job-status"' in response.text


def test_templates_autoescape() -> None:
    """Check that every template autoescapes, including ones not named .html."""
    assert templates.env.autoescape is True
    assert templates.env.from_string("{{ value }}").render(value="<b>&") == "&lt;b&gt;&amp;"
