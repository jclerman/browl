"""Admin routes, under `/{RESERVED_PREFIX}/admin` (DESIGN.md §2.13). Authentication isn't enforced yet (P-22)."""

from fastapi import APIRouter, Request
from fastapi.openapi.docs import get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.responses import HTMLResponse, JSONResponse

from browl.jobs import Stage
from browl.pages import templates

STATUS_POLL_S = 2
"""How often the status fragment polls itself while a job runs."""

SWAGGER_UI_URL = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.35.0"
"""Swagger UI for the API docs, pinned to an exact version: an accepted exception to the no-CDN rule (§2.13)."""

router = APIRouter(prefix="/admin")


@router.get("/status", response_class=HTMLResponse)
async def job_status(request: Request) -> HTMLResponse:
    """Show the load job's stage, start time, and failed stage and reason (A-5, A-6).

    An htmx request gets just the status fragment, for the upload page (P-6) to embed; while a job runs, the fragment
    polls this route and replaces itself. Any other request gets the fragment in a page, which, while a job runs,
    refreshes itself when JavaScript is off; polling needs a page that loads htmx (P-6).
    """
    template = "admin/_job_status.html" if request.headers.get("HX-Request") == "true" else "admin/status.html"
    return templates.TemplateResponse(
        request,
        template,
        {"job": request.app.state.jobs.state, "stages": list(Stage), "poll_s": STATUS_POLL_S},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/openapi.json", include_in_schema=False)
async def openapi(request: Request) -> JSONResponse:
    """Return the OpenAPI schema of the app's API."""
    return JSONResponse(request.app.openapi())


@router.get("/docs", include_in_schema=False)
async def swagger_ui(request: Request) -> HTMLResponse:
    """Show the API docs in Swagger UI."""
    return get_swagger_ui_html(
        openapi_url=request.url_for("openapi").path,
        title=f"{request.app.title} - Swagger UI",
        swagger_js_url=f"{SWAGGER_UI_URL}/swagger-ui-bundle.js",
        swagger_css_url=f"{SWAGGER_UI_URL}/swagger-ui.css",
        oauth2_redirect_url=request.url_for("swagger_ui_redirect").path,
    )


@router.get("/docs/oauth2-redirect", include_in_schema=False)
async def swagger_ui_redirect() -> HTMLResponse:
    """Serve Swagger UI's OAuth2 redirect page."""
    return get_swagger_ui_oauth2_redirect_html()
