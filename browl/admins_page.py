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
        email: Their email address, as configured or added, lowercased.
        seed: Whether they're a seed admin, who can't be removed here; one also in the `admin` table is listed once,
            as a seed admin.
        you: Whether they're the admin viewing the page.
        added_at: When they were added here; None for a seed admin.
        added_by: Who added them; None for a seed admin, or one added with `AUTH_DISABLED`.
    """

    email: str
    seed: bool
    you: bool
    added_at: datetime | None = None
    added_by: str | None = None


async def _admins_page(
    request: Request, error: str | None = None, entered: str = "", status_code: int = status.HTTP_200_OK
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    store: AdminStore = request.app.state.admins
    you = None if request.state.user is None else canonical_email(request.state.user)
    seeds = [AdminRow(email, True, canonical_email(email) == you) for email in settings.seed_admins]
    try:
        admins = await asyncio.to_thread(store.admins)
    except sqlite3.Error:
        # Still a page, e.g. for a write that failed too, with the seed admins, who don't need the database.
        logger.exception("Couldn't read the admins from %s", store.path)
        admins = []
        error = " ".join(filter(None, [error, "The admins added here couldn't be read."]))
        status_code = max(status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
    added = [
        AdminRow(admin.email, False, canonical_email(admin.email) == you, admin.added_at, admin.added_by)
        for admin in admins
        if not settings.is_seed_admin(admin.email)
    ]
    return templates.TemplateResponse(
        request,
        "admin/admins.html",
        {
            "rows": seeds + added,
            "auth_disabled": settings.auth_disabled,
            "error": error,
            "entered": entered,
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _as_listed(address: str, listed: str) -> str:
    """Name an address typed, and the form it's listed in if that differs, e.g. a Gmail address with other dots."""
    return address if address == listed else f"{address}, listed as {listed},"


def _back(request: Request) -> RedirectResponse:
    return RedirectResponse(route_href(request, "admins_page"), status.HTTP_303_SEE_OTHER)


@router.get("/admins", response_class=HTMLResponse)
async def admins_page(request: Request) -> HTMLResponse:
    """List the admins, seed admins first, with a form to add one and a button to remove each other one (A-7)."""
    return await _admins_page(request)


@router.post("/admins", response_class=HTMLResponse, dependencies=[Depends(same_origin)])
async def add_admin(request: Request, email: Annotated[str, Form()] = "") -> Response:
    """Add an admin by email address, then show the list; an invalid address, or an admin's, is refused (A-7)."""
    text = email.strip()
    try:
        address = parse_email(text)
    except ValueError:
        message = f"“{text}” isn't an email address." if text else "Enter an email address."
        return await _admins_page(request, message, email, status.HTTP_422_UNPROCESSABLE_CONTENT)
    settings: Settings = request.app.state.settings
    if settings.is_seed_admin(address):
        seed = next(seed for seed in settings.seed_admins if canonical_email(seed) == canonical_email(address))
        message = f"{_as_listed(address, seed)} is a seed admin already."
        return await _admins_page(request, message, email, status.HTTP_409_CONFLICT)
    store: AdminStore = request.app.state.admins
    try:
        added = await asyncio.to_thread(store.add, address, request.state.user, datetime.now(UTC))
    except sqlite3.Error:
        logger.exception("Couldn't add an admin to %s", store.path)
        message = f"Couldn't add {address}: the database couldn't be written."
        return await _admins_page(request, message, email, status.HTTP_500_INTERNAL_SERVER_ERROR)
    if not added:
        try:
            admins = await asyncio.to_thread(store.admins)
        except sqlite3.Error:
            admins = []  # the page reports it
        key = canonical_email(address)
        listed = next((admin.email for admin in admins if canonical_email(admin.email) == key), address)
        message = f"{_as_listed(address, listed)} is an admin already."
        return await _admins_page(request, message, email, status.HTTP_409_CONFLICT)
    logger.info("Admin added: %s, by %s", address, request.state.user)
    return _back(request)


@router.post("/admins/remove", response_class=HTMLResponse, dependencies=[Depends(same_origin)])
async def remove_admin(request: Request, email: Annotated[str, Form()] = "") -> Response:
    """Remove an admin, then show the list; a seed admin is refused (A-7).

    One already removed, e.g. by another admin meanwhile, is no error. An admin who removes themselves is refused
    the admin pages from their next request.
    """
    address = email.strip().lower()
    settings: Settings = request.app.state.settings
    if settings.is_seed_admin(address):
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
