"""Tests for the FastAPI application and its health route."""

import asyncio
import tomllib
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

import browl
import browl.app
from browl.app import create_app
from browl.settings import Settings

UNREACHABLE = "http://127.0.0.1:1"  # nothing listens on port 1, so connections are refused at once


def make_settings(**overrides: object) -> Settings:
    """Return settings for tests: authentication off, and any overrides."""
    return Settings.model_validate({"BASE_URL": "http://example.org/", "AUTH_DISABLED": True} | overrides)


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

    app = create_app(make_settings(**overrides), fuseki_transport=httpx2.MockTransport(handler))
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 200
    return response.json(), requests


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Yield a test client for an app with default settings, its lifespan running."""
    with TestClient(create_app(make_settings())) as test_client:
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
    with TestClient(create_app(make_settings(FUSEKI_URL=UNREACHABLE))) as client:
        assert client.get("/_browl/health").json() == {"status": "ok", "fuseki": "down"}


def test_reserved_prefix() -> None:
    """Check that the service's own routes live under RESERVED_PREFIX."""
    body, _ = check_health(httpx2.Response(200), path="/_site/health", RESERVED_PREFIX="_site")
    assert body["fuseki"] == "up"
    with TestClient(create_app(make_settings(RESERVED_PREFIX="_site"))) as client:
        assert client.get("/_browl/health").status_code == 404


def test_create_app_loads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that create_app reads the settings from the environment when none are given."""
    monkeypatch.setenv("BASE_URL", "http://example.org/")
    monkeypatch.setenv("AUTH_DISABLED", "true")
    assert create_app().state.settings.base_url == make_settings().base_url


def test_api_docs_show_title_and_version(client: TestClient) -> None:
    """Check that the OpenAPI schema, which the Swagger page shows, has browl's title and its pyproject.toml version."""
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    info = client.get("/openapi.json").json()["info"]
    assert info["title"] == "OWL Ontology Browser"
    assert info["version"] == browl.__version__ == pyproject["project"]["version"]
