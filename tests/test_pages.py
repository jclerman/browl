"""Tests for the "loading" and "not available" pages, the routes exempt from them, and the admin status route."""

import asyncio
import re
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import browl.jobs
from browl.derived import DERIVED_DIR
from browl.jobs import (
    INTERRUPTED_BY_RESTART,
    INTERRUPTED_BY_SHUTDOWN,
    STATE_DB,
    JobContext,
    JobState,
    JobStore,
    Site,
    Stage,
)
from browl.jobtypes import StageError
from browl.pages import RETRY_AFTER_S, SiteGate, app_path, link_root, templates
from browl.precache.usage_table import Progress
from tests.helpers import derived_stage, make_app

pytestmark = pytest.mark.usefixtures("data_dir")

SECRET_REASON = "<script>alert('reason')</script>"
"""A failure reason that must reach only the admin status, escaped."""

GATED = [
    "/",
    "/example.org/onto",
    "/no/such/page",
    "/_browl/search",
    "/_browl/tree/classes",
    "/_browl/healthz",
    "/_browl/hash?path=/x&fragment=y",
    "/_browl/iri?iri=x",
]
"""Paths gated on the job state: entity paths, the service's own non-admin routes, and paths no route matches."""

EXEMPT = [
    "/_browl/health",
    "/_browl/static/browl.css",
    "/_browl/admin/status",
    "/_browl/admin/openapi.json",
    "/_browl/admin/docs",
]

STATUS = "/_browl/admin/status"
UPLOAD = "/_browl/admin/upload"
BASE = "http://testserver"
"""The origin of the test client's requests, as htmx's `HX-Current-URL` would name it."""
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
    """Run a load job to its end, building an empty derived database, unless its reasoning stage fails with a reason."""

    async def fail(context: JobContext) -> None:
        raise StageError(reasoning)

    if reasoning is not None:
        app.state.jobs.register(Stage.REASONING, fail)
    app.state.jobs.register(Stage.PRE_CACHING, derived_stage())
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


def steps(html: str) -> list[tuple[str, str, str]]:
    """Return each stage box's state, `aria-current` attribute (or ""), and label, from the status fragment."""
    return re.findall(
        r'<li class="step step-(\w+)" tabindex="0" aria-describedby="step-tip-[\w-]+"( aria-current="step")?>'
        r' <span class="step-icon" aria-hidden="true"></span> <span class="step-label">([^<]+)</span>',
        html,
    )


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
    assert app.state.jobs.state.site == Site.NOT_AVAILABLE
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


@pytest.mark.parametrize(
    "path", ["/example.org/onto", "/_browl/iri?iri=x", "/_browl/hash?path=/x&fragment=y", "/_browl/usage-count?iri=x"]
)
def test_loading_without_derived_database(client: TestClient, app: FastAPI, path: str) -> None:
    """Check that the lookup routes serve "loading" when the gate let a request through but no derived database is."""
    run_job(client, app)
    for file in (app.state.settings.data_dir / DERIVED_DIR).iterdir():
        file.unlink()
    response = client.get(path)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(RETRY_AFTER_S)
    assert "<h1>Loading the ontology</h1>" in response.text


def test_loading_other_methods(client: TestClient, app: FastAPI) -> None:
    """Check that the gate covers every method, not just GET."""
    start_job(client, app)
    assert client.post("/example.org/onto").status_code == 503
    assert client.head("/example.org/onto").status_code == 503


def test_available(client: TestClient, app: FastAPI) -> None:
    """Check that once a load is ready the gate passes requests through to the routes."""
    run_job(client, app)
    assert app.state.jobs.state.site == Site.AVAILABLE
    response = client.get("/no/such/page")
    assert response.status_code == 404
    assert "<h1>Not found</h1>" in response.text


@pytest.mark.parametrize("path", EXEMPT)
@pytest.mark.parametrize("site", [Site.LOADING, Site.NOT_AVAILABLE])
def test_exempt(client: TestClient, app: FastAPI, path: str, site: Site) -> None:
    """Check that the health, static and admin routes, including the API docs, are served whatever the job state."""
    if site == Site.LOADING:
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


@pytest.mark.parametrize(
    ("root_path", "path", "link_root_", "app_path_"),
    [
        ("", "/x", "", "/x"),
        ("/", "/x", "", "/x"),
        ("/site", "/site/x", "/site", "/x"),
        ("/site/", "/site/x", "/site", "/x"),
        ("/site", "/site", "/site", ""),
        ("/site", "/sitex/y", "/site", "/sitex/y"),
    ],
)
def test_root_path(root_path: str, path: str, link_root_: str, app_path_: str) -> None:
    """Check the root path for links, and that a path within the app is relative to it, by whole segments."""
    scope = {"type": "http", "path": path, "root_path": root_path}
    assert (link_root(scope), app_path(scope)) == (link_root_, app_path_)


@pytest.mark.parametrize("root_path", ["site", "//evil.example", "//"])
def test_root_path_invalid(root_path: str) -> None:
    """Check that a root path that would make links protocol-relative, or relative, is refused."""
    scope = {"type": "http", "path": "/x", "root_path": root_path}
    with pytest.raises(ValueError, match="root path"):
        link_root(scope)
    with pytest.raises(ValueError, match="root path"):
        app_path(scope)


def test_exempt_under_root_path(app: FastAPI) -> None:
    """Check that exemption is judged on the path within the app when it's served under a root path."""
    with TestClient(app, root_path="/site") as client:
        assert client.get("/site/_browl/health").status_code == 200
        response = client.get("/site/example.org/onto")
        assert response.status_code == 503
        assert 'href="/site/_browl/static/browl.css"' in response.text


def test_links_ignore_request_host(client: TestClient, app: FastAPI) -> None:
    """Check that pages link to root-relative paths, never to the scheme and host a request claims."""
    start_job(client, app)
    forged = {"Host": "evil.example", "X-Forwarded-Host": "evil.example"}
    pages = [
        client.get("/example.org/onto", headers=forged),
        client.get(STATUS, headers=forged),
        client.get(STATUS, headers=forged | HTMX),
    ]
    for response in pages:
        assert "evil.example" not in response.text
    assert 'href="/_browl/static/browl.css"' in pages[0].text
    assert 'hx-get="/_browl/admin/status"' in pages[2].text


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
    assert "Running</span>" in html
    assert f'Load 1 · started <time datetime="{started.isoformat()}">{started:%Y-%m-%d %H:%M:%S} UTC</time>' in html
    assert "· running for " in html
    assert 'hx-get="/_browl/admin/status" hx-trigger="every 2s" hx-swap="outerHTML"' in html
    assert steps(html) == [
        ("done", "", "Uploaded"),
        ("done", "", "Ontology Read"),
        ("skipped", "", "No Reasoner"),
        ("current", ' aria-current="step"', "Storing Triples"),
        ("pending", "", "Pre-Cache"),
        ("pending", "", "Publish"),
    ]


def test_status_ready(client: TestClient, app: FastAPI) -> None:
    """Check that after a load the status shows Ready highlighted, and stops polling (#85)."""
    run_job(client, app)
    html = fragment(client)
    assert "Ready</span>" in html
    assert "· took " in html
    assert "hx-trigger" not in html
    assert [state for state, _, _ in steps(html)] == ["done", "done", "skipped", "done", "done", "done"]
    assert "aria-current" not in html
    assert ">Published</span>" in html


def test_status_failed(client: TestClient, app: FastAPI) -> None:
    """Check that after a failure the status shows the failed stage and the reason, escaped."""
    run_job(client, app, reasoning=SECRET_REASON)
    html = fragment(client)
    assert "Failed</span>" in html
    assert "· failed after " in html
    assert "hx-trigger" not in html
    assert steps(html) == [
        ("done", "", "Uploaded"),
        ("done", "", "Ontology Read"),
        ("failed", ' aria-current="step"', "Inference Failed"),
        ("pending", "", "Store Triples"),
        ("pending", "", "Pre-Cache"),
        ("pending", "", "Publish"),
    ]
    assert "<strong>Inference Failed</strong>: &lt;script&gt;alert(&#39;reason&#39;)&lt;/script&gt;</p>" in html
    assert SECRET_REASON not in html


def test_status_running_progress(client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check the running stage's progress: its box's time so far, a done box's time, and the details panel.

    The panel says what the stage is doing, with a bar for how far it has got.
    """
    monkeypatch.setattr(browl.jobs, "PROGRESS_S", 0)
    reported = threading.Event()

    async def loading(context: JobContext) -> None:
        context.report("Added 640,000 of 1,167,135 triples (55%)", 640_000 / 1_167_135)
        await asyncio.sleep(0.05)  # the report is recorded
        reported.set()
        await asyncio.Event().wait()

    app.state.jobs.register(Stage.LOADING, loading)
    client.portal.call(app.state.jobs.start)
    assert reported.wait(timeout=5)
    html = fragment(client)
    took = '<span class="step-time"><span class="visually-hidden">took </span>0 s</span>'
    assert f'<span class="step-label">Uploaded</span> {took}' in html
    assert '<span class="step-label">No Reasoner</span> <span class="step-tip"' in html  # skipped: no time
    assert (
        '<span class="step-label">Storing Triples</span> <span class="step-time">'
        '<span class="visually-hidden">running for </span>0 s</span>'
    ) in html
    assert (
        '<div class="step-panel" role="group" aria-labelledby="step-panel-current">'
        ' <p class="step-panel-head"><strong id="step-panel-current">Storing Triples</strong> · running for 0 s</p>'
        ' <p class="step-detail">Added 640,000 of 1,167,135 triples (55%)</p>'
        ' <progress class="step-progress" max="100" value="54.8" aria-labelledby="step-panel-current">55%</progress>'
    ) in html
    stored = JobStore(app.state.settings.data_dir / STATE_DB).read()
    assert (stored.detail, stored.fraction) == ("Added 640,000 of 1,167,135 triples (55%)", 640_000 / 1_167_135)


def test_status_running_no_detail(client: TestClient, app: FastAPI) -> None:
    """Check that the panel of a stage that hasn't said what it's doing has just its label and time, and no bar."""
    start_job(client, app)
    html = fragment(client)
    assert '<strong id="step-panel-current">Storing Triples</strong> · running for ' in html
    assert 'class="step-detail"' not in html
    assert "<progress" not in html


class FakeTables:
    """Usage tables whose build of the ready generation's table is running with some progress, or failed."""

    def __init__(self, progress: Progress | None = None, failed: bool = False) -> None:  # noqa: D107
        self._progress, self._failed = progress, failed

    def progress(self, generation: int) -> Progress | None:  # noqa: D102
        return self._progress

    def failed(self, generation: int) -> bool:  # noqa: D102
        return self._failed


def test_status_background_build(client: TestClient, app: FastAPI) -> None:
    """Check that once the job is ready, the usage table's build shows after publishing.

    The fragment polls without redirecting until the build ends.
    """
    run_job(client, app)
    app.state.usage_tables = FakeTables(Progress(triples=200, scanned=50))
    response = client.get(f"{STATUS}?background", headers=HTMX)
    assert "HX-Redirect" not in response.headers
    html = " ".join(response.text.split())
    assert f'hx-get="{STATUS}?background" hx-trigger="every 2s" hx-swap="outerHTML"' in html
    assert '<h2 class="background-title">After publishing</h2>' in html
    assert '<strong id="background-1">Building the usage table</strong> · running for 0 s</p>' in html
    assert '<p class="step-detail">Scanned 50 of 200 triples (25%)</p>' in html
    assert 'max="100" value="25.0"' in html
    assert "HX-Redirect" in client.get(STATUS, headers=HTMX).headers  # a poll from when the job ran
    app.state.usage_tables = FakeTables()
    html = fragment(client)
    assert "hx-trigger" not in html
    assert "After publishing" not in html


def test_status_background_build_failed(client: TestClient, app: FastAPI) -> None:
    """Check that a failed usage table build says so, and doesn't poll."""
    run_job(client, app)
    app.state.usage_tables = FakeTables(failed=True)
    html = fragment(client)
    assert '<p class="failure">The usage table\'s build failed' in html
    assert "hx-trigger" not in html


def test_status_poll_running_no_redirect(client: TestClient, app: FastAPI) -> None:
    """Check that a poll while a job runs swaps in the fragment, without leaving the page."""
    start_job(client, app)
    assert "HX-Redirect" not in client.get(STATUS, headers={**HTMX, "HX-Current-URL": f"{BASE}{UPLOAD}"}).headers


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (None, STATUS),
        (f"{BASE}{STATUS}", STATUS),
        (f"{BASE}{UPLOAD}", UPLOAD),
        (f"{BASE}{UPLOAD}?x=1#job-status", UPLOAD),
        ("https://attacker.test/", STATUS),
        ("//attacker.test/x", STATUS),
        (f"{BASE}/_browl/admin/docs", STATUS),
        (f"{BASE}{UPLOAD}/../docs", STATUS),
        ("https://attacker.test/_browl/admin/upload", UPLOAD),
    ],
)
@pytest.mark.parametrize("reasoning", [None, SECRET_REASON])
def test_status_poll_ended_redirects(
    client: TestClient, app: FastAPI, reasoning: str | None, current: str | None, target: str
) -> None:
    """Check that a poll finding the job ended, ready or failed, sends htmx to a fresh GET of its page (#101).

    The page is the upload page's or the status page's path, whatever the `HX-Current-URL` it sent; any other path,
    or none, goes to the status page. Another site's URL with the upload page's path is harmless: only the path is used.
    """
    run_job(client, app, reasoning)
    headers = HTMX if current is None else {**HTMX, "HX-Current-URL": current}
    response = client.get(STATUS, headers=headers)
    assert response.headers["HX-Redirect"] == target
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", [STATUS, UPLOAD])
def test_status_page_ended_no_redirect(client: TestClient, app: FastAPI, path: str) -> None:
    """Check that a page loaded after the job ended doesn't redirect or poll, so it can't reload in a loop."""
    run_job(client, app)
    response = client.get(path, headers={"HX-Current-URL": f"{BASE}{path}"})
    assert "HX-Redirect" not in response.headers
    assert f'hx-get="{STATUS}' not in response.text


def test_status_page(client: TestClient, app: FastAPI) -> None:
    """Check that a request not from htmx gets the status in a page, which refreshes without JS while a job runs."""
    refresh = f'<noscript><meta http-equiv="refresh" content="2; url={STATUS}"></noscript>'
    response = client.get(STATUS)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "<title>Load status · browl</title>" in response.text
    assert '<section id="job-status"' in response.text
    assert "refresh" not in response.text
    start_job(client, app)
    assert refresh in client.get(STATUS).text
    assert "refresh" not in client.get(STATUS, headers=HTMX).text


def test_shutdown_cancels_job(app: FastAPI, data_dir: Path) -> None:
    """Check that the app cancels a running job when it shuts down, recording why."""
    with TestClient(app) as client:
        start_job(client, app)
    state = JobStore(data_dir / STATE_DB).read()
    assert (state.failed_stage, state.reason) == (Stage.LOADING, INTERRUPTED_BY_SHUTDOWN)


def test_templates_autoescape() -> None:
    """Check that every template autoescapes, including ones not named .html."""
    assert templates.env.autoescape is True
    assert templates.env.from_string("{{ value }}").render(value="<b>&") == "&lt;b&gt;&amp;"


def test_restart_serves_loaded_ontology() -> None:
    """Check that an app started on the DATA_DIR of one that loaded an ontology serves it, as the loaded generation."""
    first = make_app()
    with TestClient(first) as client:
        run_job(client, first)
    with TestClient(make_app()) as client:
        assert client.get("/no/such/page").status_code == 404  # past the gate, to the router
        assert "Ready</span> <span>Load 1 ·" in fragment(client)


def test_restart_after_interrupted_job(data_dir: Path) -> None:
    """Check that an app started after a process stopped mid-job shows "not available", and why, to admins."""
    started = datetime(2026, 1, 2, tzinfo=UTC)
    JobStore(data_dir / STATE_DB).write(
        JobState(generation=2, stage=Stage.LOADING, started_at=started, ready_generation=1)
    )
    with TestClient(make_app()) as client:
        response = client.get("/example.org/onto")
        assert response.status_code == 503
        assert "<h1>Not available</h1>" in response.text
        assert f"<strong>Storing Failed</strong>: {INTERRUPTED_BY_RESTART}" in fragment(client)
