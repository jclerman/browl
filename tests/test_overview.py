"""Tests for the Overview tab's axioms panel when its axioms can't be read: the page still works, the log has why."""

import logging
from collections.abc import Iterator

import httpx2
import pytest
from fastapi.testclient import TestClient

import browl.overview
from browl.derived import EntityType
from browl.jobs import Stage
from tests.helpers import derived_stage, make_app

pytestmark = pytest.mark.usefixtures("data_dir")

IRI = "http://example.org/onto#A"
MESSAGE = "The axioms can't be shown just now."


def fuseki_error(request: httpx2.Request) -> httpx2.Response:
    """Answer every query as Fuseki does one that fails."""
    return httpx2.Response(500, text="Error 500: Server Error")


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Yield a test client for an app whose Fuseki fails every query, with the entity loaded."""
    app = make_app(httpx2.MockTransport(fuseki_error))
    with TestClient(app) as test_client:
        app.state.jobs.register(Stage.PRE_CACHING, derived_stage([(IRI, EntityType.CLASS)]))
        test_client.portal.call(app.state.jobs.start)
        test_client.portal.call(app.state.jobs.wait)
        yield test_client


def test_query_fails(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """Check that a failed query fails only the axioms panel: the page shows a short message, and the log the error."""
    with caplog.at_level(logging.ERROR, logger="browl.overview"):
        page = client.get("/_browl/hash", params={"path": "/onto", "fragment": "A"})
    assert page.status_code == 200
    assert MESSAGE in page.text
    assert "Error 500" not in page.text
    [record] = caplog.records
    assert record.getMessage() == f"Couldn't show the axioms of {IRI}"
    assert "Error 500: Server Error" in caplog.text


def test_usage_tab_untouched(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """Check that the Usage tab doesn't fetch the axioms."""
    page = client.get("/_browl/hash", params={"path": "/onto", "fragment": "A", "tab": "usage"})
    assert page.status_code == 200
    assert MESSAGE not in page.text
    assert not [record for record in caplog.records if record.name == "browl.overview"]


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
def test_fragment_error(client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Check that a fragment the OWLAPI can't parse fails only the axioms panel, and its parser's error is logged."""

    async def fetched(*args: object) -> bytes:
        return b"<http://example.org/onto#A> <http://exa\n"

    monkeypatch.setattr(browl.overview, "fetch_fragment", fetched)
    with caplog.at_level(logging.ERROR, logger="browl.overview"):
        page = client.get("/_browl/hash", params={"path": "/onto", "fragment": "A", "panel": "1"})
    assert page.status_code == 200
    assert MESSAGE in page.text
    assert "parse" not in page.text
    [record] = caplog.records
    assert record.exc_info is not None
    assert "FragmentError: the OWLAPI couldn't parse the fragment" in caplog.text
    assert "Detailed logs:" in caplog.text  # the Java exception's, with its parsers' errors
    assert "Stack trace:" in caplog.text
