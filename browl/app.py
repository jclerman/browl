"""The FastAPI application. Run it with one worker: `uvicorn browl.app:create_app --factory --workers 1`."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import httpx2
from fastapi import APIRouter, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import browl
from browl import admin, owlapi
from browl.fuseki import FusekiClient
from browl.jobs import JobRunner
from browl.pages import STATIC_DIR, SiteGate
from browl.settings import Settings, load_settings

FUSEKI_TIMEOUT_S = 2.0
"""Time limit for the health check's whole call to Fuseki, well under the container health check's own."""


class Health(BaseModel):
    """Response of the health route.

    Attributes:
        status: Always "ok": the app is up, whatever Fuseki's state.
        fuseki: Whether Fuseki answered its ping.
    """

    status: Literal["ok"] = "ok"
    fuseki: Literal["up", "down"]


router = APIRouter()


@router.get("/health")
async def health(request: Request) -> Health:
    """Report that the app is up, and whether Fuseki answers.

    A Fuseki outage doesn't fail the check: the app stays up to serve its "not available" page (U-16), and Fuseki's
    container has its own health check.
    """
    client: httpx2.AsyncClient = request.app.state.fuseki_http
    try:
        # httpx2's own timeout limits each phase (connect, write, read) separately, not the whole call.
        async with asyncio.timeout(FUSEKI_TIMEOUT_S):
            response = await client.get("/$/ping")
    except (httpx2.HTTPError, TimeoutError):
        return Health(fuseki="down")
    return Health(fuseki="up" if response.is_success else "down")


def create_app(
    settings: Settings | None = None,
    fuseki_transport: httpx2.AsyncBaseTransport | None = None,
    start_jvm: bool = True,
) -> FastAPI:
    """Build the application, loading the settings from the environment unless given.

    On startup the app starts the JVM (DESIGN.md §2.1), opens its HTTP client for Fuseki, and opens the load job
    runner (`app.state.jobs`) on its database in `DATA_DIR`. Uvicorn runs the startup on the main thread, where the JVM
    must start; a test client doesn't, so tests start it first. On shutdown a running load job is cancelled. Every page
    but the health, static and admin routes, and FastAPI's API docs, is gated on the job state (`SiteGate`).

    Args:
        settings: The settings; loaded from the environment if not given.
        fuseki_transport: The transport for the Fuseki client, which tests replace; httpx2's default if not given.
        start_jvm: Whether to start the JVM; tests that don't need it turn it off.

    Raises:
        pydantic.ValidationError: A setting is missing or invalid.
    """
    if settings is None:
        settings = load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if start_jvm:
            owlapi.start_jvm(settings.robot_jar, settings.app_jvm_max_heap)
        async with httpx2.AsyncClient(base_url=str(settings.fuseki_url), transport=fuseki_transport) as client:
            app.state.fuseki_http = client
            app.state.sparql = FusekiClient(client)
            app.state.jobs = await JobRunner.open(settings)
            try:
                yield
            finally:
                await app.state.jobs.close()

    app = FastAPI(title=browl.DESCRIPTIVE_TITLE, version=browl.__version__, lifespan=lifespan)
    app.state.settings = settings
    reserved = f"/{settings.reserved_prefix}"
    app.include_router(router, prefix=reserved)
    app.include_router(admin.router, prefix=reserved)
    app.mount(f"{reserved}/static", StaticFiles(directory=STATIC_DIR), name="static")
    docs = [app.openapi_url, app.docs_url, app.redoc_url]
    exempt = [f"{reserved}/health", f"{reserved}/static", f"{reserved}/admin", *(url for url in docs if url)]
    app.add_middleware(SiteGate, exempt=exempt)
    return app
