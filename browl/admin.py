"""Admin routes, under `/{RESERVED_PREFIX}/admin` (DESIGN.md §2.13): only admins may use them (§2.11), but sign-in."""

import logging
from typing import Any
from urllib.parse import urlsplit

import httpx2
from authlib.integrations.starlette_client import OAuthError, StarletteOAuth2App
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.openapi.docs import get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from joserfc.errors import JoseError

from browl.auth import (
    CALLBACK_PATH,
    SESSION_EMAIL,
    SESSION_NEXT,
    auth_page,
    callback_url,
    require_admin,
    safe_next,
)
from browl.jobs import JobRunningError, Stage
from browl.jobtypes import Reasoner
from browl.pages import href, link_root, templates
from browl.settings import Settings
from browl.upload import FORMATS, StorageError, UploadError, receive_upload

logger = logging.getLogger(__name__)

STATUS_POLL_S = 2
"""How often the status fragment polls itself while a job runs."""

SWAGGER_UI_URL = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.35.0"
"""Swagger UI for the API docs, pinned to an exact version: an accepted exception to the no-CDN rule (§2.13)."""

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
"""The admin routes; every one needs an admin signed in."""

sign_in_router = APIRouter(prefix="/admin", include_in_schema=False)
"""Signing in and out, open to everyone."""


@router.get("/status", response_class=HTMLResponse)
async def job_status(request: Request) -> HTMLResponse:
    """Show the load job's stage, start time, and failed stage and reason (A-5, A-6).

    An htmx request gets just the status fragment, which polls this route while a job runs; any other request gets a
    page. A poll that finds the job ended gets `HX-Redirect` to a fresh GET of its page, so the header shows the new
    ontology (#101); a reload would re-send a refused upload's POST. Only a running job's fragment polls, so no loop.
    """
    htmx = request.headers.get("HX-Request") == "true"
    job = await request.app.state.jobs.current()
    headers = {"Cache-Control": "no-store"}
    if htmx and not job.running:
        headers["HX-Redirect"] = _polling_page(request)
    return templates.TemplateResponse(
        request,
        "admin/_job_status.html" if htmx else "admin/status.html",
        {"job": job, "stages": list(Stage), "poll_s": STATUS_POLL_S},
        headers=headers,
    )


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


def _upload_path(request: Request) -> str:
    """Return the upload page's path relative to the app's root, where signing in leads by default."""
    return f"/{request.app.state.settings.reserved_prefix}/admin/upload"


@sign_in_router.get("/sign-in")
async def sign_in(request: Request, next: str | None = None) -> Response:
    """Send the browser to Google to sign in, then back to `next`, a path relative to the app's root (A-1).

    Without `next`, or with one that isn't a path of this app, it returns to the upload page. An admin already
    signed in, or anyone with `AUTH_DISABLED`, goes straight there.
    """
    target = safe_next(next) or _upload_path(request)
    if request.state.is_admin:
        return RedirectResponse(href(request, target), status.HTTP_303_SEE_OTHER)
    client: StarletteOAuth2App = request.app.state.oidc
    request.session[SESSION_NEXT] = target
    try:
        # select_account, so someone signed in to Google with an account that isn't an admin's can pick another.
        response: Response = await client.authorize_redirect(
            request, callback_url(request.app.state.settings), prompt="select_account"
        )
    except httpx2.HTTPError:
        logger.exception("Couldn't reach Google to start signing in")
        return auth_page(request, "Sign-in failed", "Couldn't reach Google. Try again later.", 502)
    response.headers["Cache-Control"] = "no-store"
    return response


@sign_in_router.get(f"/{CALLBACK_PATH}")
async def oidc_callback(request: Request) -> Response:
    """Finish signing in: Google sends the browser back here with an authorization code (A-1).

    The ID token Google returns for the code gives the user's email address, which must be verified. Then the browser
    returns to the path the sign-in started with; it's refused there if the address isn't an admin's.
    """
    settings: Settings = request.app.state.settings
    if settings.auth_disabled:
        return RedirectResponse(href(request, _upload_path(request)), status.HTTP_303_SEE_OTHER)
    client: StarletteOAuth2App = request.app.state.oidc
    try:
        token: dict[str, Any] = await client.authorize_access_token(request)
    except (OAuthError, JoseError) as error:  # JoseError: an ID token that's invalid, e.g. with the wrong nonce
        logger.warning("Sign-in failed: %s", error)
        return auth_page(request, "Sign-in failed", "Google didn't confirm the sign-in. Try again.", 400)
    except httpx2.HTTPError:
        logger.exception("Couldn't reach Google to finish signing in")
        return auth_page(request, "Sign-in failed", "Couldn't reach Google. Try again later.", 502)
    claims: dict[str, Any] = token.get("userinfo") or {}
    email = claims.get("email")
    if not isinstance(email, str) or claims.get("email_verified") is not True:
        return auth_page(request, "Sign-in failed", "Google didn't give a verified email address.", 400)
    target = safe_next(request.session.pop(SESSION_NEXT, None)) or _upload_path(request)
    request.session[SESSION_EMAIL] = email.lower()
    logger.info("Signed in: %s", email.lower())
    return RedirectResponse(href(request, target), status.HTTP_303_SEE_OTHER, headers={"Cache-Control": "no-store"})


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


@sign_in_router.post("/sign-out", dependencies=[Depends(same_origin)])
async def sign_out(request: Request) -> RedirectResponse:
    """Sign out, and go to the site's root."""
    request.session.clear()
    return RedirectResponse(href(request, "/"), status.HTTP_303_SEE_OTHER)


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
