"""Browser tests: what only a browser running browl's scripts shows (the hash-IRI script, htmx, no-JS fallbacks).

They drive headless Chromium through Playwright against the app on a live server (`live_server`).
"""

import asyncio
import contextlib
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
import browl.lookup
import browl.overview
import browl.pages
from browl import owl
from browl.annotations import AnnotationValue, IRIValue, LiteralValue
from browl.derived import EntityType
from browl.jobs import JobContext, Stage, StageFunction
from browl.jobtypes import StageError
from browl.links import EntityLink
from browl.rdf import IRI, RDFS, XSD
from browl.rdf import RDF as RDF_NS
from browl.usage_tab import PageNotFoundError, UsageGroup, UsageTab
from tests.conftest import DATA
from tests.helpers import WAIT_S, LiveServer, derived_stage, iso, make_app, no_axioms
from tests.test_trees import tree_stage

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


def mark(page: Page) -> None:
    """Set a marker on the page's window, which a page load loses (`marked`)."""
    page.evaluate("window.marker = 1")


def marked(page: Page) -> bool:
    """Return whether the page's window still has the marker `mark` set, so it hasn't loaded again."""
    return page.evaluate("window.marker") == 1


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
    mark(page)
    if how == "assign":
        page.evaluate("location.hash = 'Beta'")
    else:  # the link sits outside <main>, which the script replaces
        page.locator("header").evaluate(
            """(header) => header.insertAdjacentHTML("beforeend", '<a href="#Beta">B</a>')"""
        )
        page.click("header a[href='#Beta']")
    expect(page.locator("main h1")).to_have_text(f"{EX}/onto#Beta")
    assert marked(page)


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
    mark(page)
    start_endless_job(server)
    page.evaluate("location.hash = 'Alpha'")
    expect(page.locator("main h1")).to_have_text("Loading the ontology")
    expect(page.locator('meta[http-equiv="refresh"]')).to_have_count(1)
    assert not marked(page)  # reloaded, not swapped in


def test_not_available_reloads(page: Page, server: LiveServer) -> None:
    """Check that a fragment change once a later load has failed reloads the page, which then shows "not available"."""

    async def fail(context: JobContext) -> None:
        raise StageError("boom")

    page.goto(f"{server.url}/example.org/onto")
    mark(page)
    server.app.state.jobs.register(Stage.LOADING, fail)
    server.call(server.app.state.jobs.start())
    server.call(server.app.state.jobs.wait())
    page.evaluate("location.hash = 'Alpha'")
    expect(page.locator("main h1")).to_have_text("Not available")
    assert not marked(page)


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
    mark(page)
    status_server.call(release(reasoned))
    expect(current).to_have_text("loading")
    status_server.call(release(loaded))
    expect(current).to_have_text("ready")
    assert marked(page)
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
    """Check that, on a narrow screen, the name, Sign in and the menu button share a row, and the menu opens below."""
    page.set_viewport_size(NARROW)
    page.goto(f"{server.url}/example.org/thing")
    bar, name, button = page.locator(".site-bar"), page.locator(".site-name"), page.locator(".site-menu summary")
    sign_in = page.get_by_role("link", name="Sign in")
    expect(sign_in).to_be_visible()
    height = box(bar)["height"]
    middle = box(name)["y"] + box(name)["height"] / 2
    for each in (button, sign_in):
        assert abs(middle - box(each)["y"] - box(each)["height"] / 2) < 2
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
USAGE_TEXT = "The usages can't be shown just now."  # the tests' app has no Fuseki


def tab(page: Page, name: str) -> Locator:
    """Return the page tab with a name."""
    return page.get_by_role("tablist").get_by_role("tab", name=name)


def one_page_toggle(page: Page) -> Locator:
    """Return the entity page's "Scroll as one page" toggle."""
    return page.get_by_label("Scroll as one page")


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
    mark(page)
    length = page.evaluate("history.length")
    with page.expect_request(PANEL) as request:
        tab(page, "Usage").click()
    assert query_of(request.value.url) == {"tab": ["usage"], "panel": ["1"]}
    expect(page.get_by_role("tabpanel")).to_contain_text(USAGE_TEXT)
    expect_selected(page, "Usage")
    expect(page).to_have_url(f"{server.url}/example.org/plain?tab=usage")
    assert marked(page)
    assert page.evaluate("history.length") == length
    expect(page.locator("main h1")).to_have_count(1)  # the panel alone, not a page inside the page
    tab(page, "Overview").click()
    expect(page.get_by_role("tabpanel")).to_contain_text(OVERVIEW_TEXT)
    expect(page).to_have_url(f"{server.url}/example.org/plain")


def record_panels(page: Page) -> list[str]:
    """Return a list that collects the URLs of the page's panel requests, in order."""
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url) if PANEL.search(request.url) else None)
    return requests


def watch_aborts(page: Page) -> None:
    """Record the paths of the page's htmx requests that are aborted, for `wait_aborted`."""
    page.evaluate(
        "window.aborted = [];"
        "document.addEventListener('htmx:sendAbort', (event) => aborted.push(event.detail.pathInfo.requestPath))"
    )


def wait_aborted(page: Page, part: str) -> None:
    """Wait until htmx has aborted a request whose path contains `part`, so its response can't be handled."""
    wait_until(page, lambda: any(part in path for path in page.evaluate("window.aborted")), f"htmx to abort {part}")


def let_through(route: Route) -> None:
    """Let a held request through, unless the page aborted it."""
    with contextlib.suppress(PlaywrightError):
        route.continue_()


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
    mark(page)
    watch_aborts(page)
    tab(page, "Usage").click()
    wait_until(page, lambda: bool(held), "the request for Usage's panel")
    tab(page, "Overview").click()
    wait_aborted(page, "tab=usage")
    let_through(held[0])
    expect_selected(page, "Overview")
    expect(page.get_by_role("tabpanel")).to_contain_text(OVERVIEW_TEXT)
    expect(page).to_have_url(f"{server.url}/example.org/plain")
    assert marked(page)


def test_usage_label_fills_in(page: Page, server: LiveServer) -> None:
    """Check that the Usage tab's count fills in its label once the page renders, without opening the tab.

    Its panel then opens as before, and keeps the label.
    """
    page.route(
        re.compile(r"/_browl/usage-count\?"), lambda route: route.fulfill(body="Usage (7)", content_type="text/html")
    )
    page.goto(f"{server.url}/example.org/plain")
    expect(tab(page, "Usage")).to_have_text("Usage (7)")
    expect(tab(page, "Overview")).to_have_attribute("aria-selected", "true")
    expect(tab(page, "Usage")).to_have_attribute("aria-selected", "false")
    expect(page).to_have_url(f"{server.url}/example.org/plain")
    expect(page.get_by_role("tabpanel")).to_contain_text(OVERVIEW_TEXT)
    with page.expect_request(PANEL):
        tab(page, "Usage").click()
    expect(page.get_by_role("tabpanel")).to_contain_text(USAGE_TEXT)
    expect(tab(page, "Usage")).to_have_attribute("aria-selected", "true")
    expect(tab(page, "Usage")).to_have_text("Usage (7)")
    expect(page).to_have_url(f"{server.url}/example.org/plain?tab=usage")


def test_usage_label_fails(page: Page, server: LiveServer) -> None:
    """Check that a failed fetch of the Usage tab's label leaves the label and the page as they are."""
    failed: list[Route] = []

    def fail(route: Route) -> None:
        failed.append(route)
        route.fulfill(status=503, body="Loading", content_type="text/html")

    page.route(re.compile(r"/_browl/usage-count\?"), fail)
    page.goto(f"{server.url}/example.org/plain")
    wait_until(page, lambda: bool(failed), "the request for the Usage tab's label")
    page.wait_for_timeout(100)  # time for htmx and the tabs' script to handle the response
    expect(page).to_have_url(f"{server.url}/example.org/plain")
    expect(tab(page, "Usage")).to_have_text("Usage")
    expect_selected(page, "Overview")


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
    expect(one_page_toggle(page)).to_be_focused()  # at the tab bar's end
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


def fail_panels(page: Page, failure: str) -> None:
    """Make the page's panel requests fail: with a 503 for `status`, else with a network error."""
    if failure == "status":
        page.route(PANEL, lambda route: route.fulfill(status=503, content_type="text/html", body="<p>Loading</p>"))
    else:
        page.route(PANEL, lambda route: route.abort())


@pytest.mark.parametrize("failure", ["status", "network"])
def test_tab_panel_failure_loads_page(page: Page, server: LiveServer, failure: str) -> None:
    """Check that a tab whose panel can't be fetched loads its page instead."""
    fail_panels(page, failure)
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
    mark(page)
    with page.expect_request(PANEL):
        tab(page, "Usage").click()
    expect_selected(page, "Usage")
    expect(page).to_have_url(f"{server.url}/example.org/thing?tab=usage")
    assert marked(page)


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


# The Overview tab's annotations panel (P-16a, DESIGN.md §2.8, U-30, U-32)

LONG_VALUE = "word " * 60
LONG_PROPERTY = IRI(f"http://example.org/{'long' * 28}")
"""An undeclared annotation property, shown as its full IRI: a long label with no spaces."""
LONG_IRI = IRI(f"https://example.org/{'a' * 150}")
UNBROKEN = "y" * 300


@pytest.fixture
def annotated(server: LiveServer, monkeypatch: pytest.MonkeyPatch) -> LiveServer:
    """Return the server, every entity's Overview showing the same annotations, and no axioms (it has no Fuseki).

    The groups, in order: `rdfs:label` (two values), `LONG_PROPERTY` (an unbroken literal), `rdfs:comment` (a long
    value), `rdfs:seeAlso` (a short and a long web IRI).
    """
    label, comment, see_also = (IRI(f"{RDFS}{name}") for name in ("label", "comment", "seeAlso"))
    string, lang_string = IRI(f"{XSD}string"), IRI(f"{RDF_NS}langString")
    pairs = [
        (label, LiteralValue(lexical="thing", datatype=lang_string, language="en")),
        (label, LiteralValue(lexical="Ding", datatype=lang_string, language="de")),
        (comment, LiteralValue(lexical=LONG_VALUE, datatype=string)),
        (see_also, IRIValue(iri=IRI("https://example.org/elsewhere"))),
        (see_also, IRIValue(iri=LONG_IRI)),
        (LONG_PROPERTY, LiteralValue(lexical=UNBROKEN, datatype=string)),
    ]

    async def fetched(*args: object) -> list[tuple[IRI, AnnotationValue]]:
        return pairs

    monkeypatch.setattr(browl.overview, "fetch_annotations", fetched)
    monkeypatch.setattr(browl.overview, "_read_axioms", no_axioms)
    return server


def test_annotations_grid_wide(page: Page, annotated: LiveServer) -> None:
    """Check the two-column grid of annotations, wide.

    A group's first value is on its label's row, the rest below it, one per line, in one column for every group; a long
    label wraps rather than squeezing the values; a long value wraps within the readable measure; language tags are
    small and muted. The page is in the one-page view, so the panel is the page's width.
    """
    page.set_viewport_size(WIDE)
    page.goto(f"{annotated.url}/example.org/thing")
    one_page_toggle(page).check()
    terms = page.locator(".annotations dt")
    values = page.locator(".annotations dd")
    expect(terms).to_have_count(4)
    expect(values).to_have_count(6)
    expect(terms.nth(1)).to_have_text(LONG_PROPERTY)
    firsts = page.locator(".annotations dt + dd")
    for index in range(4):
        term, value = box(terms.nth(index)), box(firsts.nth(index))
        assert abs(term["y"] - value["y"]) < 2  # a group's first value is on its label's row
        assert value["x"] > term["x"] + term["width"]
    value_boxes = [box(values.nth(index)) for index in range(6)]
    assert value_boxes[1]["y"] >= value_boxes[0]["y"] + value_boxes[0]["height"]  # one line per value
    assert len({round(each["x"]) for each in value_boxes}) == 1  # one value column for every group
    char = page.evaluate("parseFloat(getComputedStyle(document.body).fontSize)")
    assert box(terms.nth(1))["width"] <= 20 * char + 1  # the long label wraps at 20rem
    comment = value_boxes[3]
    assert 40 * char < comment["width"] < 60 * char  # not squeezed by the long label, and capped near 75ch
    assert comment["height"] > 2 * value_boxes[0]["height"]  # so it wraps
    tag = page.locator(".annotations .lang").first
    expect(tag).to_have_text("en")
    sizes = tag.evaluate("each => [getComputedStyle(each).fontSize, getComputedStyle(each.parentNode).fontSize]")
    assert float(sizes[0].removesuffix("px")) < float(sizes[1].removesuffix("px"))
    muted = page.evaluate("getComputedStyle(document.querySelector('.note')).color")
    assert tag.evaluate("each => getComputedStyle(each).color") == muted


def test_annotations_grid_narrow(page: Page, annotated: LiveServer) -> None:
    """Check that on a phone each group's values go below its label, and nothing long makes the page scroll sideways."""
    page.set_viewport_size(NARROW)
    page.goto(f"{annotated.url}/example.org/thing")
    term, value = box(page.locator(".annotations dt").first), box(page.locator(".annotations dd").first)
    assert value["y"] >= term["y"] + term["height"]
    assert abs(value["x"] - term["x"]) < 2
    assert page.evaluate("document.documentElement.scrollWidth") <= NARROW["width"]


def test_annotations_layout(page: Page, annotated: LiveServer) -> None:
    """Check the annotations panel's finer layout, wide.

    In the one-page view, it comes before the axioms panel; its labels are muted; a value's wrapped lines are indented;
    groups are a little further apart than a group's values.
    """
    page.set_viewport_size(WIDE)
    page.goto(f"{annotated.url}/example.org/thing")
    one_page_toggle(page).check()
    assert box(page.locator("section.annotations"))["y"] < box(page.locator("section.axioms"))["y"]
    muted = page.evaluate("getComputedStyle(document.querySelector('.note')).color")
    assert page.locator(".annotations dt").first.evaluate("each => getComputedStyle(each).color") == muted
    comment = page.locator(".annotations dd").nth(3)
    expect(comment).to_contain_text("word word")
    lines = comment.evaluate(
        """each => {
            const range = document.createRange();
            range.selectNodeContents(each.querySelector("bdi").firstChild);
            return [...range.getClientRects()].sort((a, b) => a.top - b.top).map(rect => rect.left);
        }"""
    )
    assert len(lines) > 2
    assert min(lines[1:]) > lines[0] + 4  # wrapped lines indented
    values = [box(page.locator(".annotations dd").nth(index)) for index in range(3)]
    within = values[1]["y"] - (values[0]["y"] + values[0]["height"])
    between = values[2]["y"] - (values[1]["y"] + values[1]["height"])
    assert between > within + 4


# The window-filling frame (P-16d, DESIGN.md §2.9, U-30, U-44)

MEDIUM = {"width": 800, "height": 800}
"""Between the frame's breakpoints, 40rem and 64rem wide: the panels one above the other."""
SHORT = {"width": 1280, "height": 500}
"""Shorter than 36rem: the one-page view."""
FEW, MANY = "plain", "thing"
"""Entities with one annotation, and with `MANY_VALUES`."""
MANY_VALUES = 80


@pytest.fixture
def varied(server: LiveServer, monkeypatch: pytest.MonkeyPatch) -> LiveServer:
    """Return the server, `FEW` with one annotation and no axioms, `MANY` with `MANY_VALUES` of each (no Fuseki)."""
    label, comment = IRI(f"{RDFS}label"), IRI(f"{RDFS}comment")
    string = IRI(f"{XSD}string")

    async def fetched(sparql: object, dataset: object, iri: str, *args: object) -> list[tuple[IRI, AnnotationValue]]:
        pairs: list[tuple[IRI, AnnotationValue]] = [(label, LiteralValue(lexical="name", datatype=string))]
        if iri.endswith(f"/{MANY}"):
            pairs += [(comment, LiteralValue(lexical=f"comment {i}", datatype=string)) for i in range(MANY_VALUES)]
        return pairs

    async def read_axioms(request: object, dataset: object, iri: str, *args: object) -> tuple[list[owl.AnyAxiom], bool]:
        if not iri.endswith(f"/{MANY}"):
            return [], False
        entity = owl.Entity(kind=owl.EntityKind.CLASS, iri=iri)
        supers = (owl.Entity(kind=owl.EntityKind.CLASS, iri=f"{EX}/super{i}") for i in range(MANY_VALUES))
        return [owl.SubClassOf(sub=entity, super=each) for each in supers], False

    monkeypatch.setattr(browl.overview, "fetch_annotations", fetched)
    monkeypatch.setattr(browl.overview, "_read_axioms", read_axioms)
    return server


def panels(page: Page) -> tuple[FloatRect, FloatRect]:
    """Return the Overview's annotations and axioms panels' boxes."""
    return box(page.locator("section.annotations")), box(page.locator("section.axioms"))


def page_scrolls(page: Page) -> bool:
    """Return whether the page itself is taller than the window."""
    result: bool = page.evaluate("document.documentElement.scrollHeight > window.innerHeight")
    return result


def overflows(locator: Locator) -> bool:
    """Return whether an element's content is taller than the element, so it scrolls."""
    result: bool = locator.evaluate("(each) => each.scrollHeight > each.clientHeight")
    return result


@pytest.mark.parametrize("size", [WIDE, MEDIUM], ids=["wide", "medium"])
def test_panels_fixed(page: Page, varied: LiveServer, size: dict[str, int]) -> None:
    """Check that the panels have the same boxes on every entity page, whatever its content, filling the window.

    Wide, they're side by side, annotations on the left, each full height; medium, annotations on top, 40% to 60%. The
    page itself doesn't scroll; the panel with many annotations does.
    """
    page.set_viewport_size(size)
    boxes = []
    for entity in [FEW, MANY]:
        page.goto(f"{varied.url}/example.org/{entity}")
        boxes.append(panels(page))
        assert not page_scrolls(page)
        for each in ("annotations", "axioms"):
            assert overflows(page.locator(f"section.{each}")) is (entity == MANY)
    assert boxes[0] == boxes[1]
    annotations, axioms = boxes[0]
    space = page.evaluate("parseFloat(getComputedStyle(document.body).fontSize)")
    assert abs(axioms["y"] + axioms["height"] - (size["height"] - space)) < 2  # down to the window's padding
    if size == WIDE:
        assert annotations["y"] == axioms["y"] and annotations["height"] == axioms["height"]
        assert annotations["x"] + annotations["width"] < axioms["x"]
    else:
        assert annotations["x"] == axioms["x"] and annotations["width"] == axioms["width"]
        assert annotations["y"] + annotations["height"] < axioms["y"]
        assert abs(annotations["height"] / axioms["height"] - 2 / 3) < 0.02


def test_panel_scrolls(page: Page, varied: LiveServer) -> None:
    """Check that long panels scroll within themselves, by mouse and by keyboard, their headings staying in view.

    The keyboard reaches them with Tab after the tab panel, and they show a focus ring; the page itself doesn't move.
    The panels have borders, and their headings a background, so the content scrolls out of sight under them.
    """
    page.set_viewport_size(WIDE)
    page.goto(f"{varied.url}/example.org/{MANY}")
    panel = page.get_by_role("region", name="Annotations")
    heading = panel.get_by_role("heading", name="Annotations")
    panel.hover()
    page.mouse.wheel(0, 300)
    wait_until(page, lambda: panel.evaluate("(each) => each.scrollTop") > 0, "the panel to scroll")
    panel.evaluate("(each) => { each.scrollTop = 0; }")
    page.get_by_role("tabpanel").focus()
    page.keyboard.press("Tab")
    expect(panel).to_be_focused()
    assert panel.evaluate("(each) => getComputedStyle(each).outlineStyle") == "solid"
    page.keyboard.press("End")
    wait_until(page, lambda: panel.evaluate("(each) => each.scrollTop") > 0, "the panel to scroll")
    assert abs(box(heading)["y"] - box(panel)["y"]) < 2  # the heading stays at the top
    expect(panel.get_by_text(f"comment {MANY_VALUES - 1}", exact=True)).to_be_in_viewport()
    assert page.evaluate("window.scrollY") == 0
    assert panel.evaluate("(each) => getComputedStyle(each).borderTopStyle") == "solid"
    assert heading.evaluate("(each) => getComputedStyle(each).backgroundColor") == page.evaluate(
        "getComputedStyle(document.body).backgroundColor"
    )
    axioms = page.get_by_role("region", name="Logical axioms")
    page.keyboard.press("Tab")
    expect(axioms).to_be_focused()
    page.keyboard.press("End")
    wait_until(page, lambda: axioms.evaluate("(each) => each.scrollTop") > 0, "the axioms panel to scroll")
    expect(axioms.get_by_text(f"<{EX}/super{MANY_VALUES - 1}>", exact=True)).to_be_in_viewport()


def expect_one_page(page: Page) -> None:
    """Check the one-page view: the page scrolls, the panels don't, and the axioms come after the annotations."""
    assert page_scrolls(page)
    annotations, axioms = panels(page)
    assert not overflows(page.locator("section.annotations"))
    assert not overflows(page.locator("section.axioms"))
    assert axioms["y"] > annotations["y"] + annotations["height"]


@pytest.mark.parametrize("size", [NARROW, SHORT], ids=["narrow", "short"])
def test_small_windows_one_page(page: Page, varied: LiveServer, size: dict[str, int]) -> None:
    """Check that a narrow or short window gets the one-page view, without the toggle, which would change nothing."""
    page.set_viewport_size(size)
    page.goto(f"{varied.url}/example.org/{MANY}")
    expect_one_page(page)
    expect(one_page_toggle(page)).to_be_hidden()


@pytest.mark.parametrize(
    ("size", "layout"),
    [
        ({"width": 1024, "height": 800}, "side by side"),
        ({"width": 1023, "height": 800}, "stacked"),
        ({"width": 640, "height": 800}, "stacked"),
        ({"width": 639, "height": 800}, "one page"),
        ({"width": 1280, "height": 576}, "side by side"),
        ({"width": 1280, "height": 575}, "one page"),
    ],
    ids=lambda each: f"{each['width']}x{each['height']}" if isinstance(each, dict) else each,
)
def test_breakpoints(page: Page, varied: LiveServer, size: dict[str, int], layout: str) -> None:
    """Check the layout either side of each breakpoint: 64rem and 40rem wide, 36rem tall."""
    page.set_viewport_size(size)
    page.goto(f"{varied.url}/example.org/{MANY}")
    if layout == "one page":
        expect_one_page(page)
        expect(one_page_toggle(page)).to_be_hidden()
        return
    assert not page_scrolls(page)
    expect(one_page_toggle(page)).to_be_visible()
    annotations, axioms = panels(page)
    if layout == "side by side":
        assert annotations["y"] == axioms["y"]
        assert annotations["x"] + annotations["width"] < axioms["x"]
    else:
        assert annotations["x"] == axioms["x"]
        assert annotations["y"] + annotations["height"] < axioms["y"]


def test_stacked_minimum(page: Page, varied: LiveServer) -> None:
    """Check that, stacked in a window just tall enough for the frame, each panel is still at least 10rem high."""
    page.set_viewport_size({"width": 800, "height": 580})
    page.goto(f"{varied.url}/example.org/{MANY}")
    assert not page_scrolls(page)
    assert all(each["height"] >= 160 for each in panels(page))


def test_hash_entity_framed(page: Page, varied: LiveServer) -> None:
    """Check that a hash entity's page, which the hash-IRI script moves in, gets the frame: the same panel boxes."""
    page.set_viewport_size(WIDE)
    page.goto(f"{varied.url}/example.org/{FEW}")
    shown = panels(page)
    page.goto(f"{varied.url}/example.org/vocab#Gamma")
    expect(page.locator("main h1")).to_have_text(f"{EX}/vocab#Gamma")
    assert not page_scrolls(page)
    assert panels(page) == shown


def test_print_one_page(page: Page, varied: LiveServer) -> None:
    """Check that printing gets the one-page view, whatever the window size, and without the toggle.

    An opened IRI takes its own room rather than covering what's below it.
    """
    page.set_viewport_size(WIDE)
    page.emulate_media(media="print")
    page.goto(f"{varied.url}/example.org/{MANY}")
    expect_one_page(page)
    expect(one_page_toggle(page)).to_be_hidden()
    page.locator(".entity-iri summary").click()
    details, tab_bar = box(page.locator(".entity-iri dl")), box(page.locator(".tab-bar"))
    assert details["y"] + details["height"] <= tab_bar["y"]


def test_toggle_remembered(page: Page, varied: LiveServer) -> None:
    """Check that the toggle gives the one-page view, and is remembered on other pages, a hash entity's too."""
    page.set_viewport_size(WIDE)
    page.goto(f"{varied.url}/example.org/{MANY}")
    toggle = one_page_toggle(page)
    expect(toggle).not_to_be_checked()
    toggle.check()
    expect_one_page(page)
    page.reload()
    expect(toggle).to_be_checked()
    expect_one_page(page)
    page.goto(f"{varied.url}/example.org/vocab#Gamma")  # its <main> moved in by the hash-IRI script
    expect(page.locator("main h1")).to_have_text(f"{EX}/vocab#Gamma")
    expect(toggle).to_be_checked()
    toggle.uncheck()
    page.goto(f"{varied.url}/example.org/{MANY}")
    expect(toggle).not_to_be_checked()
    assert not page_scrolls(page)


@pytest.mark.parametrize("entity", [FEW, MANY])
def test_toggle_keeps_header(page: Page, varied: LiveServer, entity: str) -> None:
    """Check that the toggle doesn't move the header's name, type or IRI, whether or not the page then scrolls."""
    page.set_viewport_size(WIDE)
    page.goto(f"{varied.url}/example.org/{entity}")
    parts = [page.locator(each) for each in ("main h1", ".entity-type", ".entity-iri summary")]
    before = [box(each) for each in parts]
    one_page_toggle(page).check()
    if entity == MANY:
        expect_one_page(page)
    assert [box(each) for each in parts] == before


@NO_JS
def test_toggle_without_javascript(page: Page, varied: LiveServer) -> None:
    """Check that, without JavaScript, the toggle still gives the one-page view, though it isn't remembered."""
    page.set_viewport_size(WIDE)
    page.goto(f"{varied.url}/example.org/{MANY}")
    one_page_toggle(page).check()
    expect_one_page(page)
    page.goto(f"{varied.url}/example.org/{FEW}")
    expect(one_page_toggle(page)).not_to_be_checked()


ONE_PAGE_KEY = "browl.onePage"
"""Where `static/one-page.js` remembers the toggle."""


@pytest.mark.parametrize(
    ("storage", "script"),
    [
        ("blocked", "Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('blocked'); } });"),
        (
            "full",
            (
                f"localStorage.setItem('{ONE_PAGE_KEY}', 'true');"
                " Storage.prototype.setItem = () => { throw new DOMException('full', 'QuotaExceededError'); };"
            ),
        ),
    ],
    ids=["blocked", "full"],
)
def test_storage_fails(page: Page, varied: LiveServer, storage: str, script: str) -> None:
    """Check that the toggle works, without script errors, when `localStorage` is blocked, or full but remembers it."""
    errors: list[PlaywrightError] = []
    page.on("pageerror", lambda error: errors.append(error))
    page.add_init_script(script)
    page.set_viewport_size(WIDE)
    page.goto(f"{varied.url}/example.org/{MANY}")
    toggle = one_page_toggle(page)
    if storage == "blocked":
        expect(toggle).not_to_be_checked()
        toggle.check()
        expect_one_page(page)
    else:
        expect(toggle).to_be_checked()
        toggle.uncheck()  # not stored
        assert not page_scrolls(page)
    page.goto(f"{varied.url}/example.org/vocab#Gamma")  # its <main> moved in by the hash-IRI script
    expect(page.locator("main h1")).to_have_text(f"{EX}/vocab#Gamma")
    expect(toggle).to_be_checked(checked=storage == "full")
    assert errors == []


def test_toggle_restored_while_parsing(page: Page, varied: LiveServer) -> None:
    """Check that the toggle is restored before the page has been parsed, so a framed page isn't drawn first."""
    page.add_init_script(f"localStorage.setItem('{ONE_PAGE_KEY}', 'true')")
    held: list[Route] = []
    page.route("**/one-page.js", lambda route: held.append(route))
    page.set_viewport_size(WIDE)
    page.goto(f"{varied.url}/example.org/{MANY}", wait_until="commit")
    wait_until(page, lambda: bool(held), "the script's request")
    assert page.evaluate("document.readyState") == "loading"  # the parser waits for the script
    held[0].continue_()
    expect(one_page_toggle(page)).to_be_checked()


def test_toggle_follows_changes(page: Page, varied: LiveServer) -> None:
    """Check that the toggle follows another tab's change, and is restored on a page from the back-forward cache."""
    page.set_viewport_size(WIDE)
    page.goto(f"{varied.url}/example.org/{MANY}")
    other = page.context.new_page()
    other.goto(f"{varied.url}/example.org/{FEW}")
    one_page_toggle(other).check()
    expect(one_page_toggle(page)).to_be_checked()
    page.evaluate(f"localStorage.setItem('{ONE_PAGE_KEY}', 'false')")  # a page gets no storage event of its own
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }))")
    expect(one_page_toggle(page)).not_to_be_checked()


def test_header_fixed_height(page: Page, varied: LiveServer) -> None:
    """Check that a long name is clamped to two lines, whole in its tooltip, and the opened IRI overlays the panels.

    Neither moves the panels.
    """
    page.set_viewport_size(MEDIUM)
    page.goto(f"{varied.url}/example.org/{FEW}")
    heading = page.locator("main h1")
    expect(heading).to_have_attribute("title", iso(f"{EX}/{FEW}"))
    line = box(heading)["height"]
    assert box(page.locator(".entity-meta"))["x"] < box(heading)["x"]  # type and IRI first
    shown = panels(page)
    heading.evaluate("(each) => { each.textContent = 'long name '.repeat(60); }")
    assert 1.5 * line < box(heading)["height"] < 2.5 * line
    assert panels(page) == shown
    types = page.locator(".entity-type")
    type_line = box(types)["height"]
    types.evaluate("(each) => { each.textContent = 'Class · '.repeat(30); }")
    assert box(types)["height"] == type_line  # one line, cut short
    assert types.evaluate("(each) => each.scrollWidth > each.clientWidth")
    assert panels(page) == shown
    page.locator(".entity-iri summary").click()
    iri = page.locator(".entity-iri dd code")
    expect(iri).to_be_visible()
    assert panels(page) == shown
    header = box(page.locator(".entity-header"))
    details = box(page.locator(".entity-iri dl"))
    assert details["y"] + details["height"] > header["y"] + header["height"]  # over what's below the header


def test_right_to_left_name(page: Page, varied: LiveServer) -> None:
    """Check that a right-to-left name is laid out right to left, so cut short it ends in "…" on its left.

    The type and IRI stay first, on the left.
    """
    page.set_viewport_size(MEDIUM)
    page.goto(f"{varied.url}/example.org/{FEW}")
    heading = page.locator("main h1")
    heading.evaluate("(each) => { each.textContent = 'שם ארוך '.repeat(60); }")
    assert heading.evaluate("(each) => getComputedStyle(each).direction") == "rtl"
    assert box(page.locator(".entity-meta"))["x"] < box(heading)["x"]


def on_top(page: Page, x: float, y: float, selector: str) -> bool:
    """Return whether what's drawn at a point in the window is in an element matching a selector."""
    result: bool = page.evaluate(f"document.elementFromPoint({x}, {y})?.closest({selector!r}) != null")
    return result


def test_iri_box_stacking(page: Page, varied: LiveServer) -> None:
    """Check that the opened IRI box is drawn under the navigation bar when scrolled there, and over a panel heading."""
    page.set_viewport_size(NARROW)  # the one-page view, so the page scrolls
    page.goto(f"{varied.url}/example.org/{MANY}")
    page.locator(".entity-iri summary").click()
    details, bar = box(page.locator(".entity-iri dl")), box(page.locator(".site-bar"))
    page.evaluate(f"window.scrollBy(0, {details['y'] - bar['height'] / 2})")
    assert on_top(page, details["x"] + 4, bar["height"] / 2, ".site-bar")
    page.set_viewport_size(MEDIUM)
    page.evaluate("window.scrollTo(0, 0)")
    page.locator(".entity-iri dd code").first.evaluate("(each) => { each.textContent = 'segment/'.repeat(200); }")
    heading = box(page.locator(".annotations h2"))
    assert box(page.locator(".entity-iri dl"))["y"] + box(page.locator(".entity-iri dl"))["height"] > heading["y"]
    assert on_top(page, heading["x"] + 4, heading["y"] + heading["height"] / 2, ".entity-iri dl")


def test_other_tab_fills_frame(page: Page, varied: LiveServer) -> None:
    """Check that the Usage tab's panel fills the same area, and scrolls within it what doesn't fit."""
    page.set_viewport_size(WIDE)
    page.goto(f"{varied.url}/example.org/{FEW}")
    panel = page.get_by_role("tabpanel")
    overview = box(panel)
    tab(page, "Usage").click()
    expect(panel).to_contain_text(USAGE_TEXT)
    assert box(panel) == overview
    panel.evaluate("(each) => each.insertAdjacentHTML('beforeend', '<p>line</p>'.repeat(200))")
    assert overflows(panel)
    assert not page_scrolls(page)


def test_sign_in_in_view(page: Page, varied: LiveServer) -> None:
    """Check that the sign-in link is in the navigation bar, in view on an entity page, which has no footer."""
    page.set_viewport_size(WIDE)
    page.goto(f"{varied.url}/example.org/{MANY}")
    expect(page.get_by_role("navigation", name="Site").get_by_role("link", name="Sign in")).to_be_in_viewport()
    expect(page.locator("footer")).to_have_count(0)


# The Usage tab's layout (P-27b, DESIGN.md §2.8)

THING = "/example.org/thing"
VOCAB = "/example.org/vocab"
USAGE_PAGES = 5
USAGE_SUBJECTS = 8
"""How many pages `paged`'s Usage tab has, and how many subjects each page, with `USAGE_LINES` lines each."""
USAGE_LINES = 10


@pytest.fixture
def paged(server: LiveServer, monkeypatch: pytest.MonkeyPatch) -> LiveServer:
    """Return the server, each entity's Usage tab `USAGE_PAGES` pages of `USAGE_SUBJECTS` subjects (no Fuseki).

    Each subject has `USAGE_LINES` lines in columns, half with a short keyword and half with a long one, and names of
    different lengths, so the columns' widths are set by the longest.
    """

    async def read_usage_tab(request: object, state: object, iri: str, page: int) -> UsageTab:
        entity = owl.Entity(kind=owl.EntityKind.CLASS, iri=iri)
        note = owl.Entity(kind=owl.EntityKind.ANNOTATION_PROPERTY, iri=f"{EX}/a-long-annotation-property")
        groups, links = [], {}
        for index in range(USAGE_SUBJECTS):
            subject = IRI(f"{EX}/subject{index}")
            links[subject] = EntityLink(subject, f"subject {'x' * 4 * index}", f"/subject{index}")
            node = owl.Entity(kind=owl.EntityKind.CLASS, iri=subject)
            assertion = owl.AnnotationAssertion(
                subject=owl.IRIReference(iri=subject), property=note, value=owl.IRIReference(iri=iri)
            )
            half = USAGE_LINES // 2
            axioms: list[owl.AnyAxiom] = [owl.SubClassOf(sub=node, super=entity)] * half + [assertion] * half
            groups.append(UsageGroup(subject=subject, axioms=axioms))
        count = USAGE_PAGES * USAGE_SUBJECTS * USAGE_LINES
        return UsageTab(count=count, page=page, pages=USAGE_PAGES, groups=groups, links=links)

    monkeypatch.setattr(browl.lookup, "read_usage_tab", read_usage_tab)
    return server


def test_usage_tab_fills_frame(page: Page, paged: LiveServer) -> None:
    """Check the Usage tab in the window-filling frame: the page doesn't scroll; the tab panel does, under the paging.

    The paging reads "Previous Page N of M Go Next", and the count is in the tab's label only.
    """
    page.set_viewport_size(WIDE)
    page.goto(f"{paged.url}{THING}?tab=usage&page=2")
    panel = page.get_by_role("tabpanel")
    assert overflows(panel)
    assert not page_scrolls(page)  # the subjects visually hidden in each line take no room
    pager = page.get_by_role("navigation", name="Usage pages")
    expect(pager).to_have_text(re.compile(rf"^\s*Previous\s+Page\s+of\s+{USAGE_PAGES}\s+Go\s+Next\s*$"))
    expect(pager.get_by_role("spinbutton")).to_have_value("2")
    count = USAGE_PAGES * USAGE_SUBJECTS * USAGE_LINES
    expect(tab(page, f"Usage ({count})")).to_be_visible()
    expect(panel).not_to_contain_text(str(count))
    top = box(pager)["y"]
    for scrolled in (8, 500):
        panel.evaluate(f"(each) => {{ each.scrollTop = {scrolled}; }}")
        assert box(pager)["y"] == top  # held at the panel's top
    first = box(panel.locator("dt").first)
    assert first["y"] + first["height"] < top  # scrolled out of sight, under the paging
    assert page.evaluate("window.scrollY") == 0


def test_usage_tab_columns(page: Page, paged: LiveServer) -> None:
    """Check that the keywords line up down the tab, as does the rest of each line, whatever the subject's name."""
    page.set_viewport_size(WIDE)
    page.goto(f"{paged.url}{THING}?tab=usage")
    for part in ("usage-keyword", "usage-axiom"):
        lefts = {round(box(each)["x"]) for each in page.locator(f".{part}").all()}
        assert len(lefts) == 1, part
    keyword, rest = box(page.locator(".usage-keyword").first), box(page.locator(".usage-axiom").first)
    assert keyword["x"] + keyword["width"] <= rest["x"]


def test_usage_tab_look(page: Page, paged: LiveServer) -> None:
    """Check that Previous on the first page is grayed out, not a link, and the entity is highlighted, in bold."""
    page.set_viewport_size(WIDE)
    page.goto(f"{paged.url}{THING}?tab=usage")
    pager = page.get_by_role("navigation", name="Usage pages")
    expect(pager.get_by_role("link")).to_have_text(["Next"])
    color = "(each) => getComputedStyle(each).color"
    assert pager.locator(".pager-off").evaluate(color) != pager.get_by_role("link").evaluate(color)
    mark = page.locator(".usage-frame mark").first
    background, weight = mark.evaluate(
        "(each) => [getComputedStyle(each).backgroundColor, getComputedStyle(each).fontWeight]"
    )
    assert background != page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert background != "rgba(0, 0, 0, 0)"
    assert int(weight) >= 600


def test_usage_tab_narrow(page: Page, paged: LiveServer) -> None:
    """Check that on a phone the Usage tab doesn't make the page scroll sideways."""
    page.set_viewport_size(NARROW)
    page.goto(f"{paged.url}{THING}?tab=usage&page=2")
    expect(page.locator(".usage-frame dt").first).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= NARROW["width"]


def test_usage_label_fills_in_on_usage_tab(page: Page, server: LiveServer) -> None:
    """Check that the label of a Usage tab served without its count fills in, the tab staying selected.

    The label's fetch is its own, not the selected tab's, which isn't fetched again.
    """
    page.route(
        re.compile(r"/_browl/usage-count\?"), lambda route: route.fulfill(body="Usage (7)", content_type="text/html")
    )
    page.goto(f"{server.url}/example.org/plain?tab=usage")
    expect(page.get_by_role("tabpanel")).to_contain_text(USAGE_TEXT)
    expect(tab(page, "Usage (7)")).to_be_visible()
    expect_selected(page, "Usage (7)")
    expect(page).to_have_url(f"{server.url}/example.org/plain?tab=usage")


def test_usage_preparing_polls(page: Page, server: LiveServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that "Preparing…" polls its page's panel, and is replaced by it once the usage table is ready.

    The poll keeps the tab selected and the page's URL, and the label keeps its count.
    """
    calls: list[int] = []

    async def read_usage_tab(request: object, state: object, iri: str, number: int) -> UsageTab:
        calls.append(number)
        if len(calls) < 3:  # the page, then the first poll
            return UsageTab(count=30_000, page=number, pages=600, groups=None, preparing=True, remaining_s=90)
        subject = IRI(f"{EX}/subject")
        axiom = owl.SubClassOf(
            sub=owl.Entity(kind=owl.EntityKind.CLASS, iri=subject), super=owl.Entity(kind=owl.EntityKind.CLASS, iri=iri)
        )
        groups = [UsageGroup(subject=subject, axioms=[axiom])]
        return UsageTab(count=30_000, page=number, pages=600, groups=groups)

    monkeypatch.setattr(browl.lookup, "read_usage_tab", read_usage_tab)
    monkeypatch.setattr(browl.lookup, "POLL_S", 0.2)
    polls: list[str] = []
    page.on("request", lambda request: polls.append(request.url) if "panel=1" in request.url else None)
    page.goto(f"{server.url}/example.org/plain?tab=usage&page=2")
    expect(page.locator("#usage-preparing")).to_have_text("30,000 usages. Preparing… Ready in about 2 minutes.")
    expect(page.locator(".usage-frame dt")).to_have_count(1)
    expect(page.locator("#usage-preparing")).to_have_count(0)
    assert query_of(polls[0]) == {"tab": ["usage"], "page": ["2"], "panel": ["1"]}
    assert len(polls) == 2  # it stops once the table is ready
    page.wait_for_timeout(500)
    assert len(polls) == 2
    expect(tab(page, "Usage (30,000)")).to_have_attribute("aria-selected", "true")
    expect(page).to_have_url(f"{server.url}/example.org/plain?tab=usage&page=2")


def test_usage_poll_past_listed(page: Page, server: LiveServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a poll whose page is past those listed, once the usage table is ready, gets the first page.

    A 404 there wouldn't be swapped in, so it would poll on.
    """
    calls: list[int] = []

    async def read_usage_tab(request: object, state: object, iri: str, number: int) -> UsageTab:
        calls.append(number)
        if len(calls) == 1:  # the page
            return UsageTab(count=30_000, page=number, pages=600, groups=None, preparing=True)
        if number > 1:  # some of the usages counted aren't listed
            raise PageNotFoundError(f"page {number} of 1 listed")
        subject = IRI(f"{EX}/subject")
        axiom = owl.SubClassOf(
            sub=owl.Entity(kind=owl.EntityKind.CLASS, iri=subject), super=owl.Entity(kind=owl.EntityKind.CLASS, iri=iri)
        )
        return UsageTab(count=30_000, page=number, pages=1, groups=[UsageGroup(subject=subject, axioms=[axiom])])

    monkeypatch.setattr(browl.lookup, "read_usage_tab", read_usage_tab)
    monkeypatch.setattr(browl.lookup, "POLL_S", 0.2)
    polls: list[str] = []
    page.on("request", lambda request: polls.append(request.url) if "panel=1" in request.url else None)
    page.goto(f"{server.url}/example.org/plain?tab=usage&page=600")
    expect(page.locator(".usage-frame dt")).to_have_count(1)
    expect(page.locator("#usage-preparing")).to_have_count(0)
    page.wait_for_timeout(500)
    assert len(polls) == 1
    assert calls == [600, 600, 1]


# The Usage tab's paging with htmx (DESIGN.md §2.8)

USAGE_LABEL = f"Usage ({USAGE_PAGES * USAGE_SUBJECTS * USAGE_LINES})"


def pager(page: Page) -> Locator:
    """Return the Usage tab's paging."""
    return page.get_by_role("navigation", name="Usage pages")


def pager_link(page: Page, name: str) -> Locator:
    """Return the paging's Previous or Next link."""
    return pager(page).get_by_role("link", name=name)


def expect_usage_page(page: Page, number: int) -> None:
    """Check that the Usage tab shows a page."""
    expect(pager(page).get_by_role("spinbutton")).to_have_value(str(number))


def go_to(page: Page, number: str) -> None:
    """Go to a page with the paging's form."""
    pager(page).get_by_role("spinbutton").fill(number)
    pager(page).get_by_role("button", name="Go").click()


def test_usage_next_swaps_panel(page: Page, paged: LiveServer) -> None:
    """Check that Next fetches only the next page's panel, swaps it in and pushes its URL, without a page load.

    The panel scrolls to its top, and the focus stays on Next, or goes to the panel if the last page has none.
    """
    page.goto(f"{paged.url}{THING}?tab=usage&page=3")
    mark(page)
    length = page.evaluate("history.length")
    panel = page.get_by_role("tabpanel")
    panel.evaluate("(each) => { each.scrollTop = 500; }")
    assert panel.evaluate("(each) => each.scrollTop") > 0
    with page.expect_request(PANEL) as request:
        pager_link(page, "Next").click()
    assert query_of(request.value.url) == {"tab": ["usage"], "page": ["4"], "panel": ["1"]}
    expect(page).to_have_url(f"{paged.url}{THING}?tab=usage&page=4")
    expect_usage_page(page, 4)
    assert marked(page)
    assert page.evaluate("history.length") == length + 1
    expect(page.locator("main h1")).to_have_count(1)  # the panel alone, not a page inside the page
    assert panel.evaluate("(each) => each.scrollTop") == 0
    expect(pager_link(page, "Next")).to_be_focused()
    pager_link(page, "Next").click()
    expect_usage_page(page, 5)
    expect(panel).to_be_focused()


def test_usage_previous_swaps_panel(page: Page, paged: LiveServer) -> None:
    """Check that Previous fetches only the previous page's panel and pushes its URL, the first page's without `page`.

    The focus stays on Previous, or goes to the panel on the first page, which has none (#118, S4).
    """
    page.goto(f"{paged.url}{THING}?tab=usage&page=3")
    mark(page)
    length = page.evaluate("history.length")
    for number, query in [(2, "?tab=usage&page=2"), (1, "?tab=usage")]:
        with page.expect_request(PANEL) as request:
            pager_link(page, "Previous").click()
        assert query_of(request.value.url) == query_of(f"{query}&panel=1")
        expect(page).to_have_url(f"{paged.url}{THING}{query}")
        expect_usage_page(page, number)
    expect(page.get_by_role("tabpanel")).to_be_focused()
    assert marked(page)
    assert page.evaluate("history.length") == length + 2


def test_usage_go_swaps_panel(page: Page, paged: LiveServer) -> None:
    """Check that the form fetches only the page's panel and pushes its URL, the first page's without `page`."""
    page.goto(f"{paged.url}{THING}?tab=usage")
    mark(page)
    number = pager(page).get_by_role("spinbutton")
    number.fill("3")
    with page.expect_request(PANEL) as request:
        number.press("Enter")
    assert query_of(request.value.url) == {"tab": ["usage"], "page": ["3"], "panel": ["1"]}
    expect(page).to_have_url(f"{paged.url}{THING}?tab=usage&page=3")
    expect_usage_page(page, 3)
    expect(number).to_be_focused()
    go_to(page, "1")
    expect(page).to_have_url(f"{paged.url}{THING}?tab=usage")
    expect_usage_page(page, 1)
    expect(pager(page).get_by_role("button", name="Go")).to_be_focused()
    assert marked(page)


def test_usage_go_leading_zero_and_page_shown(page: Page, paged: LiveServer) -> None:
    """Check that the form reads `02` as page 2, and replaces the entry of the page shown (#118, C3).

    So each page keeps one URL, and no entry is repeated. The paging swapped in is a new element.
    """
    page.goto(f"{paged.url}{THING}?tab=usage")
    mark(page)
    length = page.evaluate("history.length")
    for value in ("02", "2"):
        pager(page).evaluate("(nav) => { nav.dataset.old = 'true'; }")
        with page.expect_request(PANEL) as request:
            go_to(page, value)
        assert query_of(request.value.url)["page"] == ["2"]
        expect(pager(page)).not_to_have_attribute("data-old", "true")
        expect(page).to_have_url(f"{paged.url}{THING}?tab=usage&page=2")
        expect_usage_page(page, 2)
    assert page.evaluate("history.length") == length + 1
    assert marked(page)


def test_usage_go_invalid_number(page: Page, paged: LiveServer) -> None:
    """Check that the form fetches nothing for a number that isn't one of the pages.

    The valid number's request comes after any the others made, so they'd be recorded first.
    """
    page.goto(f"{paged.url}{THING}?tab=usage")
    requests = record_panels(page)
    for value in (str(USAGE_PAGES + 1), "0", ""):
        go_to(page, value)
    with page.expect_request(PANEL):
        go_to(page, "3")
    expect_usage_page(page, 3)
    assert [query_of(url).get("page") for url in requests] == [["3"]]


def test_usage_back_and_forward(page: Page, paged: LiveServer) -> None:
    """Check that Back and Forward step through the Usage pages, fetching only their panels, back to the served one."""
    page.goto(f"{paged.url}{THING}?tab=usage")
    mark(page)
    for number in (2, 3):
        pager_link(page, "Next").click()
        expect_usage_page(page, number)
    requests = record_panels(page)
    for step, query, number in [
        (page.go_back, "?tab=usage&page=2", 2),
        (page.go_back, "?tab=usage", 1),
        (page.go_forward, "?tab=usage&page=2", 2),
    ]:
        step()
        expect(page).to_have_url(f"{paged.url}{THING}{query}")
        expect_usage_page(page, number)
    assert [query_of(url).get("page") for url in requests] == [["2"], None, ["2"]]
    assert marked(page)


def test_usage_back_after_tab_change(page: Page, paged: LiveServer) -> None:
    """Check that Back to a Usage page once another tab is chosen loads that page, and the tab's entry is its own."""
    page.goto(f"{paged.url}{THING}?tab=usage")
    mark(page)
    pager_link(page, "Next").click()
    expect_usage_page(page, 2)
    tab(page, "Overview").click()
    expect(page).to_have_url(f"{paged.url}{THING}")
    assert page.evaluate("history.state?.browlPanel ?? null") is None
    page.go_back()
    expect(page).to_have_url(f"{paged.url}{THING}?tab=usage")
    expect_usage_page(page, 1)
    expect_selected(page, USAGE_LABEL)
    assert not marked(page)
    page.go_forward()
    expect(page).to_have_url(f"{paged.url}{THING}")
    expect_selected(page, "Overview")


def test_usage_tab_change_records_panel(page: Page, paged: LiveServer) -> None:
    """Check that the Usage tab, chosen again on a later page's entry, records the panel it now shows for Back."""
    page.goto(f"{paged.url}{THING}?tab=usage&page=2")
    mark(page)
    pager_link(page, "Next").click()
    expect_usage_page(page, 3)
    page.go_back()
    expect_usage_page(page, 2)
    tab(page, "Overview").click()
    expect_selected(page, "Overview")
    tab(page, USAGE_LABEL).click()
    expect_usage_page(page, 1)
    page.go_forward()
    expect_usage_page(page, 3)
    with page.expect_request(PANEL) as request:
        page.go_back()
    assert query_of(request.value.url).get("page") is None  # page 1's panel, not page 2's
    expect(page).to_have_url(f"{paged.url}{THING}?tab=usage")
    expect_usage_page(page, 1)
    assert marked(page)


def test_tab_click_while_page_loads(page: Page, paged: LiveServer) -> None:
    """Check that a tab clicked while a Usage page's panel loads cancels that load, so the tab's panel stays."""
    held: list[Route] = []
    page.route(re.compile(r"page=2&panel=1"), lambda route: held.append(route))
    page.goto(f"{paged.url}{THING}?tab=usage")
    watch_aborts(page)
    pager_link(page, "Next").click()
    wait_until(page, lambda: bool(held), "the request for the next page's panel")
    with page.expect_request(PANEL):
        tab(page, "Overview").click()
    wait_aborted(page, "page=2")
    let_through(held[0])
    expect_selected(page, "Overview")
    expect(page).to_have_url(f"{paged.url}{THING}")


@pytest.mark.parametrize("step", ["Next", "Back"])
def test_selected_tab_click_while_page_loads(page: Page, paged: LiveServer, step: str) -> None:
    """Check that a click on the selected Usage tab while Next's or Back's panel loads lets it load (#118, S1).

    The panel's requests share one queue, which would abort the load for a click it then drops.
    """
    page.goto(f"{paged.url}{THING}?tab=usage")
    held: list[Route] = []
    if step == "Next":
        page.route(re.compile(r"page=2&panel=1"), lambda route: held.append(route))
        pager_link(page, "Next").click()
        number, query = 2, "?tab=usage&page=2"
    else:
        pager_link(page, "Next").click()
        expect_usage_page(page, 2)
        page.route(re.compile(r"\?tab=usage&panel=1"), lambda route: held.append(route))
        page.go_back()
        number, query = 1, "?tab=usage"
    wait_until(page, lambda: bool(held), "the request for the page's panel")
    tab(page, USAGE_LABEL).click()
    let_through(held[0])
    expect(page).to_have_url(f"{paged.url}{THING}{query}")
    expect_usage_page(page, number)


def test_usage_next_twice_while_loading(page: Page, paged: LiveServer) -> None:
    """Check that Next clicked again while the next page's panel loads shows that page once, in one history entry."""
    page.goto(f"{paged.url}{THING}?tab=usage")
    length = page.evaluate("history.length")
    held: list[Route] = []
    page.route(re.compile(r"page=2&panel=1"), lambda route: held.append(route), times=1)
    watch_aborts(page)
    pager_link(page, "Next").click()
    wait_until(page, lambda: bool(held), "the request for the next page's panel")
    pager_link(page, "Next").click()
    wait_aborted(page, "page=2")
    let_through(held[0])
    expect(page).to_have_url(f"{paged.url}{THING}?tab=usage&page=2")
    expect_usage_page(page, 2)
    assert page.evaluate("history.length") == length + 1


def test_usage_next_while_back_loads(page: Page, paged: LiveServer) -> None:
    """Check that Next on the page shown while Back's panel loads keeps the panel Back's entry records (#118, C1)."""
    page.goto(f"{paged.url}{THING}?tab=usage")
    for number in (2, 3):
        pager_link(page, "Next").click()
        expect_usage_page(page, number)
    held: list[Route] = []
    page.route(re.compile(r"page=2&panel=1"), lambda route: held.append(route), times=1)
    page.go_back()
    wait_until(page, lambda: bool(held), "the request for page 2's panel")
    pager_link(page, "Next").click()  # page 3's, still shown
    expect(page).to_have_url(f"{paged.url}{THING}?tab=usage&page=4")
    expect_usage_page(page, 4)
    let_through(held[0])
    page.go_back()
    expect(page).to_have_url(f"{paged.url}{THING}?tab=usage&page=2")
    expect_usage_page(page, 2)


@pytest.mark.parametrize("name", ["Previous", "Next"])
@pytest.mark.parametrize("modifier", ["ControlOrMeta", "Shift", "Alt"])
def test_usage_pager_modifier_click_left_to_browser(page: Page, paged: LiveServer, modifier: str, name: str) -> None:
    """Check that Previous or Next clicked with a modifier key is left to the browser, and leaves this tab alone.

    The browser opens the page in a new browser tab or window, or, with Alt, downloads it.
    """
    page.goto(f"{paged.url}{THING}?tab=usage&page=2")
    length = page.evaluate("history.length")
    requests = record_panels(page)
    target = f"{paged.url}{THING}?tab=usage{'&page=3' if name == 'Next' else ''}"
    if modifier == "Alt":
        with page.expect_download() as download:
            pager_link(page, name).click(modifiers=[modifier])
        assert download.value.url == target
    else:
        with page.context.expect_page() as opened:
            pager_link(page, name).click(modifiers=[modifier])
        opened.value.wait_for_load_state()
        assert opened.value.url == target
    expect(page).to_have_url(f"{paged.url}{THING}?tab=usage&page=2")
    expect_usage_page(page, 2)
    assert page.evaluate("history.length") == length
    assert requests == []


@pytest.mark.parametrize("control", ["Next", "Go"])
@pytest.mark.parametrize("failure", ["status", "network"])
def test_usage_pager_failure_loads_page(page: Page, paged: LiveServer, failure: str, control: str) -> None:
    """Check that a Usage page whose panel can't be fetched is loaded instead."""
    fail_panels(page, failure)
    page.goto(f"{paged.url}{THING}?tab=usage")
    mark(page)
    if control == "Next":
        pager_link(page, "Next").click()
    else:
        go_to(page, "2")
    expect(page).to_have_url(f"{paged.url}{THING}?tab=usage&page=2")
    expect_usage_page(page, 2)
    assert not marked(page)


@pytest.mark.parametrize("failure", ["status", "network"])
def test_usage_back_failure_reloads(page: Page, paged: LiveServer, failure: str) -> None:
    """Check that a Usage page whose panel can't be fetched on Back is loaded instead."""
    page.goto(f"{paged.url}{THING}?tab=usage")
    mark(page)
    pager_link(page, "Next").click()
    expect_usage_page(page, 2)
    fail_panels(page, failure)
    with page.expect_event("load"):
        page.go_back()
    expect(page).to_have_url(f"{paged.url}{THING}?tab=usage")
    expect_usage_page(page, 1)
    assert not marked(page)


def test_usage_back_to_foreign_panel_reloads(page: Page, paged: LiveServer) -> None:
    """Check that Back to an entry whose recorded panel is on another site reloads the page rather than fetch it.

    Not left to htmx's `selfRequestsOnly` and the policy, which would refuse it, the panel unchanged (#118, C10).
    """
    page.goto(f"{paged.url}{THING}?tab=usage")
    page.evaluate("history.replaceState({ browlPanel: 'http://attacker.test/?panel=1' }, '')")
    mark(page)
    pager_link(page, "Next").click()
    expect_usage_page(page, 2)
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url))
    with page.expect_event("load"):
        page.go_back()
    assert not marked(page)
    expect(page).to_have_url(f"{paged.url}{THING}?tab=usage")
    expect_usage_page(page, 1)
    assert not [url for url in requests if "attacker" in url]


def test_hash_entity_usage_pages(page: Page, paged: LiveServer) -> None:
    """Check that a hash entity's Usage pages come from the hash route, their URLs keeping the fragment.

    Next, Go, Back and Forward each fetch their panel there (#118, S5).
    """
    page.goto(f"{paged.url}{VOCAB}?tab=usage#Gamma")
    expect(page.locator("main h1")).to_have_text(f"{EX}/vocab#Gamma")
    mark(page)
    hash_panel = {"path": [VOCAB], "fragment": ["Gamma"], "tab": ["usage"], "panel": ["1"]}
    for step, query, number in [
        (lambda: pager_link(page, "Next").click(), "?tab=usage&page=2", 2),
        (lambda: go_to(page, "4"), "?tab=usage&page=4", 4),
        (page.go_back, "?tab=usage&page=2", 2),
        (page.go_back, "?tab=usage", 1),
        (page.go_forward, "?tab=usage&page=2", 2),
    ]:
        with page.expect_request(PANEL) as request:
            step()
        assert HASH_ROUTE.search(request.value.url)
        assert query_of(request.value.url) == {**hash_panel, **({"page": [str(number)]} if number > 1 else {})}
        expect(page).to_have_url(f"{paged.url}{VOCAB}{query}#Gamma")
        expect_usage_page(page, number)
    expect(page.locator("main h1")).to_have_text(f"{EX}/vocab#Gamma")
    assert marked(page)


def test_usage_back_over_fragment_change(page: Page, paged: LiveServer) -> None:
    """Check that Back to a Usage page from another fragment's page is left to the hash-IRI script.

    The script's request comes after any the paging made, so they'd be recorded first.
    """
    page.goto(f"{paged.url}/example.org/onto?tab=usage#Alpha")
    expect(page.locator("main h1")).to_have_text(f"{EX}/onto#Alpha")
    pager_link(page, "Next").click()
    expect_usage_page(page, 2)
    page.evaluate("location.hash = 'Beta'")
    expect(page.locator("main h1")).to_have_text(f"{EX}/onto#Beta")
    mark(page)
    requests = record_panels(page)
    with page.expect_request(HASH_ROUTE):
        page.go_back()
    expect(page.locator("main h1")).to_have_text(f"{EX}/onto#Alpha")
    expect(page).to_have_url(f"{paged.url}/example.org/onto?tab=usage&page=2#Alpha")
    expect_usage_page(page, 2)
    assert requests == []
    assert marked(page)


def test_link_to_url_shown_keeps_page(page: Page, paged: LiveServer) -> None:
    """Check that a click on a link to the URL shown keeps the page, and the panel its entry records (#118, C2).

    Such as a hash entity's link to itself. Chromium handles it as a fragment navigation that replaces the entry,
    dropping its state, and fires popstate before the click returns.
    """
    page.goto(f"{paged.url}/example.org/onto?tab=usage#Alpha")
    expect(page.locator("main h1")).to_have_text(f"{EX}/onto#Alpha")
    pager_link(page, "Next").click()
    expect_usage_page(page, 2)
    mark(page)
    navigations = page.evaluate("""() => {
      const types = [];
      navigation.addEventListener("navigate", (event) => types.push(event.navigationType));
      const link = Object.assign(document.createElement("a"), { href: location.href });
      document.body.append(link);
      link.click();
      return types;
    }""")
    assert "reload" not in navigations
    assert marked(page)
    page.go_back()
    expect_usage_page(page, 1)
    with page.expect_request(PANEL) as request:
        page.go_forward()
    assert query_of(request.value.url)["page"] == ["2"]
    expect_usage_page(page, 2)
    assert marked(page)


@NO_JS
def test_usage_pager_without_javascript(page: Page, paged: LiveServer) -> None:
    """Check that, without JavaScript, Previous, Next and the form load their pages, the form's without `panel`."""
    page.goto(f"{paged.url}{THING}?tab=usage")
    pager_link(page, "Next").click()
    expect(page).to_have_url(f"{paged.url}{THING}?tab=usage&page=2")
    go_to(page, "4")
    expect(page).to_have_url(f"{paged.url}{THING}?tab=usage&page=4")
    expect_usage_page(page, 4)
    pager_link(page, "Previous").click()
    expect(page).to_have_url(f"{paged.url}{THING}?tab=usage&page=3")
    expect_usage_page(page, 3)


# The tree pages (P-18, U-7, U-17, DESIGN.md §2.13)

TREE_FRAGMENT = re.compile(r"/_browl/tree/classes\?node=")

TOP_AND_BIG = urllib.parse.urlencode([("open", f"{EX}/Top"), ("open", f"{EX}/Big")])
"""The query that opens `Top` and its child `Big`, which has two and a half pages of children."""


@pytest.fixture
def trees(live_server: Callable[[FastAPI], LiveServer]) -> LiveServer:
    """Return a live server with `tests.test_trees`' hierarchies loaded."""
    server = live_server(make_app())
    server.app.state.jobs.register(Stage.PRE_CACHING, tree_stage())
    server.call(server.app.state.jobs.start())
    server.call(server.app.state.jobs.wait())
    return server


def tree_item(page: Page, name: str) -> Locator:
    """Return the tree item with this name."""
    return page.get_by_role("treeitem", name=name, exact=True)


def tree_toggle(page: Page, name: str) -> Locator:
    """Return the toggle of the tree item with this name."""
    return page.locator("li", has=tree_item(page, name)).last.locator(":scope > .tree-toggle")


def record_fragments(page: Page) -> list[str]:
    """Return a list that collects the URLs of the page's requests for nodes' children, in order."""
    requests: list[str] = []
    page.on("request", lambda request: requests.append(request.url) if TREE_FRAGMENT.search(request.url) else None)
    return requests


def test_tree_opens_in_place(page: Page, trees: LiveServer) -> None:
    """Check that a toggle fetches its node's children once, and then opens and closes them in place."""
    watch_policy(page)
    page.goto(f"{trees.url}/_browl/tree/classes")
    mark(page)
    requests = record_fragments(page)
    top = tree_item(page, "Top")
    expect(top).to_have_attribute("aria-expanded", "false")
    expect(tree_item(page, "Left")).to_have_count(0)
    tree_toggle(page, "Top").click()
    expect(tree_item(page, "Left")).to_be_visible()
    expect(top).to_have_attribute("aria-expanded", "true")
    assert query_of(requests[0]) == {"node": [f"{EX}/Top"], "at": ["t-1"], "open": [f"{EX}/Top"]}
    tree_toggle(page, "Top").click()
    expect(tree_item(page, "Left")).to_be_hidden()
    expect(top).to_have_attribute("aria-expanded", "false")
    tree_toggle(page, "Top").click()
    expect(tree_item(page, "Left")).to_be_visible()
    assert len(requests) == 1
    assert marked(page)
    expect(page).to_have_url(f"{trees.url}/_browl/tree/classes")
    assert violations(page) == []


def test_tree_keyboard(page: Page, trees: LiveServer) -> None:
    """Check the WAI-ARIA tree pattern's keys: up, down, Home and End move; right opens, then enters; left leaves.

    Only one item is in the Tab sequence, and the toggles are in none.
    """
    page.goto(f"{trees.url}/_browl/tree/classes")
    page.locator(".sign-in").focus()  # the last stop before the tree
    page.keyboard.press("Tab")
    expect(tree_item(page, "Free")).to_be_focused()
    requests = record_fragments(page)
    steps = [
        ("ArrowDown", "Top"),
        ("ArrowRight", "Top"),  # opens it, fetching its children
        ("ArrowRight", "Big"),
        ("ArrowDown", "Left"),
        ("End", "Right"),
        ("ArrowLeft", "Top"),
        ("Home", "Free"),
        ("ArrowUp", "Free"),
        ("ArrowLeft", "Free"),  # a root: nowhere to go
        ("ArrowDown", "Top"),
        ("ArrowLeft", "Top"),  # closes it
        ("End", "Top"),
    ]
    for key, name in steps:
        page.keyboard.press(key)
        expect(tree_item(page, name)).to_be_focused()
        if (key, name) == ("ArrowRight", "Top"):
            expect(tree_item(page, "Top")).to_have_attribute("aria-expanded", "true")
    assert len(requests) == 1
    expect(tree_item(page, "Top")).to_have_attribute("aria-expanded", "false")
    expect(page.locator('[role="tree"] [tabindex="0"]')).to_have_count(1)
    expect(tree_item(page, "Top")).to_have_attribute("tabindex", "0")
    expect(page.locator(".tree-toggle").first).to_have_attribute("tabindex", "-1")
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    expect(tree_item(page, "Top")).to_be_focused()  # back on the roving item
    page.keyboard.press("Enter")
    expect(page).to_have_url(f"{trees.url}/Top")


def test_tree_close_moves_roving_item(page: Page, trees: LiveServer) -> None:
    """Check that closing a node that holds the item in the Tab sequence moves that to the node."""
    page.goto(f"{trees.url}/_browl/tree/classes?open={urllib.parse.quote(EX + '/Top')}")
    tree_item(page, "Left").focus()
    tree_toggle(page, "Top").click()
    expect(tree_item(page, "Left")).to_be_hidden()
    expect(tree_item(page, "Top")).to_have_attribute("tabindex", "0")
    expect(page.locator('[role="tree"] [tabindex="0"]')).to_have_count(1)
    page.locator(".sign-in").focus()
    page.keyboard.press("Tab")
    expect(tree_item(page, "Top")).to_be_focused()


@NO_JS
def test_tree_without_javascript(page: Page, trees: LiveServer) -> None:
    """Check that, without JavaScript, a toggle loads the page with its node open or closed (U-17)."""
    page.goto(f"{trees.url}/_browl/tree/classes")
    tree_toggle(page, "Top").click()
    expect(tree_item(page, "Left")).to_be_visible()
    assert query_of(page.url) == {"open": [f"{EX}/Top"]}
    assert urllib.parse.urlsplit(page.url).fragment == "t-1"
    tree_toggle(page, "Right").click()
    expect(tree_item(page, "Bottom")).to_be_visible()
    assert query_of(page.url) == {"open": [f"{EX}/Top", f"{EX}/Right"]}
    tree_toggle(page, "Top").click()
    expect(tree_item(page, "Left")).to_have_count(0)
    assert query_of(page.url) == {"open": [f"{EX}/Right"]}


def test_tree_show_more_in_place(page: Page, trees: LiveServer) -> None:
    """Check that "Show more" adds the next page of children in place, the focus moving to its first."""
    page.goto(f"{trees.url}/_browl/tree/classes?{TOP_AND_BIG}")
    mark(page)
    more = page.get_by_role("treeitem", name="Show 100 more")
    with page.expect_request(TREE_FRAGMENT):
        more.click()
    expect(tree_item(page, "Kid 100")).to_be_focused()
    expect(tree_item(page, "Kid 100")).to_have_attribute("tabindex", "0")
    expect(page.get_by_role("treeitem", name="Show 50 more")).to_be_visible()
    expect(page.locator('[role="tree"] [tabindex="0"]')).to_have_count(1)
    page.keyboard.press("ArrowUp")
    expect(tree_item(page, "Kid 099")).to_be_focused()
    assert marked(page)


@NO_JS
def test_tree_show_more_without_javascript(page: Page, trees: LiveServer) -> None:
    """Check that, without JavaScript, "Show more" loads the page with the next page of children, and a link back."""
    page.goto(f"{trees.url}/_browl/tree/classes?{TOP_AND_BIG}")
    page.get_by_role("treeitem", name="Show 100 more").click()
    expect(tree_item(page, "Kid 100")).to_be_visible()
    expect(tree_item(page, "Kid 099")).to_have_count(0)
    page.get_by_role("treeitem", name="Show the previous 100").click()
    expect(tree_item(page, "Kid 099")).to_be_visible()


@pytest.mark.parametrize("failure", ["status", "network"])
def test_tree_fragment_failure_loads_page(page: Page, trees: LiveServer, failure: str) -> None:
    """Check that a node whose children can't be fetched loads the page with it open instead."""
    if failure == "status":
        page.route(TREE_FRAGMENT, lambda route: route.fulfill(status=503, content_type="text/html", body="<p>No</p>"))
    else:
        page.route(TREE_FRAGMENT, lambda route: route.abort())
    page.goto(f"{trees.url}/_browl/tree/classes")
    tree_toggle(page, "Top").click()
    expect(tree_item(page, "Left")).to_be_visible()
    assert query_of(page.url) == {"open": [f"{EX}/Top"]}


def test_tree_modifier_click_left_to_browser(page: Page, trees: LiveServer) -> None:
    """Check that a toggle clicked with a modifier key opens its page in a new browser tab, and leaves this one."""
    page.goto(f"{trees.url}/_browl/tree/classes")
    with page.context.expect_page() as opened:
        tree_toggle(page, "Top").click(modifiers=["ControlOrMeta"])
    opened.value.wait_for_load_state()
    assert query_of(opened.value.url) == {"open": [f"{EX}/Top"]}
    expect(tree_item(page, "Top")).to_have_attribute("aria-expanded", "false")
