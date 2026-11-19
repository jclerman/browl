"""Browser tests: what only a browser running browl's scripts shows (the hash-IRI script, htmx, no-JS fallbacks).

They drive headless Chromium through Playwright against the app on a live server (`live_server`).
"""

import asyncio
import re
import urllib.parse
from collections.abc import Callable
from pathlib import Path

import pytest
import rdflib
from fastapi import FastAPI
from playwright.sync_api import ConsoleMessage, Page, Route, expect
from playwright.sync_api import Error as PlaywrightError
from rdflib.namespace import OWL, RDF

import browl.admin
from browl.derived import EntityType
from browl.jobs import JobContext, Stage, StageFunction
from browl.jobtypes import StageError
from tests.conftest import DATA
from tests.helpers import WAIT_S, LiveServer, derived_stage, make_app

pytestmark = [pytest.mark.browser, pytest.mark.usefixtures("data_dir")]

EX = "http://example.org"
SPECIAL = f"{EX}/vocab#f{{g}}"
"""A hash IRI whose fragment has characters RFC 3987 forbids, which Turtle can't hold."""

TITLE = "Routing tests"
"""The ontology's title, its home page's heading."""

HASH_ROUTE = re.compile(r"/_browl/hash\?")

NO_JS = pytest.mark.browser_context_args(java_script_enabled=False)
"""Run a test's `page` with JavaScript disabled; its parser then shows `<noscript>`'s content."""


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


def gate(event: asyncio.Event) -> StageFunction:
    """Return a job stage that waits for an event, set on the server's loop (`release`)."""

    async def stage(context: JobContext) -> None:
        await event.wait()

    return stage


async def release(event: asyncio.Event) -> None:
    """Set an event, on the server's loop."""
    event.set()


def start_endless_job(server: LiveServer) -> None:
    """Start a load job that stays in its loading stage until the app shuts down."""
    server.app.state.jobs.register(Stage.LOADING, gate(asyncio.Event()))
    server.call(server.app.state.jobs.start())


@pytest.fixture
def server(live_server: Callable[[FastAPI], LiveServer]) -> LiveServer:
    """Return a live server for an app whose entity URLs are all proxied, at /example.org/…, with the entities loaded.

    That's the manual setup `tests/data/routing.ttl` describes.
    """
    server = live_server(make_app(BASE_URL="http://localhost:8000/"))
    run_job(server)
    return server


def wait_until(page: Page, condition: Callable[[], bool], what: str) -> None:
    """Handle the page's events until a condition holds, failing after `WAIT_S`."""
    for _ in range(int(WAIT_S / 0.01)):
        if condition():
            return
        page.wait_for_timeout(10)
    raise AssertionError(f"timed out waiting for {what}")


def query_of(url: str) -> dict[str, list[str]]:
    """Return a URL's query parameters."""
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


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


@NO_JS
def test_hash_prefix_without_javascript(page: Page, server: LiveServer) -> None:
    """Check that, without JavaScript, a hash prefix's page says it needs it."""
    page.goto(f"{server.url}/example.org/vocab#Gamma")
    message = page.locator("main noscript p")  # Playwright's text matching skips <noscript>
    expect(message).to_be_visible()
    assert "needs JavaScript" in message.inner_text()


@pytest.mark.parametrize("how", ["assign", "click"])
def test_fragment_change_without_reload(page: Page, server: LiveServer, how: str) -> None:
    """Check that changing only the fragment, or following a link that does, shows the new entity without a reload."""
    page.goto(f"{server.url}/example.org/onto#Alpha")
    expect(page.locator("main h1")).to_have_text(f"{EX}/onto#Alpha")
    page.evaluate("window.marker = 1")
    if how == "assign":
        page.evaluate("location.hash = 'Beta'")
    else:  # the link sits outside <main>, which the script replaces
        page.locator("header").evaluate(
            """(header) => header.insertAdjacentHTML("beforeend", '<a href="#Beta">B</a>')"""
        )
        page.click("header a[href='#Beta']")
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
    assert query_of(request.value.url) == {"tab": ["usage"], "path": ["/example.org/vocab"], "fragment": ["Gamma"]}


@pytest.mark.parametrize("new", ["Beta", ""])
def test_stale_response_ignored(page: Page, server: LiveServer, new: str) -> None:
    """Check that a hash route response for an earlier fragment never replaces what a newer one shows (#87, S2).

    The responses are held until the test lets them through, so the order doesn't depend on timing.
    """
    held: dict[str, Route] = {}
    page.route(HASH_ROUTE, lambda route: held.__setitem__(query_of(route.request.url)["fragment"][0], route))
    page.goto(f"{server.url}/example.org/onto#Alpha")
    wait_until(page, lambda: "Alpha" in held, "the request for #Alpha")
    page.evaluate(f"location.hash = '{new}'")
    shown = f"{EX}/onto#Beta" if new else TITLE
    if new:
        wait_until(page, lambda: new in held, f"the request for #{new}")
        page.wait_for_timeout(100)  # time for the aborted request's handler to run
        expect(page.locator("main h1")).to_have_text(TITLE)  # the served page, not an error
        held[new].continue_()
    expect(page.locator("main h1")).to_have_text(shown)
    stale = held["Alpha"]
    try:
        stale.continue_()
    except PlaywrightError:
        pass  # the script aborted it, so nothing arrives
    else:
        if response := stale.request.response():
            response.finished()
        page.wait_for_timeout(100)  # time for the script to handle it
    expect(page.locator("main h1")).to_have_text(shown)


def test_loading_reloads(page: Page, server: LiveServer) -> None:
    """Check that a fragment change while a load job runs reloads the page, which then shows "loading" (#87, C1)."""
    page.goto(f"{server.url}/example.org/onto")
    page.evaluate("window.marker = 1")
    start_endless_job(server)
    page.evaluate("location.hash = 'Alpha'")
    expect(page.locator("main h1")).to_have_text("Loading the ontology")
    expect(page.locator('meta[http-equiv="refresh"]')).to_have_count(1)
    assert page.evaluate("window.marker") is None  # reloaded, not swapped in


def test_not_available_reloads(page: Page, server: LiveServer) -> None:
    """Check that a fragment change once a later load has failed reloads the page, which then shows "not available"."""

    async def fail(context: JobContext) -> None:
        raise StageError("boom")

    page.goto(f"{server.url}/example.org/onto")
    page.evaluate("window.marker = 1")
    server.app.state.jobs.register(Stage.LOADING, fail)
    server.call(server.app.state.jobs.start())
    server.call(server.app.state.jobs.wait())
    page.evaluate("location.hash = 'Alpha'")
    expect(page.locator("main h1")).to_have_text("Not available")
    assert page.evaluate("window.marker") is None


def test_fetch_error(page: Page, server: LiveServer) -> None:
    """Check that a hash route request that fails shows an error in place of the content."""
    page.route(HASH_ROUTE, lambda route: route.abort())
    page.goto(f"{server.url}/example.org/vocab#Gamma")
    expect(page.locator("main h1")).to_have_text("Error")
    expect(page).to_have_title("Error")


def test_iri_redirect_special_fragment(page: Page, server: LiveServer) -> None:
    """Check that the fallback route's redirect for a hash IRI with `{` `}` in its fragment lands on the entity."""
    page.goto(f"{server.url}/_browl/iri?iri={urllib.parse.quote(SPECIAL, safe='')}")
    expect(page.locator("main h1")).to_have_text(SPECIAL)


# htmx on the admin status (P-5, A-5)

STATUS = "/_browl/admin/status"
UPLOAD = "/_browl/admin/upload"
POLL_S = 0.25
"""How often the status polls in these tests, more often than the app's."""
REFRESH_S = 1
"""How often the no-JS pages refresh in these tests: a meta refresh reads only whole seconds."""


@pytest.fixture
def status_server(live_server: Callable[[FastAPI], LiveServer], monkeypatch: pytest.MonkeyPatch) -> LiveServer:
    """Return a live server for an app whose status polls every `POLL_S`, with no job yet."""
    monkeypatch.setattr(browl.admin, "STATUS_POLL_S", POLL_S)
    return live_server(make_app())


def test_status_polls_while_running(page: Page, status_server: LiveServer) -> None:
    """Check that, while a job runs, the status shows each stage without a page reload, and stops polling at the end.

    htmx does this under the Content-Security-Policy, breaking none of it.
    """
    jobs = status_server.app.state.jobs
    reasoned, loaded = asyncio.Event(), asyncio.Event()
    jobs.register(Stage.REASONING, gate(reasoned))
    jobs.register(Stage.LOADING, gate(loaded))
    jobs.register(Stage.PRE_CACHING, derived_stage())
    status_server.call(jobs.start())
    errors = csp_errors(page)
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
    page.wait_for_timeout(POLL_S * 3 * 1000)
    assert polls == []
    assert errors == []  # htmx works under the Content-Security-Policy


@NO_JS
@pytest.mark.parametrize("path", [STATUS, UPLOAD])
def test_status_refreshes_without_javascript(
    page: Page, status_server: LiveServer, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Check that, without JavaScript, the status and upload pages refresh themselves while a job runs."""
    monkeypatch.setattr(browl.admin, "STATUS_POLL_S", REFRESH_S)
    start_endless_job(status_server)
    page.goto(f"{status_server.url}{path}")
    page.wait_for_event("request", lambda request: request.is_navigation_request())


@NO_JS
@pytest.mark.parametrize("path", [STATUS, UPLOAD])
def test_no_refresh_once_finished(page: Page, status_server: LiveServer, path: str) -> None:
    """Check that, without JavaScript, the status and upload pages don't refresh once the job has ended."""
    status_server.app.state.jobs.register(Stage.PRE_CACHING, derived_stage())
    status_server.call(status_server.app.state.jobs.start())
    status_server.call(status_server.app.state.jobs.wait())
    page.goto(f"{status_server.url}{path}")
    expect(page.locator("#job-status")).to_contain_text("is ready")
    expect(page.locator('meta[http-equiv="refresh"]')).to_have_count(0)


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
    page.goto(f"{server.url}{UPLOAD}")
    page.set_input_files("#ontology", upload)
    with page.expect_response(lambda response: response.request.method == "POST") as response:
        page.click("button[type=submit]")
    assert response.value.status == status
    expect(page.get_by_role("alert")).to_have_text(re.compile(f"The upload was refused: {re.escape(reason)}"))


@pytest.mark.parametrize("origin", ["http://attacker.test", "http://127.0.0.1:59999"])
def test_upload_from_another_site_refused(
    page: Page, live_server: Callable[[FastAPI], LiveServer], origin: str
) -> None:
    """Check that the upload refuses a form submitted from another site's page, or another port's (CSRF).

    The check reads `Sec-Fetch-Site`, which only a browser sets; the page is the test's, served by `page.route`.
    """
    server = live_server(make_app())
    form = (
        f'<form method="post" action="{server.url}{UPLOAD}" enctype="multipart/form-data">'
        '<input name="reasoner" value="none"><button>Upload</button></form>'
    )
    page.route(f"{origin}/", lambda route: route.fulfill(body=form, content_type="text/html"))
    page.goto(f"{origin}/")
    with page.expect_response(lambda response: response.request.method == "POST") as response:
        page.click("button")
    assert response.value.status == 403
    assert server.call(server.app.state.jobs.current()).stage is None


# The page frame (P-14a, DESIGN.md §2.9)

TREES = ["Classes", "Object properties", "Data properties", "Annotation properties"]
WIDE = {"width": 1280, "height": 800}
NARROW = {"width": 375, "height": 700}
"""Viewports either side of the navigation bar's breakpoint (48rem)."""


def csp_errors(page: Page) -> list[str]:
    """Return a list that collects the page's console messages about its Content-Security-Policy."""
    errors: list[str] = []

    def collect(message: ConsoleMessage) -> None:
        if "Content Security Policy" in message.text:
            errors.append(message.text)

    page.on("console", collect)
    return errors


def test_navigation_bar_wide(page: Page, server: LiveServer) -> None:
    """Check that, on a wide screen, the tree links sit in the bar without a menu button, and the name links home."""
    page.set_viewport_size(WIDE)
    page.goto(f"{server.url}/example.org/thing")
    nav = page.get_by_role("navigation", name="Site")
    for label in TREES:
        expect(nav.get_by_role("link", name=label)).to_be_visible()
    expect(nav.locator("summary")).to_be_hidden()
    nav.get_by_role("link", name=f"browl · {TITLE}").click()
    expect(page.locator("main h1")).to_have_text(TITLE)


@NO_JS
def test_navigation_menu_narrow(page: Page, server: LiveServer) -> None:
    """Check that, on a narrow screen, the tree links are in a menu that opens without JavaScript."""
    page.set_viewport_size(NARROW)
    page.goto(f"{server.url}/example.org/thing")
    nav = page.get_by_role("navigation", name="Site")
    classes = nav.get_by_role("link", name="Classes")
    expect(classes).to_be_hidden()
    nav.get_by_text("Browse").click()
    for label in TREES:
        expect(nav.get_by_role("link", name=label)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_navigation_bar_sticky(page: Page, server: LiveServer) -> None:
    """Check that the navigation bar stays at the top when the page scrolls."""
    page.set_viewport_size(NARROW)
    page.goto(f"{server.url}/example.org/thing")
    page.evaluate("document.querySelector('main').style.minHeight = '5000px'; window.scrollTo(0, 2000)")
    assert page.evaluate("window.scrollY") > 0
    box = page.locator(".site-bar").bounding_box()
    assert box is not None
    assert (box["x"], box["y"], box["width"]) == (0, 0, NARROW["width"])


@pytest.mark.parametrize(("scheme", "background"), [("light", "rgb(250, 250, 247)"), ("dark", "rgb(29, 31, 33)")])
def test_theme_follows_browser(page: Page, server: LiveServer, scheme: str, background: str) -> None:
    """Check that the palette follows the browser's color scheme."""
    page.emulate_media(color_scheme=scheme)  # type: ignore[arg-type]
    page.goto(f"{server.url}/example.org/thing")
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == background


def test_hash_script_under_policy(page: Page, server: LiveServer) -> None:
    """Check that the hash-IRI script runs under the Content-Security-Policy, which an inline handler can't."""
    errors = csp_errors(page)
    page.goto(f"{server.url}/example.org/vocab#Gamma")
    expect(page.locator("main h1")).to_have_text(f"{EX}/vocab#Gamma")
    assert errors == []
    page.evaluate("document.body.insertAdjacentHTML('beforeend', '<img src=x onerror=\"window.ran = 1\">')")
    wait_until(page, lambda: bool(errors), "the policy to block the handler")
    assert page.evaluate("window.ran") is None


def test_hash_script_refuses_non_html(page: Page, server: LiveServer) -> None:
    """Check that the hash-IRI script shows an error, not the response, when the hash route's answer isn't HTML."""
    body = "<main><h1>Injected</h1></main>"
    page.route(HASH_ROUTE, lambda route: route.fulfill(status=200, content_type="text/plain", body=body))
    page.goto(f"{server.url}/example.org/vocab#Gamma")
    expect(page.locator("main h1")).to_have_text("Error")
