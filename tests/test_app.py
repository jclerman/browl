"""Tests for the FastAPI application and its health route."""

import asyncio
import re
import tomllib
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

import browl
import browl.app
from browl import admin
from browl.admin import SWAGGER_UI_URL
from browl.app import create_app
from browl.fuseki import FusekiClient
from browl.jobs import JobRunner, Site, Stage
from tests.helpers import derived_stage, make_app, make_settings

UNREACHABLE = "http://127.0.0.1:1"  # nothing listens on port 1, so connections are refused at once


pytestmark = pytest.mark.usefixtures("data_dir")


Answer = httpx2.Response | Exception | Callable[[httpx2.Request], Awaitable[httpx2.Response]]
"""What the fake Fuseki does with a request: return a response, raise an exception, or call an async handler."""


def check_health(answer: Answer, path: str = "/_browl/health", **overrides: object) -> tuple[Any, list[httpx2.Request]]:
    """Call the health route of an app whose Fuseki client, built from the settings, gets this answer.

    Returns:
        The route's JSON response (after checking its status is 200), and the requests sent to Fuseki.
    """
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx2.Response):
            return answer
        return await answer(request)

    with TestClient(make_app(httpx2.MockTransport(handler), **overrides)) as client:
        response = client.get(path)
    assert response.status_code == 200
    return response.json(), requests


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Yield a test client for an app with default settings, its lifespan running."""
    with TestClient(make_app()) as test_client:
        yield test_client


def test_health_fuseki_up() -> None:
    """Check that the health route pings Fuseki once and reports it up when it answers."""
    body, requests = check_health(httpx2.Response(200))
    assert body == {"status": "ok", "fuseki": "up"}
    assert [str(request.url) for request in requests] == ["http://fuseki:3030/$/ping"]


@pytest.mark.parametrize(
    ("fuseki_url", "ping_url"),
    [
        ("http://triplestore:3031", "http://triplestore:3031/$/ping"),
        ("http://example.org/fuseki", "http://example.org/fuseki/$/ping"),
        ("http://example.org/fuseki/", "http://example.org/fuseki/$/ping"),
    ],
)
def test_health_pings_fuseki_url(fuseki_url: str, ping_url: str) -> None:
    """Check that the ping goes to FUSEKI_URL, including one with a path, with or without a trailing slash."""
    _, requests = check_health(httpx2.Response(200), FUSEKI_URL=fuseki_url)
    assert [str(request.url) for request in requests] == [ping_url]


@pytest.mark.parametrize(
    "answer",
    [httpx2.Response(301), httpx2.Response(404), httpx2.Response(503), httpx2.ReadTimeout("timed out")],
    ids=["redirect", "not-found", "unavailable", "timeout"],
)
def test_health_fuseki_down(answer: Answer) -> None:
    """Check that the app stays healthy, reporting Fuseki down, when Fuseki fails or doesn't answer."""
    body, _ = check_health(answer)
    assert body == {"status": "ok", "fuseki": "down"}


def test_health_fuseki_slow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the time limit covers the whole call to Fuseki, not each phase: a slow answer reports it down."""

    async def slow(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(1)
        return httpx2.Response(200)

    monkeypatch.setattr(browl.app, "FUSEKI_TIMEOUT_S", 0.05)
    body, _ = check_health(slow)
    assert body == {"status": "ok", "fuseki": "down"}


def test_health_fuseki_unreachable() -> None:
    """Check that the app's default transport reports a refused connection as down."""
    with TestClient(make_app(FUSEKI_URL=UNREACHABLE)) as client:
        assert client.get("/_browl/health").json() == {"status": "ok", "fuseki": "down"}


def test_reserved_prefix() -> None:
    """Check that the service's own routes live under RESERVED_PREFIX."""
    body, _ = check_health(httpx2.Response(200), path="/_site/health", RESERVED_PREFIX="_site")
    assert body["fuseki"] == "up"
    with TestClient(make_app(RESERVED_PREFIX="_site")) as client:
        # An ordinary path now, so with no ontology loaded it gets the "not available" page.
        assert client.get("/_browl/health").status_code == 503


def test_create_app_loads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that create_app reads the settings from the environment when none are given."""
    monkeypatch.setenv("BASE_URL", "http://example.org/")
    monkeypatch.setenv("AUTH_DISABLED", "true")
    assert create_app().state.settings.base_url == make_settings().base_url


def test_api_docs_show_title_and_version(client: TestClient) -> None:
    """Check that the OpenAPI schema, which the Swagger page shows, has browl's title and its pyproject.toml version."""
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    info = client.get("/_browl/admin/openapi.json").json()["info"]
    assert info["title"] == "OWL Ontology Browser"
    assert info["version"] == browl.__version__ == pyproject["project"]["version"]


@pytest.mark.parametrize(("root_path", "servers"), [("", None), ("/site", [{"url": "/site"}])])
def test_api_docs_servers(root_path: str, servers: list[dict[str, str]] | None) -> None:
    """Check that the schema names the root path as its server, so "Try it out" calls paths under it."""
    with TestClient(make_app(), root_path=root_path) as client:
        schema = client.get(f"{root_path}/_browl/admin/openapi.json").json()
    assert schema.get("servers") == servers


@pytest.mark.parametrize("prefix", ["_browl", "_site"])
def test_api_docs_are_under_admin(prefix: str) -> None:
    """Check that the API docs are admin routes, using a pinned Swagger UI, and nothing is at FastAPI's default paths.

    An ontology is loaded, so the gate passes requests through and the default paths reach the catch-all route.
    """
    with TestClient(make_app(RESERVED_PREFIX=prefix)) as client:
        jobs = client.app.state.jobs  # type: ignore[attr-defined]
        jobs.register(Stage.PRE_CACHING, derived_stage())
        client.portal.call(jobs.start)
        client.portal.call(jobs.wait)
        page = client.get(f"/{prefix}/admin/docs").text
        assert f"url: '/{prefix}/admin/openapi.json'" in page
        assert f"window.location.origin + '/{prefix}/admin/docs/oauth2-redirect'" in page
        assert f'src="{SWAGGER_UI_URL}/swagger-ui-bundle.js"' in page
        assert f'href="{SWAGGER_UI_URL}/swagger-ui.css"' in page
        assert re.fullmatch(r"https://cdn\.jsdelivr\.net/npm/swagger-ui-dist@\d+\.\d+\.\d+", SWAGGER_UI_URL)
        assert "oauth2" in client.get(f"/{prefix}/admin/docs/oauth2-redirect").text
        assert not any(
            "docs" in path or "openapi" in path for path in client.get(f"/{prefix}/admin/openapi.json").json()["paths"]
        )
        for path in ["/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc", f"/{prefix}/admin/redoc"]:
            assert client.get(path).status_code == 404
    docs = {"/admin/openapi.json", "/admin/docs", "/admin/docs/oauth2-redirect"}
    assert docs <= {route.path for route in admin.router.routes}  # type: ignore[attr-defined]
    assert not any("redoc" in route.path for route in admin.router.routes)  # type: ignore[attr-defined]


def test_startup_starts_jvm(monkeypatch: pytest.MonkeyPatch, robot_jar: Path) -> None:
    """Check that startup starts the JVM with ROBOT_JAR and APP_JVM_MAX_HEAP, and makes the SPARQL client."""
    calls = []
    monkeypatch.setattr(browl.app.owlapi, "start_jvm", lambda *args: calls.append(args))
    app = create_app(make_settings(ROBOT_JAR=str(robot_jar), APP_JVM_MAX_HEAP="768m"))
    with TestClient(app):
        assert isinstance(app.state.sparql, FusekiClient)
        assert isinstance(app.state.fuseki_http, httpx2.AsyncClient)
        assert isinstance(app.state.jobs, JobRunner)
    assert calls == [(robot_jar, "768m")]


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
def test_startup_with_running_jvm(robot_jar: Path) -> None:
    """Check that the app starts when the JVM is already running, as in tests, whose client runs off the main thread."""
    with TestClient(create_app(make_settings(ROBOT_JAR=str(robot_jar)))) as client:
        assert client.get("/_browl/health").status_code == 200


def test_startup_fails_without_robot_jar(tmp_path: Path) -> None:
    """Check that the app doesn't start when ROBOT_JAR doesn't exist."""
    with (
        pytest.raises(FileNotFoundError, match="ROBOT_JAR"),
        TestClient(create_app(make_settings(ROBOT_JAR=str(tmp_path / "robot.jar")))),
    ):
        pass


def test_startup_opens_job_database(data_dir: Path) -> None:
    """Check that startup opens the job runner on its database in DATA_DIR, with no job yet."""
    app = make_app()
    with TestClient(app):
        assert app.state.jobs.state.site == Site.NOT_AVAILABLE
    assert (data_dir / "state.sqlite3").is_file()
