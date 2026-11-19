"""Browser tests: what only a browser running browl's scripts shows (the hash-IRI script, htmx, no-JS fallbacks).

They drive headless Chromium through Playwright against the app on a live server (`live_server`).
"""

import asyncio
import re
import urllib.parse
from collections.abc import Callable, Coroutine, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
import rdflib
from fastapi import FastAPI
from playwright.sync_api import Browser, Page, Route, expect
from playwright.sync_api import Error as PlaywrightError
from rdflib.namespace import OWL, RDF

import browl.admin
from browl.derived import EntityType
from browl.jobs import Stage
from tests.conftest import DATA
from tests.helpers import WAIT_S, LiveServer, derived_stage, make_app

pytestmark = [pytest.mark.browser, pytest.mark.usefixtures("data_dir")]

EX = "http://example.org"
SPECIAL = f"{EX}/vocab#f{{g}}"
"""A hash IRI whose fragment has characters RFC 3987 forbids, which Turtle can't hold."""

TITLE = "Routing tests"
"""The ontology's title, its home page's heading."""

HASH_ROUTE = re.compile(r"/_browl/hash\?")


def routing_entities() -> tuple[str, list[tuple[str, EntityType]]]:
    """Return `tests/data/routing.ttl`'s ontology IRI and classes, and `SPECIAL`."""
    graph = rdflib.Graph().parse(DATA / "routing.ttl")
    ontology = str(next(graph.subjects(RDF.type, OWL.Ontology)))
    classes = sorted(str(iri) for iri in graph.subjects(RDF.type, OWL.Class))
    return ontology, [(iri, EntityType.CLASS) for iri in [*classes, SPECIAL]]


def run_job(server: LiveServer) -> None:
    """Run a load job to its end, its derived database holding `routing_entities`."""
    ontology, entities = routing_entities()
    server.app.state.jobs.register(Stage.PRE_CACHING, derived_stage(entities, ontology, title=TITLE))
    server.call(server.app.state.jobs.start())
    server.call(server.app.state.jobs.wait())


async def wait_forever(context: object) -> None:
    """Wait until the app shuts down: a job stage that never ends."""
    await asyncio.Event().wait()


@pytest.fixture
def server(live_server: Callable[[FastAPI], LiveServer]) -> LiveServer:
    """Return a live server for an app whose entity URLs are all proxied, at /example.org/…, with the entities loaded.

    That's the manual setup `tests/data/routing.ttl` describes.
    """
    server = live_server(make_app(BASE_URL="http://localhost:8000/"))
    run_job(server)
    return server


@pytest.fixture
def no_js_page(browser: Browser) -> Iterator[Page]:
    """Yield a page in a browser context with JavaScript disabled, whose parser shows `<noscript>`'s content."""
    context = browser.new_context(java_script_enabled=False)
    try:
        yield context.new_page()
    finally:
        context.close()


def wait_until(page: Page, condition: Callable[[], bool]) -> None:
    """Handle the page's events until a condition holds, failing after `WAIT_S`."""
    for _ in range(int(WAIT_S / 0.01)):
        if condition():
            return
        page.wait_for_timeout(10)
    raise AssertionError("timed out")


def fragment_of(url: str) -> str:
    """Return the `fragment` parameter of a hash route URL."""
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("fragment", [""])[0]


# The hash-IRI script (DESIGN.md §2.3, U-13, U-14)


def test_hash_prefix_shows_entity(page: Page, server: LiveServer) -> None:
    """Check that a hash prefix's page with a fragment shows that entity, and takes its title."""
    page.goto(f"{server.url}/example.org/vocab#Gamma")
    expect(page.locator("main h1")).to_have_text(f"{EX}/vocab#Gamma")
    expect(page).to_have_title(f"{EX}/vocab#Gamma · browl")


@pytest.mark.parametrize("fragment", ["", "#Nope"])
def test_hash_prefix_not_found(page: Page, server: LiveServer, fragment: str) -> None:
    """Check that a hash prefix's page shows "not found" without a fragment, or with one no entity has."""
    page.goto(f"{server.url}/example.org/vocab{fragment}")
    expect(page.locator("main h1")).to_have_text("Not found")
    expect(page).to_have_title("Not found · browl")


def test_hash_prefix_without_javascript(no_js_page: Page, server: LiveServer) -> None:
    """Check that, without JavaScript, a hash prefix's page says it needs it."""
    no_js_page.goto(f"{server.url}/example.org/vocab#Gamma")
    message = no_js_page.locator("main noscript p")  # Playwright's text matching skips <noscript>
    expect(message).to_be_visible()
    assert "needs JavaScript" in message.inner_text()


def test_fragment_change_without_reload(page: Page, server: LiveServer) -> None:
    """Check that changing only the fragment shows the new entity without loading the page again."""
    page.goto(f"{server.url}/example.org/onto#Alpha")
    expect(page.locator("main h1")).to_have_text(f"{EX}/onto#Alpha")
    page.evaluate("window.marker = 1")
    page.evaluate("location.hash = 'Beta'")
    expect(page.locator("main h1")).to_have_text(f"{EX}/onto#Beta")
    assert page.evaluate("window.marker") == 1


def test_entity_and_hash_prefix(page: Page, server: LiveServer) -> None:
    """Check that a path that's an entity's and a hash prefix shows the entity, a fragment's, then its own again."""
    page.goto(f"{server.url}/example.org/thing")
    expect(page.locator("main h1")).to_have_text(f"{EX}/thing")
    page.evaluate("location.hash = 'Delta'")
    expect(page.locator("main h1")).to_have_text(f"{EX}/thing#Delta")
    expect(page).to_have_title(f"{EX}/thing#Delta · browl")
    page.evaluate("location.hash = ''")
    expect(page.locator("main h1")).to_have_text(f"{EX}/thing")
    expect(page).to_have_title(f"{EX}/thing · browl")


def test_shared_hash_entity(page: Page, server: LiveServer) -> None:
    """Check that a fragment that the http and https forms of an IRI share shows the choose page, linking both."""
    page.goto(f"{server.url}/example.org/vocab#Epsilon")
    expect(page.locator("main h1")).to_have_text("Choose an entity")
    expect(page.locator("main li code")).to_have_text([f"{EX}/vocab#Epsilon", "https://example.org/vocab#Epsilon"])


def test_query_passed_on(page: Page, server: LiveServer) -> None:
    """Check that the script passes the page's own query, such as the tab, on to the hash route."""
    with page.expect_request(HASH_ROUTE) as request:
        page.goto(f"{server.url}/example.org/vocab?tab=usage#Gamma")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.value.url).query)
    assert query == {"tab": ["usage"], "path": ["/example.org/vocab"], "fragment": ["Gamma"]}


@pytest.mark.parametrize("new", ["Beta", ""])
def test_stale_response_ignored(page: Page, server: LiveServer, new: str) -> None:
    """Check that a hash route response for an earlier fragment never replaces what a newer one shows (#87, S2).

    The responses are held until the test lets them through, so the order doesn't depend on timing.
    """
    held: dict[str, Route] = {}
    page.route(HASH_ROUTE, lambda route: held.__setitem__(fragment_of(route.request.url), route))
    page.goto(f"{server.url}/example.org/onto#Alpha")
    wait_until(page, lambda: "Alpha" in held)
    page.evaluate(f"location.hash = '{new}'")
    if new:
        wait_until(page, lambda: new in held)
        page.evaluate("new Promise((resolve) => setTimeout(resolve, 100))")
        expect(page.locator("main h1")).to_have_text(TITLE)  # the served page, not an error
        held[new].continue_()
    expect(page.locator("main h1")).to_have_text(f"{EX}/onto#Beta" if new else TITLE)
    with suppress(PlaywrightError):  # the script aborted it
        held["Alpha"].continue_()
    page.evaluate("new Promise((resolve) => setTimeout(resolve, 500))")
    expect(page.locator("main h1")).to_have_text(f"{EX}/onto#Beta" if new else TITLE)


def test_loading_reloads(page: Page, server: LiveServer) -> None:
    """Check that a fragment change while a load job runs reloads the page, which then shows "loading" (#87, C1)."""
    page.goto(f"{server.url}/example.org/onto")
    server.app.state.jobs.register(Stage.LOADING, wait_forever)
    server.call(server.app.state.jobs.start())
    with page.expect_navigation(timeout=WAIT_S * 1000):
        page.evaluate("location.hash = 'Alpha'")
    expect(page.locator("main h1")).to_have_text("Loading the ontology")
    expect(page.locator('meta[http-equiv="refresh"]')).to_have_count(1)


def test_iri_redirect_special_fragment(page: Page, server: LiveServer) -> None:
    """Check that the fallback route's redirect for a hash IRI with `{` `}` in its fragment lands on the entity."""
    page.goto(f"{server.url}/_browl/iri?iri={urllib.parse.quote(SPECIAL, safe='')}")
    expect(page.locator("main h1")).to_have_text(SPECIAL)


# htmx on the admin status (P-5, A-5)

STATUS = "/_browl/admin/status"
POLL_S = 0.5
"""The status's poll interval in these tests, shorter than the app's."""


@pytest.fixture
def status_server(live_server: Callable[[FastAPI], LiveServer], monkeypatch: pytest.MonkeyPatch) -> LiveServer:
    """Return a live server for an app whose status polls every `POLL_S`, with no job yet."""
    monkeypatch.setattr(browl.admin, "STATUS_POLL_S", POLL_S)
    return live_server(make_app())


def gate(event: asyncio.Event) -> Callable[[object], Coroutine[Any, Any, None]]:
    """Return a job stage that waits for an event, set on the server's loop (`release`)."""

    async def stage(context: object) -> None:
        await event.wait()

    return stage


async def release(event: asyncio.Event) -> None:
    """Set an event, on the server's loop."""
    event.set()


def test_status_polls_while_running(page: Page, status_server: LiveServer) -> None:
    """Check that, while a job runs, the status shows each stage without a page reload, and stops polling at the end."""
    jobs = status_server.app.state.jobs
    reasoned, loaded = asyncio.Event(), asyncio.Event()
    jobs.register(Stage.REASONING, gate(reasoned))
    jobs.register(Stage.LOADING, gate(loaded))
    jobs.register(Stage.PRE_CACHING, derived_stage())
    status_server.call(jobs.start())
    page.goto(f"{status_server.url}{STATUS}")
    current = page.locator("#job-status li[aria-current]")
    expect(current).to_have_text("reasoning")
    page.evaluate("window.marker = 1")
    status_server.call(release(reasoned))
    expect(current).to_have_text("loading")
    status_server.call(release(loaded))
    expect(current).to_have_text("ready")
    assert page.evaluate("window.marker") == 1
    polls: list[str] = []
    page.on("request", lambda request: polls.append(request.url))
    page.wait_for_timeout(POLL_S * 4 * 1000)
    assert polls == []


@pytest.mark.parametrize("path", [STATUS, "/_browl/admin/upload"])
def test_status_refreshes_without_javascript(no_js_page: Page, status_server: LiveServer, path: str) -> None:
    """Check that, without JavaScript, the status and upload pages refresh themselves while a job runs."""
    status_server.app.state.jobs.register(Stage.LOADING, wait_forever)
    status_server.call(status_server.app.state.jobs.start())
    no_js_page.goto(f"{status_server.url}{path}")
    with no_js_page.expect_request(lambda request: request.is_navigation_request(), timeout=WAIT_S * 1000):
        pass


# Upload refusal (#67; PR #65's review, C3)

UPLOAD_MB = 64
"""The upload's size: far more than the socket buffers hold, so the browser is still sending when it's refused."""


@pytest.mark.parametrize(
    ("name", "limit", "status", "reason"),
    [
        ("ontology.owl", 1000, 413, "the upload is larger than the limit of 1,000 bytes"),
        ("ontology.ttl", None, 400, "the file's name says Turtle, but its content looks like RDF/XML"),
    ],
)
def test_upload_refused_early(
    page: Page,
    live_server: Callable[[FastAPI], LiveServer],
    tmp_path: Path,
    name: str,
    limit: int | None,
    status: int,
    reason: str,
) -> None:
    """Check that a browser shows the refusal page, not a connection reset, for an upload refused while it's sending.

    The server answers once it has read the first bytes, and closes the connection without reading the rest.
    """
    server = live_server(make_app(**({} if limit is None else {"MAX_UPLOAD_BYTES": limit})))
    upload = tmp_path / name
    with upload.open("wb") as file:
        file.write(b'<?xml version="1.0"?>\n<rdf:RDF ')
        file.truncate(UPLOAD_MB * 2**20)
    page.goto(f"{server.url}/_browl/admin/upload")
    page.set_input_files("#ontology", upload)
    with page.expect_response(lambda response: response.request.method == "POST") as response:
        page.click("button[type=submit]")
    assert response.value.status == status
    expect(page.get_by_role("alert")).to_have_text(re.compile(f"The upload was refused: {re.escape(reason)}"))
