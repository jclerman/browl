"""Admin routes, under `/{RESERVED_PREFIX}/admin` (DESIGN.md §2.13). Authentication isn't enforced yet (P-22)."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.openapi.docs import get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from browl.jobs import JobRunningError, Reasoner, Stage
from browl.pages import templates
from browl.upload import FORMATS, StorageError, UploadError, receive_upload

STATUS_POLL_S = 2
"""How often the status fragment polls itself while a job runs."""

SWAGGER_UI_URL = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.35.0"
"""Swagger UI for the API docs, pinned to an exact version: an accepted exception to the no-CDN rule (§2.13)."""

router = APIRouter(prefix="/admin")


@router.get("/status", response_class=HTMLResponse)
async def job_status(request: Request) -> HTMLResponse:
    """Show the load job's stage, start time, and failed stage and reason (A-5, A-6).

    An htmx request gets just the status fragment, which the upload page embeds; while a job runs, the fragment polls
    this route and replaces itself. Any other request gets the fragment in a page, which, while a job runs, polls the
    same way, or refreshes itself when JavaScript is off.
    """
    template = "admin/_job_status.html" if request.headers.get("HX-Request") == "true" else "admin/status.html"
    return templates.TemplateResponse(
        request,
        template,
        {"job": await request.app.state.jobs.current(), "stages": list(Stage), "poll_s": STATUS_POLL_S},
        headers={"Cache-Control": "no-store"},
    )


REASONER_LABELS = {Reasoner.NONE: "None", Reasoner.ELK: "ELK", Reasoner.HERMIT: "HermiT"}
"""The upload form's reasoner choices (A-3), the first the default."""


async def _upload_page(request: Request, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin/upload.html",
        {
            "job": await request.app.state.jobs.current(),
            "stages": list(Stage),
            "poll_s": STATUS_POLL_S,
            "reasoners": list(REASONER_LABELS.items()),
            "accept": ",".join(sorted(FORMATS)),
            "error": error,
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request) -> HTMLResponse:
    """Show the upload form (A-2, A-3) and the load job's status (A-5, A-6)."""
    return await _upload_page(request)


def same_origin(request: Request) -> None:
    """Refuse a request a browser sent from another site's page, e.g. one submitting the upload form (CSRF).

    Browsers say where a request came from in `Sec-Fetch-Site`; a request without it, e.g. from curl, passes. It's
    checked before the body is read, so it suits the streaming upload.

    Raises:
        HTTPException: 403, the request came from another site.
    """
    if request.headers.get("Sec-Fetch-Site", "none") not in {"same-origin", "none"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "requests from other sites are refused")


@router.post("/upload", response_class=HTMLResponse, dependencies=[Depends(same_origin)])
async def upload(request: Request) -> Response:
    """Start a load job that replaces the ontology with the uploaded file, streamed to the data volume (A-2, A-4).

    The body is a multipart form: a `reasoner` field (`none`, `elk` or `hermit`), then an `ontology` file (`.ttl`,
    `.rdf`, `.owl` as RDF/XML, or `.ofn`). It's read as it streams in, so FastAPI can't describe it.

    On success, redirects to the upload page, which shows the job's progress; a refused upload gets the page with the
    reason: 400 if the request was invalid, 403 if it came from another site's page, 409 if a job was already running,
    413 if it's larger than `MAX_UPLOAD_BYTES`, 507 if the data volume is full or would be, 500 if the file couldn't
    be stored for another reason.
    """
    # A refusal before the body has been read (e.g. a bad suffix, or 409) responds while the browser may still be
    # sending a large file. Some browsers might then show a connection reset instead of the page (review C3); curl and
    # a client that sends the whole body first both got the page from uvicorn, so it's unconfirmed. If it shows up,
    # read and discard the rest of the body, up to a cap, before responding.
    try:
        await receive_upload(request, request.app.state.jobs)
    except UploadError as error:
        return await _upload_page(request, str(error), error.status_code)
    except StorageError as error:
        code = status.HTTP_507_INSUFFICIENT_STORAGE if error.no_space else status.HTTP_500_INTERNAL_SERVER_ERROR
        return await _upload_page(request, str(error), code)
    except JobRunningError:
        return await _upload_page(request, "a load job is already running", status.HTTP_409_CONFLICT)
    return RedirectResponse(request.url_for("upload_page").path, status.HTTP_303_SEE_OTHER)


@router.get("/openapi.json", include_in_schema=False)
async def openapi(request: Request) -> JSONResponse:
    """Return the OpenAPI schema of the app's API, with the root path as its server, as FastAPI's own route does.

    Without it, Swagger UI's "Try it out" would call paths missing the root path when the app is served under one.
    """
    schema = request.app.openapi()
    root_path = request.scope.get("root_path", "").rstrip("/")
    servers = schema.get("servers", [])
    if root_path and root_path not in {server.get("url") for server in servers}:
        schema = {**schema, "servers": [{"url": root_path}, *servers]}
    return JSONResponse(schema)


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
