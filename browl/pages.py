"""Templates, static files, and the gate that serves the "loading" and "not available" pages (U-16).

The gate is a plain ASGI middleware, not a route dependency, so it also covers paths no route matches, and it leaves
request bodies alone, so admin uploads stream through it untouched.
"""

from pathlib import Path

import jinja2
from starlette.requests import Request
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Receive, Scope, Send

from browl.jobs import Site

PACKAGE_DIR = Path(__file__).parent
STATIC_DIR = PACKAGE_DIR / "static"

RETRY_AFTER_S = 10
"""How soon a client should retry while a load runs: the loading page's `Retry-After` and its refresh interval."""


def file_size(size: int) -> str:
    """Return a size in bytes for people to read, in decimal units, e.g. "412.3 MB"."""
    if size < 1000:
        return f"{size} B"
    value = float(size)
    for unit in ("kB", "MB", "GB"):
        value /= 1000
        if value < 999.95 or unit == "GB":
            return f"{value:.1f} {unit}"
    raise AssertionError("unreachable")  # pragma: no cover


templates = Jinja2Templates(
    env=jinja2.Environment(
        loader=jinja2.FileSystemLoader(PACKAGE_DIR / "templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    )
)
templates.env.filters["file_size"] = file_size


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
        headers = {"Cache-Control": "no-store"}
        if site == Site.LOADING:
            headers["Retry-After"] = str(RETRY_AFTER_S)
            template, context = "loading.html", {"refresh_s": RETRY_AFTER_S}
        else:
            template, context = "not_available.html", {}
        response = templates.TemplateResponse(request, template, context, status_code=503, headers=headers)
        await response(scope, receive, send)
