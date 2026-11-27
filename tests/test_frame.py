"""Tests for the page frame (P-14a, DESIGN.md §2.9): the navigation bar, stylesheet and security headers."""

import asyncio
import html.parser
import logging
import re
import sqlite3
from collections.abc import Iterator, Sequence
from time import monotonic

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import browl.pages
from browl.derived import EntityType, NotCurrentError
from browl.jobs import JobContext, Stage
from browl.pages import (
    CONTENT_SECURITY_POLICY,
    MAX_NAME_CHARS,
    PROJECT_URL,
    SECURITY_HEADERS,
    STATIC_DIR,
    TREES,
)
from tests.helpers import AUTH_ON, derived_stage, make_app

pytestmark = pytest.mark.usefixtures("data_dir")

ONTOLOGY = "http://example.org/onto"  # native under BASE_URL http://example.org/, so at /onto
ENTITIES = [
    ("http://example.org/x", EntityType.CLASS),
    ("http://other.org/y", EntityType.CLASS),  # with the next, at /other.org/y: the disambiguation page
    ("https://other.org/y", EntityType.CLASS),
    ("http://example.org/vocab#A", EntityType.CLASS),  # a hash entity, at /vocab#A
]
TREE_LINKS = [
    ("/_browl/tree/classes", "Classes"),
    ("/_browl/tree/object-properties", "Object properties"),
    ("/_browl/tree/data-properties", "Data properties"),
    ("/_browl/tree/annotation-properties", "Annotation properties"),
]
ADMIN_LINK = ("/_browl/admin/upload", "Admin")
"""The menu's last link for an admin; the tests' app has authentication off, so everyone is one."""
ADVANCED = ("/_browl/search", "Advanced search")
SIGN_IN = ("/_browl/admin/sign-in", "Admin sign-in")
CREDIT = (PROJECT_URL, "hosted with")
TOKEN_FONT_SIZES = {"var(--title-size)", "inherit", "1em", "0.8em", "0.85em"}
"""Font sizes allowed besides `var(--text-*)`: an entity's title, and sizes inherited or relative to the text around,
such as an icon's or a language tag's in an axiom annotation."""
TOKEN_RADII = {"999px", "50%", "2px"}
"""Radii allowed besides the tokens: a pill's and a circle's, and a highlighted word's, under `--radius`."""
SPACING = re.compile(r"(row-|column-)?gap|padding(-\w+)*|border(-\w+)*-radius")
"""The properties whose values must be spacing or radius tokens (DESIGN.md §2.9, Spacing)."""


class Frame(html.parser.HTMLParser):
    """A page's frame: its navigation bar's links, as (href, text), and text, and whether it has a footer.

    Attributes:
        nav: The navigation bar's links.
        nav_text: The navigation bar's text, with entities decoded.
        footer: Whether the page has a `<footer>`.
    """

    def __init__(self, page: str) -> None:
        """Parse a page."""
        super().__init__()
        self.nav: list[tuple[str, str]] = []
        self.nav_text = ""
        self.footer = False
        self._part: list[tuple[str, str]] | None = None
        self._link: tuple[str, list[str]] | None = None
        self.feed(page)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Start a part, or a link in one."""
        if tag == "header":
            self._part = self.nav
        elif tag == "footer":
            self.footer = True
        elif tag == "a" and self._part is not None:
            self._link = (dict(attrs).get("href") or "", [])

    def handle_endtag(self, tag: str) -> None:
        """End a part, or a link in one."""
        if tag == "header":
            self._part = None
        elif tag == "a" and self._link is not None and self._part is not None:
            self._part.append((self._link[0], "".join(self._link[1]).strip()))
            self._link = None

    def handle_data(self, data: str) -> None:
        """Collect a link's text, and the bar's."""
        if self._link is not None:
            self._link[1].append(data)
        if self._part is self.nav:
            self.nav_text += data


def run(
    client: TestClient,
    app: FastAPI,
    ontology: str | None = ONTOLOGY,
    title: str | None = "The <Onto>",
    entities: Sequence[tuple[str, EntityType]] = ENTITIES,
) -> None:
    """Run a load job whose derived database holds the ontology, if given, with a title, and the entities."""
    app.state.jobs.register(Stage.PRE_CACHING, derived_stage(entities, ontology, title=title))
    client.portal.call(app.state.jobs.start)
    client.portal.call(app.state.jobs.wait)


def title(client: TestClient, path: str = "/example.org/x") -> str:
    """Return the `<title>` of the page at a path, with entities decoded."""
    match = re.search(r"<title>(.*?)</title>", client.get(path).text)
    assert match is not None
    return html.unescape(match[1])


def frame(client: TestClient, path: str = "/example.org/x", **kwargs: object) -> Frame:
    """Return the frame of the page at a path."""
    return Frame(client.get(path, **kwargs).text)  # type: ignore[arg-type]


@pytest.fixture
def app() -> FastAPI:
    """Return an app with default settings."""
    return make_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Yield a test client for the app, with the ontology loaded."""
    with TestClient(app) as test_client:
        run(test_client, app)
        yield test_client


@pytest.mark.parametrize(
    "path",
    [
        "/onto",
        "/example.org/x",
        "/other.org/y",
        "/vocab",
        "/_browl/hash?path=/vocab&fragment=A",
        "/no/such/page",
        "/_browl/admin/status",
        "/_browl/admin/upload",
    ],
)
def test_frame_on_every_page(client: TestClient, path: str) -> None:
    """Check that every kind of page has the navigation bar and its links, admin last, and no in-page links or footer.

    An in-page link (`#…`) would be looked up as an entity by the hash-IRI script on a path hash entities share.
    """
    page = frame(client, path)
    assert page.nav == [("/onto", "The <Onto>"), ADVANCED, *TREE_LINKS, ADMIN_LINK, CREDIT]
    assert not page.footer
    assert not [link for link, _ in page.nav if link.startswith("#")]


@pytest.mark.parametrize("loaded", [False, True])
def test_frame_on_unavailable_pages(app: FastAPI, loaded: bool) -> None:
    """Check that "not available" has the frame and policy, without the ontology; "loading" keeps the served one's."""

    async def stall(context: JobContext) -> None:
        await asyncio.Event().wait()

    with TestClient(app) as client:
        if loaded:
            run(client, app)
            app.state.jobs.register(Stage.LOADING, stall)
            client.portal.call(app.state.jobs.start)
        response = client.get("/onto")
        assert response.status_code == 503
        assert ("Retry-After" in response.headers) is loaded
        assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
        page = Frame(response.text)
        assert page.nav[-len(TREE_LINKS) - 3 :] == [ADVANCED, *TREE_LINKS, ADMIN_LINK, CREDIT]
        assert ("The <Onto>" in page.nav_text) is loaded


@pytest.mark.parametrize("auth_disabled", [True, False])
def test_admin_link(auth_disabled: bool) -> None:
    """Check that with `AUTH_DISABLED` the bar has the admin link and no sign-in; else, signed out, the reverse.

    `tests/test_auth.py` checks the bar of a signed-in user.
    """
    app = make_app(**({} if auth_disabled else AUTH_ON))
    with TestClient(app) as client:
        run(client, app)
        nav = frame(client).nav
        assert (ADMIN_LINK in nav) is auth_disabled
        assert (SIGN_IN in nav) is not auth_disabled


def test_no_ontology_iri(app: FastAPI) -> None:
    """Check that an ontology with neither IRI nor title gets no name in the bar."""
    with TestClient(app) as client:
        run(client, app, ontology=None, title=None)
        page = frame(client)
        assert page.nav == [ADVANCED, *TREE_LINKS, ADMIN_LINK, CREDIT]
        labels = " ".join(tree.label for tree in TREES).split()
        words = ["Search", "the", "ontology", "Advanced", "search", "Browse", *labels, "Admin", "hosted", "with"]
        assert page.nav_text.split() == words


def test_ontology_without_page(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that an ontology IRI with no ontology page gets its name, linking nowhere."""
    monkeypatch.setattr(browl.pages, "entities_with_iri", lambda connection, iri: [])
    client.app.state.derived.close()  # type: ignore[attr-defined]
    run(client, client.app, title="Unlinked")  # type: ignore[arg-type]
    page = frame(client)
    assert page.nav == [ADVANCED, *TREE_LINKS, ADMIN_LINK, CREDIT]
    assert "Unlinked" in page.nav_text
    assert title(client).endswith(" · \u2068Unlinked\u2069")


@pytest.mark.parametrize("root", ["", "/site"])
def test_hash_iri_home(app: FastAPI, root: str) -> None:
    """Check that an untitled ontology is named by its IRI, and a hash IRI's home link has its fragment and root."""
    with TestClient(app, root_path=root) as client:
        run(client, app, ontology="http://example.org/vocab#onto", title=None)
        page = frame(client, f"{root}/example.org/x")
        assert page.nav[0] == (f"{root}/vocab#onto", "http://example.org/vocab#onto")


def test_long_name_cut(app: FastAPI) -> None:
    """Check that a long ontology name is cut short."""
    with TestClient(app) as client:
        run(client, app, title="x" * (MAX_NAME_CHARS + 1))
        assert frame(client).nav[0][1] == f"{'x' * (MAX_NAME_CHARS - 1)}…"
        assert title(client).endswith(f" · \u2068{'x' * (MAX_NAME_CHARS - 1)}…\u2069")


def test_new_generation_renamed(client: TestClient, app: FastAPI) -> None:
    """Check that the navigation bar shows the newly loaded ontology's name once a new load is ready."""
    assert frame(client).nav[0][1] == "The <Onto>"
    run(client, app, title="Second")
    assert frame(client).nav[0][1] == "Second"
    assert title(client).endswith(" · \u2068Second\u2069")


def test_under_root_path(app: FastAPI) -> None:
    """Check that the frame's links are within the app's root path."""
    with TestClient(app, root_path="/site") as client:
        run(client, app)
        page = frame(client, "/site/example.org/x")
        assert page.nav == [
            ("/site/onto", "The <Onto>"),
            *[(f"/site{link}", text) for link, text in [ADVANCED, *TREE_LINKS, ADMIN_LINK]],
            CREDIT,
        ]


@pytest.mark.parametrize(
    "error", [NotCurrentError("rebuilding"), sqlite3.DatabaseError("database disk image is malformed")]
)
def test_unreadable_name(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    """Check that, while the ontology's name can't be read, pages show just browl's name, and it's tried again later.

    A failure other than the database not being current is logged, and doesn't fail the page.
    """
    client.app.state.derived.close()  # type: ignore[attr-defined]  # drop the reads cached so far
    calls = []
    reader = client.app.state.derived  # type: ignore[attr-defined]
    real_read = reader.read

    async def failing_read(generation: int, function: object) -> object:
        calls.append(generation)
        raise error

    monkeypatch.setattr(reader, "read", failing_read)
    run(client, client.app, title="Second")  # type: ignore[arg-type]  # a new generation, so the name is read again
    with caplog.at_level(logging.ERROR, logger="browl.pages"):
        for path in ["/_browl/admin/upload", "/_browl/admin/status"]:
            response = client.get(path)
            assert response.status_code == 200
            assert Frame(response.text).nav == [ADVANCED, *TREE_LINKS, ADMIN_LINK, CREDIT]
            assert " · browl</title>" in response.text
    assert len(calls) == 1  # the failure is kept until RETRY_AFTER_S has passed
    assert bool(caplog.records) is not isinstance(error, NotCurrentError)
    monkeypatch.setattr(reader, "read", real_read)
    later = monotonic() + browl.pages.RETRY_AFTER_S + 1
    monkeypatch.setattr(browl.pages, "monotonic", lambda: later)
    assert frame(client).nav[0][1] == "Second"


@pytest.mark.parametrize(
    ("method", "path", "headers"),
    [
        ("GET", "/onto", {}),
        ("HEAD", "/onto", {}),
        ("GET", "/no/such/page", {}),
        ("GET", "/_browl/admin/status", {}),
        ("GET", "/_browl/admin/status", {"HX-Request": "true"}),
        ("GET", "/_browl/hash?path=/x&fragment=y", {}),
    ],
)
def test_security_headers(client: TestClient, method: str, path: str, headers: dict[str, str]) -> None:
    """Check that browl's pages and fragments get the policy and the security headers."""
    response = client.request(method, path, headers=headers)
    assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


@pytest.mark.parametrize("path", ["/_browl/admin/docs", "/_browl/static/browl.css", "/_browl/health"])
def test_exempt_from_policy(client: TestClient, path: str) -> None:
    """Check that Swagger UI (with its inline script) and non-pages get no policy, but do get the security headers."""
    response = client.get(path)
    assert "Content-Security-Policy" not in response.headers
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_htmx_served_locally(client: TestClient) -> None:
    """Check that htmx comes from the app's static files, and that pages load no script from elsewhere."""
    assert client.get("/_browl/static/htmx.min.js").status_code == 200
    page = client.get("/onto").text
    assert '<script src="/_browl/static/htmx.min.js"' in page
    assert not re.search(r"<script[^>]*src=\"(https?:)?//", page)


def test_favicon(client: TestClient) -> None:
    """Check that pages name the logo as their icon, and that the app serves it as SVG."""
    page = client.get("/onto").text
    assert '<link rel="icon" type="image/svg+xml" href="/_browl/static/browl-logo.svg">' in page
    response = client.get("/_browl/static/browl-logo.svg")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("image/svg+xml")


def test_credit(client: TestClient) -> None:
    """Check that the bar credits browl with its wordmark, linking to the project, and that the app serves it as SVG."""
    page = client.get("/onto").text
    wordmark = '<img src="/_browl/static/browl-wordmark.svg" alt="browl"'
    assert re.search(rf'<a class="hosted-with" href="{PROJECT_URL}">hosted with\s*{wordmark}', page)
    response = client.get("/_browl/static/browl-wordmark.svg")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("image/svg+xml")


def palettes() -> list[dict[str, str]]:
    """Return the stylesheet's light and dark palettes: each `:root` block's color custom properties."""
    css = (STATIC_DIR / "browl.css").read_text()
    blocks = re.findall(r":root \{([^}]*)\}", css)
    return [dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6});", block)) for block in blocks]


def contrast(foreground: str, background: str) -> float:
    """Return the WCAG contrast ratio of two `#rrggbb` colors."""

    def luminance(color: str) -> float:
        channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    high, low = sorted([luminance(foreground), luminance(background)], reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("theme", [0, 1], ids=["light", "dark"])
@pytest.mark.parametrize("text", ["fg", "muted", "accent", "danger"])
@pytest.mark.parametrize("background", ["bg", "surface", "bar-bg"])
def test_contrast(theme: int, text: str, background: str) -> None:
    """Check that each text color meets WCAG AA (4.5:1) on each background, in both themes (U-29).

    The backgrounds are the page, a card's surface and the navigation bar.
    """
    palette = palettes()[theme]
    assert contrast(palette[text], palette[background]) >= 4.5


@pytest.mark.parametrize("theme", [0, 1], ids=["light", "dark"])
@pytest.mark.parametrize("text", ["fg", "muted", "accent"])
def test_annotation_contrast(theme: int, text: str) -> None:
    """Check that each text color in an axiom annotation's box meets WCAG AA (4.5:1) on its tint, in both themes."""
    palette = palettes()[theme]
    assert contrast(palette[text], palette["annotation-bg"]) >= 4.5


@pytest.mark.parametrize("theme", [0, 1], ids=["light", "dark"])
@pytest.mark.parametrize("background", ["bg", "surface"])
def test_annotation_border(theme: int, background: str) -> None:
    """Check that an axiom annotation's border is 3:1 or more against the page and a card (WCAG 1.4.11), both themes."""
    palette = palettes()[theme]
    assert contrast(palette["annotation-border"], palette[background]) >= 3


def declarations() -> list[tuple[str, str]]:
    """Return the stylesheet's declarations as `(property, value)`, outside comments and the `:root` blocks."""
    css = re.sub(r"/\*.*?\*/", "", (STATIC_DIR / "browl.css").read_text(), flags=re.DOTALL)
    css = re.sub(r":root \{[^}]*\}", "", css)
    return [(name, value.strip()) for name, value in re.findall(r"([\w-]+)\s*:\s*([^;{}]+);", css)]


def on_scale(value: str) -> bool:
    """Return whether each part of a spacing value is 0, a custom property, `calc` of them, or em beside text."""
    parts = re.findall(r"calc\((?:[^()]|\([^()]*\))*\)|\S+", value)
    for part in parts:
        if part.startswith("calc("):
            part = re.sub(r"var\(--[\w-]+\)", "", part.removeprefix("calc(").removesuffix(")"))
            if not re.fullmatch(r"[\d.\s*/+-]*", part):
                return False
        elif not re.fullmatch(r"0|var\(--[\w-]+\)|[\d.]+em", part):
            return False
    return True


def test_font_sizes_use_tokens() -> None:
    """Check that every font size is a type token, or on the short list of exceptions (DESIGN.md §2.9, Type)."""
    sizes = {value for name, value in declarations() if name == "font-size"}
    assert {each for each in sizes if not re.fullmatch(r"var\(--text-\w+\)", each)} <= TOKEN_FONT_SIZES


def test_spacing_uses_tokens() -> None:
    """Check that every gap, padding and radius is on the spacing scale, or a named exception (DESIGN.md §2.9)."""
    values = {(name, value) for name, value in declarations() if SPACING.fullmatch(name)}
    off = {(name, value) for name, value in values if not on_scale(value)}
    assert {value for name, value in off if "radius" in name} <= TOKEN_RADII
    assert {(name, value) for name, value in off if "radius" not in name} == set()
