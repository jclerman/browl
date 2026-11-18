"""Templates, static files, the gate that serves the "loading" and "not available" pages (U-16), and derived reads.

The gate is a plain ASGI middleware, not a route dependency, so it also covers paths no route matches, and it leaves
request bodies alone, so admin uploads stream through it untouched.

Pages read whole-ontology data only from the derived database (N-4), through `read_derived`.
"""

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

import jinja2
from starlette.requests import Request
from starlette.responses import Response
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Receive, Scope, Send

from browl.derived import DerivedReader, NotCurrentError
from browl.jobs import Site

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).parent
STATIC_DIR = PACKAGE_DIR / "static"

RETRY_AFTER_S = 10
"""How soon a client should retry while a load runs: the loading page's `Retry-After` and its refresh interval."""


templates = Jinja2Templates(
    env=jinja2.Environment(
        loader=jinja2.FileSystemLoader(PACKAGE_DIR / "templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    )
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
        return any(path == prefix or path.startswith(f"{prefix}/") for prefix in self.exempt)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the request, or the page for the site's state."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        root_path: str = scope.get("root_path", "")
        if root_path and path.startswith(root_path):
            path = path[len(root_path) :]
        request = Request(scope)
        site = (await request.app.state.jobs.current()).site
        if site == Site.AVAILABLE or self.is_exempt(path):
            await self.app(scope, receive, send)
            return
        await unavailable_page(request, site)(scope, receive, send)
