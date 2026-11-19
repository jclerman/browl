"""Templates, the page frame, static files, the "loading" and "not available" pages (U-16), and derived reads.

The gate and the frame are plain ASGI middleware, not route dependencies, so they also cover paths no route matches,
and they leave request bodies alone, so admin uploads stream through them untouched.

Pages read whole-ontology data only from the derived database (N-4), through `read_derived`.
"""

import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import jinja2
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import Response
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Receive, Scope, Send

from browl.derived import DerivedReader, EntityType, NotCurrentError, entities_with_iri, read_meta
from browl.jobs import Site

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).parent
STATIC_DIR = PACKAGE_DIR / "static"

RETRY_AFTER_S = 10
"""How soon a client should retry while a load runs: the loading page's `Retry-After` and its refresh interval."""


MAX_NAME_CHARS = 100
"""The longest ontology name the navigation bar shows; a longer one is cut short (DESIGN.md §2.9)."""

TREES = (
    ("classes", "Classes"),
    ("object-properties", "Object properties"),
    ("data-properties", "Data properties"),
    ("annotation-properties", "Annotation properties"),
)
"""Each tree page's path segment under `tree/` (DESIGN.md §2.13) and its link text, in the navigation bar's order."""

CONTENT_SECURITY_POLICY = "script-src 'self'; object-src 'none'; base-uri 'none'"
"""The `Content-Security-Policy` of browl's own pages: no inline scripts or event handlers.

The hash-IRI script moves a fetched page's `<main>` into the live page, where inline handlers in it would run, so this
backs up autoescaping there.
"""


@dataclass(frozen=True)
class Home:
    """The served ontology's name and home page, for the navigation bar.

    Attributes:
        name: Its display name, else its IRI, cut to `MAX_NAME_CHARS`; None if it has neither.
        path: The home page's path, from the app's root; None if the ontology has no page.
        fragment: The home page's fragment, if its IRI is a hash IRI.
    """

    name: str | None = None
    path: str | None = None
    fragment: str | None = None


NO_HOME = Home()


def read_home(connection: sqlite3.Connection) -> Home:
    """Return the served ontology's name and home page from its derived database."""
    meta = read_meta(connection)
    name = meta.title or meta.ontology_iri
    if name is not None and len(name) > MAX_NAME_CHARS:
        name = f"{name[: MAX_NAME_CHARS - 1]}…"
    if meta.ontology_iri is None:
        return Home(name)
    pages = [
        entity for entity in entities_with_iri(connection, meta.ontology_iri) if entity.type == EntityType.ONTOLOGY
    ]
    return Home(name, pages[0].path, pages[0].fragment) if pages else Home(name)


def frame_context(request: Request) -> dict[str, object]:
    """Return what the base template's frame needs: the navigation bar's links, and whether the user is an admin.

    The home page comes from `PageFrame`. P-22 marks requests from admins by setting `request.state.is_admin`.
    """
    root = link_root(request.scope)
    reserved = f"{root}/{request.app.state.settings.reserved_prefix}"
    home: Home = getattr(request.state, "home", NO_HOME)
    home_link = None
    if home.path is not None:
        home_link = f"{root}{home.path}" if home.fragment is None else f"{root}{home.path}#{home.fragment}"
    return {
        "frame": {
            "name": home.name,
            "home": home_link,
            "trees": [(f"{reserved}/tree/{slug}", label) for slug, label in TREES],
            "admin": f"{reserved}/admin/upload",
            "is_admin": getattr(request.state, "is_admin", False) is True,
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


def unavailable_page(request: Request, site: Site) -> Response:
    """Return the "loading" or "not available" page, as the gate serves it."""
    headers = {"Cache-Control": "no-store"}
    if site == Site.LOADING:
        headers["Retry-After"] = str(RETRY_AFTER_S)
        template, context = "loading.html", {"refresh_s": RETRY_AFTER_S}
    else:
        template, context = "not_available.html", {}
    return templates.TemplateResponse(request, template, context, status_code=503, headers=headers)


async def read_derived[T](request: Request, function: Callable[[sqlite3.Connection], T]) -> T:
    """Call a function with a connection to the ready generation's derived database, in the thread pool.

    Raises:
        NotCurrentError: No generation is ready, or its derived database isn't current, which the app serves as
            "loading" (`derived_not_current`).
    """
    generation = (await request.app.state.jobs.current()).ready_generation
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
        return any(is_under(path, prefix) for prefix in self.exempt)

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
    """Give each request the ontology's home (`request.state.home`), and each HTML response a `Content-Security-Policy`.

    The home is the navigation bar's ontology name and link, read once per generation. While no generation's derived
    database is current, there's none, and the navigation bar shows only browl's name.

    Args:
        app: The application it wraps.
        bare: Path prefixes passed straight through, as for `SiteGate`: routes that serve no pages, e.g. static files.
        csp_exempt: Path prefixes whose pages get no policy: pages browl doesn't build, such as the API docs.
    """

    def __init__(self, app: ASGIApp, bare: list[str], csp_exempt: list[str]) -> None:
        """Wrap the app."""
        self.app = app
        self.bare = bare
        self.csp_exempt = csp_exempt
        self._homes: dict[int, Home] = {}  # the newest generation's only

    async def home(self, request: Request) -> Home:
        """Return the ready generation's ontology home, or `NO_HOME` if no generation's derived database is current."""
        generation = (await request.app.state.jobs.current()).ready_generation
        if generation is None:
            return NO_HOME
        home = self._homes.get(generation)
        if home is None:
            try:
                home = await read_derived(request, read_home)
            except NotCurrentError:
                return NO_HOME
            self._homes = {generation: home}
        return home

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the request, with the home in its state, adding the policy to an HTML response."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = app_path(scope)
        if any(is_under(path, prefix) for prefix in self.bare):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        request.state.home = await self.home(request)
        if any(is_under(path, prefix) for prefix in self.csp_exempt):
            await self.app(scope, receive, send)
            return

        async def send_with_policy(message: Mapping[str, object]) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)  # type: ignore[arg-type]
                if headers.get("content-type", "").startswith("text/html"):
                    headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            await send(message)  # type: ignore[arg-type]

        await self.app(scope, receive, send_with_policy)
