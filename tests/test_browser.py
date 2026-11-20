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
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import FloatRect, Locator, Page, Route, expect
from rdflib.namespace import OWL, RDF

import browl.admin
import browl.pages
from browl.derived import EntityType
from browl.jobs import JobContext, Stage, StageFunction
from browl.jobtypes import StageError
from tests.conftest import DATA
from tests.helpers import WAIT_S, LiveServer, derived_stage, iso, make_app

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
    expect(page).to_have_title(f"{iso(EX + '/vocab#Gamma')} · {iso('Routing tests')}")


@pytest.mark.parametrize("fragment", ["", "#Nope"])
def test_hash_prefix_not_found(page: Page, server: LiveServer, fragment: str) -> None:
    """Check that a hash prefix's page shows "not found" without a fragment, or with one no entity has."""
    page.goto(f"{server.url}/example.org/vocab{fragment}")
    expect(page.locator("main h1")).to_have_text("Not found")
    expect(page).to_have_title(f"Not found · {iso('Routing tests')}")


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
    expect(page).to_have_title(f"{iso(EX + '/thing#Delta')} · {iso('Routing tests')}")
    page.evaluate("location.hash = ''")
    expect(page.locator("main h1")).to_have_text(f"{EX}/thing")
    expect(page).to_have_title(f"{iso(EX + '/thing')} · {iso('Routing tests')}")


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
    """Check that, while a job runs, the status shows each stage without a page reload, and stops polling at the end."""
    jobs = status_server.app.state.jobs
    reasoned, loaded = asyncio.Event(), asyncio.Event()
    jobs.register(Stage.REASONING, gate(reasoned))
    jobs.register(Stage.LOADING, gate(loaded))
    jobs.register(Stage.PRE_CACHING, derived_stage())
    status_server.call(jobs.start())
    watch_policy(page)
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
    assert violations(page) == []  # htmx works under the Content-Security-Policy


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

TREES = [label for _, label in browl.pages.TREES]
WIDE = {"width": 1280, "height": 800}
NARROW = {"width": 375, "height": 700}
"""Viewports either side of the navigation bar's breakpoint (48rem)."""


def watch_policy(page: Page) -> None:
    """Make the page record its Content-Security-Policy violations, for `violations`; call before loading it."""
    page.add_init_script(
        "window.violations = []; document.addEventListener('securitypolicyviolation',"
        " (event) => window.violations.push(event.violatedDirective));"
    )


def violations(page: Page) -> list[str]:
    """Return the directives the page has violated since it loaded."""
    result: list[str] = page.evaluate("window.violations")
    return result


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
    """Check that the hash-IRI script runs under the policy, which blocks an inline handler in the content it moves."""
    watch_policy(page)
    page.goto(f"{server.url}/example.org/vocab#Gamma")
    expect(page.locator("main h1")).to_have_text(f"{EX}/vocab#Gamma")
    assert violations(page) == []
    body = '<title>Delta</title><main><h1>Delta</h1><img src="x" onerror="window.ran = 1"></main>'
    page.route(HASH_ROUTE, lambda route: route.fulfill(status=200, content_type="text/html", body=body))
    page.evaluate("location.hash = 'Delta'")
    expect(page.locator("main h1")).to_have_text("Delta")
    wait_until(page, lambda: "script-src-attr" in violations(page), "the policy to block the handler")
    assert page.evaluate("window.ran") is None


@pytest.mark.parametrize("content_type", ["text/plain", "application/xhtml+xml", None])
def test_hash_script_refuses_non_html(page: Page, server: LiveServer, content_type: str | None) -> None:
    """Check that the hash-IRI script shows an error, not the response, when the hash route's answer isn't HTML."""
    body = b"<main><h1>Injected</h1></main>"
    headers = {} if content_type is None else {"Content-Type": content_type}
    page.route(HASH_ROUTE, lambda route: route.fulfill(status=200, headers=headers, body=body))
    page.goto(f"{server.url}/example.org/vocab#Gamma")
    expect(page.locator("main h1")).to_have_text("Error")


def test_narrow_bar_one_row(page: Page, server: LiveServer) -> None:
    """Check that, on a narrow screen, the name and menu button share a row, and the menu opens as a panel under it."""
    page.set_viewport_size(NARROW)
    page.goto(f"{server.url}/example.org/thing")
    bar, name, button = page.locator(".site-bar"), page.locator(".site-name"), page.locator(".site-menu summary")
    height = box(bar)["height"]
    assert abs(box(name)["y"] + box(name)["height"] / 2 - box(button)["y"] - box(button)["height"] / 2) < 2
    main_y = box(page.locator("main"))["y"]
    button.click()
    panel = box(page.locator(".site-menu ul"))
    assert (box(bar)["height"], box(page.locator("main"))["y"]) == (height, main_y)
    assert panel["y"] >= box(button)["y"] + box(button)["height"]
    assert abs(panel["x"] + panel["width"] - box(button)["x"] - box(button)["width"]) < 2


def test_fonts_and_measure(page: Page, server: LiveServer) -> None:
    """Check the system font for text, a monospace font for IRIs, and that paragraphs are capped at a readable width."""
    page.set_viewport_size(WIDE)
    page.goto(f"{server.url}/example.org/thing")
    assert page.evaluate("getComputedStyle(document.body).fontFamily").startswith("system-ui")
    assert page.evaluate("getComputedStyle(document.querySelector('main code')).fontFamily").startswith("ui-monospace")
    page.evaluate(
        "document.querySelector('main').insertAdjacentHTML('beforeend', '<p>' + 'word '.repeat(500) + '</p>')"
    )
    width = page.evaluate("document.querySelector('main p:last-child').getBoundingClientRect().width")
    char = page.evaluate("parseFloat(getComputedStyle(document.body).fontSize)")
    assert 35 * char < width < 60 * char  # 75ch is about 47em in the system font; uncapped, it would be about 78em


def box(locator: Locator) -> FloatRect:
    """Return a visible element's bounding box."""
    result = locator.bounding_box()
    assert result is not None
    return result


# Page tabs (P-14b, DESIGN.md §2.9, U-28, U-17)

PANEL = re.compile(r"[?&]panel=1(&|$)")
OVERVIEW_TEXT = "Logical axioms"
USAGE_TEXT = "Where other entities use this one"


def tab(page: Page, name: str) -> Locator:
    """Return the page tab with a name."""
    return page.get_by_role("tablist").get_by_role("tab", name=name)


def expect_selected(page: Page, name: str) -> None:
    """Check that a tab is the selected one, and labels the tab panel."""
    for other in page.get_by_role("tablist").get_by_role("tab").all():
        expect(other).to_have_attribute("aria-selected", "true" if other.inner_text() == name else "false")
    expect(page.get_by_role("tabpanel")).to_have_attribute("aria-labelledby", tab(page, name).get_attribute("id") or "")


@NO_JS
def test_tabs_without_javascript(page: Page, server: LiveServer) -> None:
    """Check that, without JavaScript, the tabs are links to their pages, which the server renders."""
    page.goto(f"{server.url}/example.org/plain")
    expect_selected(page, "Overview")
    tab(page, "Usage").click()
    expect(page).to_have_url(f"{server.url}/example.org/plain?tab=usage")
    expect_selected(page, "Usage")
    expect(page.get_by_role("tabpanel")).to_contain_text(USAGE_TEXT)
    tab(page, "Overview").click()
    expect(page).to_have_url(f"{server.url}/example.org/plain")
    expect(page.get_by_role("tabpanel")).to_contain_text(OVERVIEW_TEXT)


def test_tab_swaps_panel(page: Page, server: LiveServer) -> None:
    """Check that, with htmx, a tab fetches only its panel, swaps it in, and replaces the URL, without a navigation."""
    page.goto(f"{server.url}/example.org/plain")
    page.evaluate("window.marker = 1")
    length = page.evaluate("history.length")
    with page.expect_request(PANEL) as request:
        tab(page, "Usage").click()
    assert query_of(request.value.url) == {"tab": ["usage"], "panel": ["1"]}
    expect(page.get_by_role("tabpanel")).to_contain_text(USAGE_TEXT)
    expect_selected(page, "Usage")
    expect(page).to_have_url(f"{server.url}/example.org/plain?tab=usage")
    assert page.evaluate("[window.marker, history.length]") == [1, length]
    expect(page.locator("main h1")).to_have_count(1)  # the panel alone, not a page inside the page
    tab(page, "Overview").click()
    expect(page.get_by_role("tabpanel")).to_contain_text(OVERVIEW_TEXT)
    expect(page).to_have_url(f"{server.url}/example.org/plain")


def record_panels(page: Page) -> list[str]:
    """Return a list that collects the URLs of the page's panel requests, in order."""
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url) if PANEL.search(request.url) else None)
    return requests


def test_selected_tab_not_fetched(page: Page, server: LiveServer) -> None:
    """Check that the selected tab, as served or as swapped in, doesn't fetch its panel again.

    Each click on the other tab makes a request, so one from the click before it would be recorded first.
    """
    page.goto(f"{server.url}/example.org/plain")
    requests = record_panels(page)
    tab(page, "Overview").click()
    with page.expect_request(PANEL):
        tab(page, "Usage").click()
    expect_selected(page, "Usage")
    tab(page, "Usage").click()
    with page.expect_request(PANEL):
        tab(page, "Overview").click()
    expect_selected(page, "Overview")
    assert [query_of(url).get("tab") for url in requests] == [["usage"], None]


def test_first_click_focuses_tab(page: Page, server: LiveServer) -> None:
    """Check that a tab opened by a first click keeps the focus, in the Tab sequence (#92, S3)."""
    page.goto(f"{server.url}/example.org/plain")
    tab(page, "Usage").click()
    expect_selected(page, "Usage")
    expect(tab(page, "Usage")).to_be_focused()
    expect(tab(page, "Usage")).to_have_attribute("tabindex", "0")


def test_htmx_eval_off(page: Page, server: LiveServer) -> None:
    """Check that htmx's eval and script-tag features are off (#92, C2)."""
    page.goto(f"{server.url}/example.org/plain")
    assert page.evaluate("[htmx.config.allowEval, htmx.config.allowScriptTags]") == [False, False]


def test_tab_click_while_loading(page: Page, server: LiveServer) -> None:
    """Check that clicking back to the selected tab while another's panel loads cancels that load, and stays put."""
    held: list[Route] = []
    page.route(re.compile(r"tab=usage&panel=1"), lambda route: held.append(route))
    page.goto(f"{server.url}/example.org/plain")
    page.evaluate("window.marker = 1")
    tab(page, "Usage").click()
    wait_until(page, lambda: bool(held), "the request for Usage's panel")
    tab(page, "Overview").click()
    try:
        held[0].continue_()
    except PlaywrightError:
        pass  # htmx aborted it, so nothing arrives
    else:
        if response := held[0].request.response():
            response.finished()
    page.wait_for_timeout(100)  # time for htmx to handle a response, if one arrived
    expect_selected(page, "Overview")
    expect(page.get_by_role("tabpanel")).to_contain_text(OVERVIEW_TEXT)
    expect(page).to_have_url(f"{server.url}/example.org/plain")
    assert page.evaluate("window.marker") == 1


def test_tab_keyboard(page: Page, server: LiveServer) -> None:
    """Check the WAI-ARIA keyboard support, with manual activation: arrows, Home and End move, Enter or Space opens.

    Only the selected tab is in the Tab sequence, and focus entering the list lands on it.
    """
    page.goto(f"{server.url}/example.org/plain?tab=usage")
    page.locator(".entity-iri summary").focus()  # the last stop before the tabs
    page.keyboard.press("Tab")
    expect(tab(page, "Usage")).to_be_focused()
    requests = record_panels(page)
    for key, name in [("ArrowRight", "Overview"), ("ArrowLeft", "Usage"), ("Home", "Overview"), ("End", "Usage")]:
        page.keyboard.press(key)
        expect(tab(page, name)).to_be_focused()
    expect_selected(page, "Usage")  # moving focus doesn't open a tab
    page.keyboard.press("Home")
    with page.expect_request(PANEL):
        page.keyboard.press("Enter")
    expect_selected(page, "Overview")
    assert len(requests) == 1  # Enter's; the arrow keys, Home and End fetched nothing
    expect(page).to_have_url(f"{server.url}/example.org/plain")
    page.evaluate("window.addEventListener('keydown', (event) => { window.spaceKept = !event.defaultPrevented; })")
    page.keyboard.press("End")
    page.keyboard.press(" ")
    expect_selected(page, "Usage")
    assert page.evaluate("window.spaceKept") is False  # Space doesn't also scroll the page
    expect(tab(page, "Usage")).to_have_attribute("tabindex", "0")
    expect(tab(page, "Overview")).to_have_attribute("tabindex", "-1")
    page.keyboard.press("Tab")
    expect(page.get_by_role("tabpanel")).to_be_focused()


def test_tab_modifier_click_left_to_browser(page: Page, server: LiveServer) -> None:
    """Check that a tab clicked with a modifier key opens its page in a new browser tab, and leaves this one alone."""
    page.goto(f"{server.url}/example.org/plain")
    with page.context.expect_page() as opened:
        tab(page, "Usage").click(modifiers=["ControlOrMeta"])
    opened.value.wait_for_load_state()
    assert opened.value.url == f"{server.url}/example.org/plain?tab=usage"
    expect_selected(page, "Overview")
    expect(page).to_have_url(f"{server.url}/example.org/plain")


@pytest.mark.parametrize("failure", ["status", "network"])
def test_tab_panel_failure_loads_page(page: Page, server: LiveServer, failure: str) -> None:
    """Check that a tab whose panel can't be fetched loads its page instead."""
    if failure == "status":
        page.route(PANEL, lambda route: route.fulfill(status=503, content_type="text/html", body="<p>Loading</p>"))
    else:
        page.route(PANEL, lambda route: route.abort())
    page.goto(f"{server.url}/example.org/plain")
    tab(page, "Usage").click()
    expect(page).to_have_url(f"{server.url}/example.org/plain?tab=usage")
    expect_selected(page, "Usage")
    expect(page.get_by_role("tabpanel")).to_contain_text(USAGE_TEXT)


def test_hash_entity_tabs(page: Page, server: LiveServer) -> None:
    """Check that a hash entity's tabs fetch their panels from the hash route, and keep the query before its fragment.

    The page then reloads on the tab its URL names.
    """
    page.goto(f"{server.url}/example.org/vocab#Gamma")
    expect(page.locator("main h1")).to_have_text(f"{EX}/vocab#Gamma")
    with page.expect_request(PANEL) as request:
        tab(page, "Usage").click()
    assert "/_browl/hash?" in request.value.url
    assert query_of(request.value.url) == {
        "path": ["/example.org/vocab"],
        "fragment": ["Gamma"],
        "tab": ["usage"],
        "panel": ["1"],
    }
    expect(page.get_by_role("tabpanel")).to_contain_text(USAGE_TEXT)
    expect(page).to_have_url(f"{server.url}/example.org/vocab?tab=usage#Gamma")
    page.reload()
    expect(page.locator("main h1")).to_have_text(f"{EX}/vocab#Gamma")
    expect_selected(page, "Usage")


def test_hash_entity_tab_from_url(page: Page, server: LiveServer) -> None:
    """Check that a hash URL's tab, before its fragment, is shown, and stays chosen when the fragment changes."""
    page.goto(f"{server.url}/example.org/onto?tab=usage#Alpha")
    expect(page.locator("main h1")).to_have_text(f"{EX}/onto#Alpha")
    expect_selected(page, "Usage")
    page.evaluate("location.hash = 'Beta'")
    expect(page.locator("main h1")).to_have_text(f"{EX}/onto#Beta")
    expect_selected(page, "Usage")


def test_served_page_after_tab_change(page: Page, server: LiveServer) -> None:
    """Check that a path's own entity, shown again when the fragment is cleared, shows the tab its URL now names.

    The tab was chosen on a hash entity's page, so the page served for the path shows another tab.
    """
    page.goto(f"{server.url}/example.org/thing#Delta")
    expect(page.locator("main h1")).to_have_text(f"{EX}/thing#Delta")
    tab(page, "Usage").click()
    expect(page).to_have_url(f"{server.url}/example.org/thing?tab=usage#Delta")
    page.evaluate("location.hash = ''")
    expect(page.locator("main h1")).to_have_text(f"{EX}/thing")
    expect_selected(page, "Usage")


def test_restored_page_tabs_work(page: Page, server: LiveServer) -> None:
    """Check that a path's own entity's tabs still fetch only their panel once the fragment is cleared."""
    page.goto(f"{server.url}/example.org/thing#Delta")
    expect(page.locator("main h1")).to_have_text(f"{EX}/thing#Delta")
    page.evaluate("location.hash = ''")
    expect(page.locator("main h1")).to_have_text(f"{EX}/thing")
    page.evaluate("window.marker = 1")
    with page.expect_request(PANEL):
        tab(page, "Usage").click()
    expect_selected(page, "Usage")
    expect(page).to_have_url(f"{server.url}/example.org/thing?tab=usage")
    assert page.evaluate("window.marker") == 1


def test_tabs_under_policy(page: Page, server: LiveServer) -> None:
    """Check that the tabs work under the Content-Security-Policy."""
    watch_policy(page)
    page.goto(f"{server.url}/example.org/plain")
    tab(page, "Usage").click()
    expect(page.get_by_role("tabpanel")).to_contain_text(USAGE_TEXT)
    assert violations(page) == []


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_tabs_look(page: Page, server: LiveServer, scheme: str) -> None:
    """Check that the tabs are underlined text on the page background, unlike the tinted navigation bar."""
    page.emulate_media(color_scheme=scheme)  # type: ignore[arg-type]
    page.goto(f"{server.url}/example.org/plain")
    style = """(element) => {
        const style = getComputedStyle(element);
        return [style.backgroundColor, style.borderBottomColor, style.borderBottomWidth, style.color];
    }"""
    tablist = page.get_by_role("tablist").evaluate(style)
    selected = tab(page, "Overview").evaluate(style)
    other = tab(page, "Usage").evaluate(style)
    bar = page.locator(".site-bar").evaluate(style)
    assert tablist[0] == "rgba(0, 0, 0, 0)" != bar[0]  # the page's background shows through
    assert selected[2] == "3px"
    assert selected[1] == page.evaluate("getComputedStyle(document.querySelector('.brand')).color")  # the accent
    assert other[1] == "rgba(0, 0, 0, 0)"
    assert selected[3] != other[3]


# The entity page header (P-14c, U-5)


def test_entity_header_iri_folded(page: Page, server: LiveServer) -> None:
    """Check that the header's IRI is folded away until its <details> is opened."""
    page.goto(f"{server.url}/example.org/plain")
    iri = page.locator(".entity-iri dd code")
    expect(page.locator(".entity-type")).to_have_text("Class")
    expect(iri).to_be_hidden()
    page.locator(".entity-iri summary").click()
    expect(iri).to_have_text(f"{EX}/plain")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_entity_header_look(page: Page, server: LiveServer, scheme: str) -> None:
    """Check that the header's type line and the IRI's summary are muted, unlike the heading."""
    page.emulate_media(color_scheme=scheme)  # type: ignore[arg-type]
    page.goto(f"{server.url}/example.org/plain")
    color = "(element) => getComputedStyle(element).color"
    muted = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--muted').trim()")
    probe = page.evaluate(
        f"""() => {{ const probe = document.createElement('span'); probe.style.color = '{muted}';
        document.body.append(probe); const value = getComputedStyle(probe).color; probe.remove(); return value; }}"""
    )
    heading = page.locator("main h1").evaluate(color)
    assert page.locator(".entity-type").evaluate(color) == probe != heading
    assert page.locator(".entity-iri summary").evaluate(color) == probe
