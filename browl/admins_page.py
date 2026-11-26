"""The admins page, under `/{RESERVED_PREFIX}/admin`: list the admins, and add and remove them (A-7; DESIGN.md §2.11).

Seed admins, from `SEED_ADMINS`, are listed but can't be removed here. The others are in the persistent database's
`admin` table (`browl.admins.AdminStore`), read on every request, so a change takes effect at once.
"""

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from browl.admins import AdminStore, canonical_email, parse_email
from browl.auth import require_admin
from browl.pages import route_href, same_origin, templates
from browl.settings import Settings

logger = logging.getLogger(__name__)

MAX_FORM_BYTES = 4096
"""The largest request body the admins page's forms accept: one email address, with room to spare."""

# FastAPI parses a `Form()` body before the dependencies run, so before `require_admin` and `same_origin`; the caps
# bound what anyone, signed in or not, can make it read: a body over `MAX_FORM_BYTES` gets 413, and a file part 400.
router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(require_admin)],
    max_body_size=MAX_FORM_BYTES,
    form_limits={"max_files": 0},
)
"""The admins page's routes; every one needs an admin signed in."""


@dataclass(frozen=True)
class AdminRow:
    """An admin, as the admins page lists them.

    Attributes:
        email: Their email address, in its canonical form.
        seed: Whether they're a seed admin, who can't be removed here; one also in the `admin` table is listed once,
            as a seed admin.
        added_at: When they were added here; None for a seed admin.
        added_by: Who added them; None for a seed admin, or one added with `AUTH_DISABLED`.
    """

    email: str
    seed: bool
    added_at: datetime | None = None
    added_by: str | None = None


async def _admins_page(
    request: Request, error: str | None = None, entered: str = "", status_code: int = status.HTTP_200_OK
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    store: AdminStore = request.app.state.admins
    seeds = settings.seed_admins
    added = [
        AdminRow(admin.email, False, admin.added_at, admin.added_by)
        for admin in await asyncio.to_thread(store.admins)
        if admin.email not in seeds
    ]
    return templates.TemplateResponse(
        request,
        "admin/admins.html",
        {
            "rows": [AdminRow(email, True) for email in seeds] + added,
            "you": request.state.user,
            "auth_disabled": settings.auth_disabled,
            "error": error,
            "entered": entered,
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _back(request: Request) -> RedirectResponse:
    return RedirectResponse(route_href(request, "admins_page"), status.HTTP_303_SEE_OTHER)


@router.get("/admins", response_class=HTMLResponse)
async def admins_page(request: Request) -> HTMLResponse:
    """List the admins, seed admins first, with a form to add one and a button to remove each other one (A-7)."""
    return await _admins_page(request)


@router.post("/admins", response_class=HTMLResponse, dependencies=[Depends(same_origin)])
async def add_admin(request: Request, email: Annotated[str, Form()] = "") -> Response:
    """Add an admin by email address, then show the list; an invalid address, or an admin's, is refused (A-7)."""
    try:
        address = parse_email(email)
    except ValueError:
        message = f"“{email.strip()}” isn't an email address." if email.strip() else "Enter an email address."
        return await _admins_page(request, message, email, status.HTTP_422_UNPROCESSABLE_CONTENT)
    settings: Settings = request.app.state.settings
    if address in settings.seed_admins:
        return await _admins_page(request, f"{address} is a seed admin already.", email, status.HTTP_409_CONFLICT)
    store: AdminStore = request.app.state.admins
    try:
        added = await asyncio.to_thread(store.add, address, request.state.user, datetime.now(UTC))
    except sqlite3.Error:
        logger.exception("Couldn't add an admin to %s", store.path)
        message = f"Couldn't add {address}: the database couldn't be written."
        return await _admins_page(request, message, email, status.HTTP_500_INTERNAL_SERVER_ERROR)
    if not added:
        return await _admins_page(request, f"{address} is an admin already.", email, status.HTTP_409_CONFLICT)
    logger.info("Admin added: %s, by %s", address, request.state.user)
    return _back(request)


@router.post("/admins/remove", response_class=HTMLResponse, dependencies=[Depends(same_origin)])
async def remove_admin(request: Request, email: Annotated[str, Form()] = "") -> Response:
    """Remove an admin, then show the list; a seed admin is refused (A-7).

    One already removed, e.g. by another admin meanwhile, is no error. An admin who removes themselves is refused
    the admin pages from their next request.
    """
    address = canonical_email(email.strip())
    settings: Settings = request.app.state.settings
    if address in settings.seed_admins:
        message = f"{address} is a seed admin, set in SEED_ADMINS, so can't be removed here."
        return await _admins_page(request, message, status_code=status.HTTP_409_CONFLICT)
    store: AdminStore = request.app.state.admins
    try:
        removed = await asyncio.to_thread(store.remove, address)
    except sqlite3.Error:
        logger.exception("Couldn't remove an admin from %s", store.path)
        message = f"Couldn't remove {address}: the database couldn't be written."
        return await _admins_page(request, message, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    if removed:
        logger.info("Admin removed: %s, by %s", address, request.state.user)
    return _back(request)
