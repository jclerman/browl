"""Templates, the page frame, static files, the "loading" and "not available" pages (U-16), and derived reads.

The gate and the frame are plain ASGI middleware, not route dependencies, so they also cover paths no route matches,
and they leave request bodies alone, so admin uploads stream through them untouched.

Pages read whole-ontology data only from the derived database (N-4), through `read_derived`.
"""

import logging
import re
import sqlite3
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import NamedTuple

import jinja2
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import Response
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from browl.derived import DerivedReader, EntityType, NotCurrentError, entities_with_iri, read_meta
from browl.jobs import JobState, Site
from browl.search import MAX_QUERY_CHARS

logger = logging.getLogger(__name__)


class TreePage(NamedTuple):
    """A hierarchy's tree page.

    Attributes:
        slug: Its path segment under `tree/`.
        label: Its link text.
        hierarchy: Its hierarchy.
    """

    slug: str
    label: str
    hierarchy: EntityType


PACKAGE_DIR = Path(__file__).parent
STATIC_DIR = PACKAGE_DIR / "static"

RETRY_AFTER_S = 10
"""How soon a client should retry while a load runs: the loading page's `Retry-After` and its refresh interval."""


MAX_NAME_CHARS = 100
"""The longest name pages show; a longer one is cut short (`cut_name`, DESIGN.md §2.9)."""

MAX_IRI_CHARS = 200
"""The longest IRI a page shows as text; a longer one is cut short (`cut_text`, DESIGN.md §2.8), and isn't a link to
itself, which could hide where it goes."""

MAX_LITERAL_CHARS = 1_000
"""The longest literal an axiom shows, or other text between its IRIs and keywords; a longer one is cut short."""

MAX_LANGUAGE_CHARS = 35
"""The longest language tag a page gives as an element's `lang`, BCP 47's practical limit (RFC 5646 §4.4.1); a longer
one is shown cut short, and not given."""

SITE_NAME = "browl"
"""browl's name, which page titles end with when the ontology has no title."""

TREES = (
    TreePage("classes", "Classes", EntityType.CLASS),
    TreePage("object-properties", "Object properties", EntityType.OBJECT_PROPERTY),
    TreePage("data-properties", "Data properties", EntityType.DATA_PROPERTY),
    TreePage("annotation-properties", "Annotation properties", EntityType.ANNOTATION_PROPERTY),
)
"""Each hierarchy's tree page (DESIGN.md §2.13), in the navigation bar's order."""

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none';"
    " form-action 'self'; frame-ancestors 'none'"
)
"""The `Content-Security-Policy` of browl's own pages (DESIGN.md §2.9)."""

SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}
"""Headers every response gets, including those the policy exempts."""


@dataclass(frozen=True)
class OntologyLink:
    """The served ontology's name and home page, for the navigation bar.

    Attributes:
        name: Its display name, else its IRI, cut short (`cut_name`); None if it has neither.
        path: The home page's path, from the app's root; None if the ontology has no page.
        fragment: The home page's fragment, if its IRI is a hash IRI.
        title: Its display name, cut short; None if it has none. Page titles end with it.
    """

    name: str | None = None
    path: str | None = None
    fragment: str | None = None
    title: str | None = None


NO_LINK = OntologyLink()


def cut_text(text: str, limit: int) -> str:
    """Return text cut to `limit` characters, ending with `…` if it was longer."""
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def cut_name(name: str) -> str:
    """Return a name cut to `MAX_NAME_CHARS`, ending with `…` if it was longer: labels can be huge (DESIGN.md §2.9)."""
    return cut_text(name, MAX_NAME_CHARS)


def read_ontology_link(connection: sqlite3.Connection) -> OntologyLink:
    """Return the served ontology's name and home page from its derived database."""
    meta = read_meta(connection)
    title = cut_name(meta.title) if meta.title else None
    name = title or (cut_name(meta.ontology_iri) if meta.ontology_iri else None)
    entities = entities_with_iri(connection, meta.ontology_iri) if meta.ontology_iri is not None else []
    page = next((entity for entity in entities if entity.type == EntityType.ONTOLOGY), None)
    if page is None:
        return OntologyLink(name, title=title)
    return OntologyLink(name, page.path, page.fragment, title)


def is_web_iri(iri: str) -> bool:
    """Return whether an IRI is an `http` or `https` one, which a page may link to (DESIGN.md §2.9).

    Examples:
        >>> is_web_iri("HTTPS://example.org/a"), is_web_iri("javascript:alert(1)")
        (True, False)
    """
    return iri.lower().startswith(("http://", "https://"))


_BIDI_CONTROLS = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def bidi_encoded(iri: str) -> str:
    """Return an IRI with its bidirectional-text controls percent-encoded, so it's shown in the order it's read.

    `<code dir="ltr">` isolates an IRI from the text around it, but not from its own controls, with which
    `https://evil.example/\u202emoc.elgoog` would read like another host (DESIGN.md §2.9). A browser encodes them in
    an `href` too.

    Examples:
        >>> bidi_encoded("https://evil.example/\u202emoc.elgoog")
        'https://evil.example/%E2%80%AEmoc.elgoog'
    """
    return _BIDI_CONTROLS.sub(lambda match: urllib.parse.quote(match.group()), iri)


def isolate(text: str) -> str:
    """Return text wrapped in U+2068 … U+2069, isolating it from the text around it (DESIGN.md §2.9).

    For plain text, such as `<title>`, which can't hold `<bdi>`.
    """
    return f"\u2068{text}\u2069"


def join_link(root: str, path: str, fragment: str | None = None) -> str:
    """Return a root-relative link to a path from the app's root (`link_root`), with a fragment if given."""
    return f"{root}{path}" if fragment is None else f"{root}{path}#{fragment}"


def href(request: Request, path: str, fragment: str | None = None) -> str:
    """Return a root-relative link to a path from the app's root, with a fragment if given."""
    return join_link(link_root(request.scope), path, fragment)


def frame_context(request: Request) -> dict[str, object]:
    """Return what the base template's frame needs: the navigation bar's links, the title's end, and the user's role.

    Page titles end with the ontology's title, isolated, else `SITE_NAME` (`site_name`).

    The ontology's link comes from `PageFrame`. P-22 marks requests from admins by setting `request.state.is_admin`.
    A tree page sets `request.state.current_tree` to its slug, so the bar marks its link.
    """
    reserved = f"/{request.app.state.settings.reserved_prefix}"
    link: OntologyLink = getattr(request.state, "ontology_link", NO_LINK)
    current = getattr(request.state, "current_tree", None)
    return {
        "frame": {
            "name": link.name,
            "home": None if link.path is None else href(request, link.path, link.fragment),
            "trees": [
                (href(request, f"{reserved}/tree/{tree.slug}"), tree.label, tree.slug == current) for tree in TREES
            ],
            "current_tree": next((tree.label for tree in TREES if tree.slug == current), None),
            "search": href(request, f"{reserved}/search"),
            "suggest": href(request, f"{reserved}/search?suggest=1"),
            "max_query_chars": MAX_QUERY_CHARS,
            "admin": href(request, f"{reserved}/admin/upload"),
            "is_admin": getattr(request.state, "is_admin", False) is True,
            "title_end": isolate(link.title) if link.title is not None else SITE_NAME,
            "site_name": SITE_NAME,
        }
    }


templates = Jinja2Templates(
    env=jinja2.Environment(
        loader=jinja2.FileSystemLoader(PACKAGE_DIR / "templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    ),
    context_processors=[frame_context],
)
templates.env.filters["isolate"] = isolate
templates.env.filters["cut_text"] = cut_text
templates.env.filters["bidi_encoded"] = bidi_encoded
templates.env.tests["web_iri"] = is_web_iri
templates.env.globals.update(
    MAX_IRI_CHARS=MAX_IRI_CHARS, MAX_LITERAL_CHARS=MAX_LITERAL_CHARS, MAX_LANGUAGE_CHARS=MAX_LANGUAGE_CHARS
)


def unavailable_page(request: Request, site: Site) -> Response:
    """Return the "loading" or "not available" page, as the gate serves it."""
    headers = {"Cache-Control": "no-store"}
    if site == Site.LOADING:
        headers["Retry-After"] = str(RETRY_AFTER_S)
        template, context = "loading.html", {"refresh_s": RETRY_AFTER_S}
    else:
        template, context = "not_available.html", {}
    return templates.TemplateResponse(request, template, context, status_code=503, headers=headers)


async def read_derived[T](
    request: Request, function: Callable[[sqlite3.Connection], T], state: JobState | None = None
) -> T:
    """Call a function with a connection to the ready generation's derived database, in the thread pool.

    Args:
        request: The request.
        function: The function.
        state: The job state whose ready generation to read, so that a page's reads all see one generation; else the
            current one.

    Raises:
        NotCurrentError: No generation is ready, or its derived database isn't current, which the app serves as
            "loading" (`derived_not_current`).
    """
    if state is None:
        state = await request.app.state.jobs.current()
    generation = state.ready_generation
    if generation is None:
        raise NotCurrentError("no generation is ready")
    reader: DerivedReader = request.app.state.derived
    return await reader.read(generation, function)


async def derived_not_current(request: Request, error: Exception) -> Response:
    """Serve "loading" for a request the gate let through while the derived database wasn't current.

    That happens in a server worker that last read the job state before another worker started rebuilding it after a
    restart (DESIGN.md §2.4), until it next reads the state.
    """
    logger.warning("Serving the loading page for %s: %s", request.url.path, error)
    return unavailable_page(request, Site.LOADING)


def link_root(scope: Scope) -> str:
    """Return the app's root path, for links: empty, or `/` and more, without a trailing `/`.

    Raises:
        ValueError: The root path is set but doesn't start with `/`, or starts with `//`, which would make links go
            to another host.
    """
    root: str = scope.get("root_path", "")
    if root and (not root.startswith("/") or root.startswith("//")):
        raise ValueError(f"The root path must be empty or start with a single /, not {root!r}")
    return root.rstrip("/")


def app_path(scope: Scope) -> str:
    """Return a request's percent-decoded path relative to the app's root, as routes match it."""
    path: str = scope["path"]
    root = link_root(scope)
    return path[len(root) :] if root and is_under(path, root) else path


def is_under_any(path: str, prefixes: Iterable[str]) -> bool:
    """Return whether a path is under any of the prefixes (`is_under`)."""
    return any(is_under(path, prefix) for prefix in prefixes)


def is_under(path: str, prefix: str) -> bool:
    """Return whether a path is a prefix, such as `/_browl`, or under it: whole path segments only."""
    return path == prefix or path.startswith(f"{prefix}/")


def clear_page_cache() -> None:
    """Empty the page cache, when a load job starts (DESIGN.md §2.4). It does nothing until the cache exists (P-21)."""


class SiteGate:
    """Serve the "loading" or "not available" page in place of every page but the exempt ones (U-16, DESIGN.md §2.13).

    Args:
        app: The application it wraps.
        exempt: Path prefixes always passed through, each a whole path or a path followed by `/`.
    """

    def __init__(self, app: ASGIApp, exempt: list[str]) -> None:
        """Wrap the app."""
        self.app = app
        self.exempt = exempt

    def is_exempt(self, path: str) -> bool:
        """Return whether a path, relative to the app's root, is passed through whatever the job state."""
        return is_under_any(path, self.exempt)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the request, or the page for the site's state."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        site = (await request.app.state.jobs.current()).site
        if site == Site.AVAILABLE or self.is_exempt(app_path(scope)):
            await self.app(scope, receive, send)
            return
        await unavailable_page(request, site)(scope, receive, send)


class PageFrame:
    """Give each request the ontology's link (`request.state.ontology_link`), and each response the security headers.

    The link is read once per generation; while it can't be read, the navigation bar shows only browl's name, and it's
    tried again after `RETRY_AFTER_S`. HTML responses also get `CONTENT_SECURITY_POLICY`.

    Args:
        app: The application it wraps.
        passthrough: Path prefixes that serve no pages, e.g. static files, so need no link.
        csp_exempt: Path prefixes whose pages get no policy: pages browl doesn't build, such as the API docs.
    """

    def __init__(self, app: ASGIApp, passthrough: list[str], csp_exempt: list[str]) -> None:
        """Wrap the app."""
        self.app = app
        self.passthrough = passthrough
        self.csp_exempt = csp_exempt
        self._cached: tuple[int, OntologyLink | None, float] | None = None  # generation, link or failure, retry time

    async def ontology_link(self, request: Request) -> OntologyLink:
        """Return the ready generation's ontology link, or `NO_LINK` if there's none or it can't be read."""
        generation = (await request.app.state.jobs.current()).ready_generation
        if generation is None:
            return NO_LINK
        cached = self._cached
        if cached is not None and cached[0] == generation and (cached[1] is not None or monotonic() < cached[2]):
            return cached[1] or NO_LINK
        reader: DerivedReader = request.app.state.derived
        link: OntologyLink | None = None
        try:
            link = await reader.read(generation, read_ontology_link)
        except NotCurrentError:
            pass
        except Exception:
            # The name is decoration: failing to read it mustn't fail the page, such as the admin's upload page.
            logger.exception("Couldn't read the ontology's name and home page for the navigation bar")
        self._cached = (generation, link, monotonic() + RETRY_AFTER_S)
        return link or NO_LINK

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the request, with the ontology's link in its state, adding the security headers."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = app_path(scope)
        if not is_under_any(path, self.passthrough):
            request = Request(scope)
            request.state.ontology_link = await self.ontology_link(request)
        policy = not is_under_any(path, self.csp_exempt)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                if policy and headers.get("content-type", "").startswith("text/html"):
                    headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            await send(message)

        await self.app(scope, receive, send_with_headers)
