"""Admin routes, under `/{RESERVED_PREFIX}/admin` (DESIGN.md §2.13): only admins may use them (§2.11).

Signing in and out, under the same prefix but open to everyone, is in `browl.auth`.
"""

import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.openapi.docs import get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from browl.auth import require_admin
from browl.job_view import Activity, progress_context, usage_table_activity
from browl.jobs import JobRunningError, JobState
from browl.jobtypes import Reasoner
from browl.pages import is_htmx, link_root, same_origin, templates
from browl.upload import FORMATS, StorageError, UploadError, receive_upload
from browl.usage_tables import UsageTables

logger = logging.getLogger(__name__)

STATUS_POLL_S = 2
"""How often the status fragment polls itself while a job runs, or background work does."""

BACKGROUND_POLL = "background"
"""The query parameter of the status fragment's polls while only background work runs (`job_status`)."""

SWAGGER_UI_URL = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.35.0"
"""Swagger UI for the API docs, pinned to an exact version: an accepted exception to the no-CDN rule (§2.13)."""

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
"""The admin routes; every one needs an admin signed in."""


@router.get("/status", response_class=HTMLResponse)
async def job_status(request: Request) -> HTMLResponse:
    """Show the load job's stage, start time, and failed stage and reason (A-5, A-6).

    An htmx request gets just the status fragment, which polls this route while a job runs; any other request gets a
    page. A poll that finds the job ended gets `HX-Redirect` to a fresh GET of its page, so the header shows the new
    ontology (#101); a reload would re-send a refused upload's POST. Only a running job's fragment polls, so no loop.

    Once the job has ended, the fragment polls while background work runs, such as the usage table's build (P-27c),
    with `?background`, so such a poll swaps in the fragment rather than redirecting; the polling stops once the work
    ends.
    """
    htmx = is_htmx(request)
    job = await request.app.state.jobs.current()
    headers = {"Cache-Control": "no-store"}
    if htmx and not job.running and BACKGROUND_POLL not in request.query_params:
        headers["HX-Redirect"] = _polling_page(request)
    return templates.TemplateResponse(
        request,
        "admin/_job_status.html" if htmx else "admin/status.html",
        status_context(job, getattr(request.app.state, "usage_tables", None)),
        headers=headers,
    )


def status_context(job: JobState, tables: UsageTables | None = None) -> dict[str, Any]:
    """Return what the status fragment (`admin/_job_status.html`) renders.

    That's the job's progress, with the IRI a stage is working on (`progress_context`), and the background work.

    Args:
        job: The job state.
        tables: The usage tables, whose build of the ready generation's table shows once no job is running.
    """
    background: list[Activity] = []
    background_failed = False
    if tables is not None and not job.running and job.ready_generation is not None:
        if (progress := tables.progress(job.ready_generation)) is not None:
            background.append(usage_table_activity(progress))
        background_failed = tables.failed(job.ready_generation)
    return {
        **progress_context(job, with_iri=True),
        "background": background,
        "background_failed": background_failed,
        "background_poll": BACKGROUND_POLL,
        "poll_s": STATUS_POLL_S,
    }


def _polling_page(request: Request) -> str:
    """Return the path of the admin page an htmx poll came from (`HX-Current-URL`), or else the status page's.

    Only the path is compared, so another origin's URL with the upload page's path passes; that's harmless, as the
    result is always one of the two pages' own paths.
    """
    status_path = request.url_for("job_status").path
    current = urlsplit(request.headers.get("HX-Current-URL", "")).path
    return current if current in {status_path, request.url_for("upload_page").path} else status_path


REASONER_LABELS = {Reasoner.NONE: "None", Reasoner.ELK: "ELK", Reasoner.HERMIT: "HermiT"}
"""The upload form's reasoner choices (A-3), the first the default."""


async def _upload_page(
    request: Request, error: str | None = None, status_code: int = status.HTTP_200_OK
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin/upload.html",
        {
            **status_context(await request.app.state.jobs.current(), getattr(request.app.state, "usage_tables", None)),
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


@router.post("/upload", response_class=HTMLResponse, dependencies=[Depends(same_origin)])
async def upload(request: Request) -> Response:
    """Start a load job that replaces the ontology with the uploaded file, streamed to the jobs volume (A-2, A-4).

    The body is a multipart form: a `reasoner` field (`none`, `elk` or `hermit`), then an `ontology` file (`.ttl`,
    `.rdf`, `.owl` as RDF/XML, or `.ofn`). It's read as it streams in, so FastAPI can't describe it.

    On success, redirects to the upload page, which shows the job's progress; a refused upload gets the page with the
    reason: 400 if the request was invalid, 403 if it came from another site's page, 409 if a job was already running,
    413 if it's larger than `MAX_UPLOAD_BYTES`, 507 if the jobs volume is full or would be, 500 if the file couldn't
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
    root_path = link_root(request.scope)
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
