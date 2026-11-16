"""Tests for the FastAPI application and its health route."""

import tomllib
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

import browl
from browl.app import create_app
from browl.settings import Settings

UNREACHABLE = "http://127.0.0.1:1"  # nothing listens on port 1, so connections are refused at once


def make_settings(**overrides: object) -> Settings:
    """Return settings for tests: authentication off, and any overrides."""
    return Settings.model_validate({"BASE_URL": "http://example.org/", "AUTH_DISABLED": True} | overrides)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Yield a test client for an app with default settings, its lifespan running."""
    with TestClient(create_app(make_settings())) as test_client:
        yield test_client


def answer_pings(client: TestClient, response: httpx2.Response | Exception) -> list[httpx2.Request]:
    """Make the app's Fuseki client get this response, or raise this exception, and return the requests it sends."""
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if isinstance(response, Exception):
            raise response
        return response

    client.app.state.fuseki = httpx2.AsyncClient(  # type: ignore[attr-defined]
        base_url="http://fuseki:3030", transport=httpx2.MockTransport(handler)
    )
    return requests


def test_health_fuseki_up(client: TestClient) -> None:
    """Check that the health route pings Fuseki and reports it up when it answers."""
    requests = answer_pings(client, httpx2.Response(200))
    response = client.get("/_browl/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "fuseki": "up"}
    assert [str(request.url) for request in requests] == ["http://fuseki:3030/$/ping"]


@pytest.mark.parametrize("answer", [httpx2.Response(503), httpx2.ReadTimeout("timed out")])
def test_health_fuseki_down(client: TestClient, answer: httpx2.Response | Exception) -> None:
    """Check that the app stays healthy, reporting Fuseki down, when Fuseki fails or doesn't answer."""
    answer_pings(client, answer)
    response = client.get("/_browl/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "fuseki": "down"}


def test_health_fuseki_unreachable() -> None:
    """Check that the app's own Fuseki client, built from FUSEKI_URL, reports a refused connection as down."""
    with TestClient(create_app(make_settings(FUSEKI_URL=UNREACHABLE))) as client:
        assert client.get("/_browl/health").json() == {"status": "ok", "fuseki": "down"}


def test_reserved_prefix() -> None:
    """Check that the service's own routes live under RESERVED_PREFIX."""
    with TestClient(create_app(make_settings(RESERVED_PREFIX="_site"))) as client:
        answer_pings(client, httpx2.Response(200))
        assert client.get("/_site/health").status_code == 200
        assert client.get("/_browl/health").status_code == 404


def test_create_app_loads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that create_app reads the settings from the environment when none are given."""
    monkeypatch.setenv("BASE_URL", "http://example.org/")
    monkeypatch.setenv("AUTH_DISABLED", "true")
    assert create_app().state.settings.base_url == make_settings().base_url


def test_api_docs_show_version(client: TestClient) -> None:
    """Check that the OpenAPI schema, which the Swagger page shows, has browl's version from pyproject.toml."""
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert client.get("/openapi.json").json()["info"]["version"] == browl.__version__ == pyproject["project"]["version"]
