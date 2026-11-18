"""The FastAPI application. Run it with one worker: `uvicorn browl.app:create_app --factory --workers 1`."""

import asyncio
import functools
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Literal

import httpx2
from fastapi import APIRouter, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import browl
from browl import admin, imports, loading, owlapi, robot
from browl.fuseki import FusekiClient
from browl.jobs import JobRunner, Stage, StageFunction
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
    stages: Mapping[Stage, StageFunction] | None = None,
) -> FastAPI:
    """Build the application, loading the settings from the environment unless given.

    On startup the app starts the JVM (DESIGN.md §2.1), on the main thread, where Uvicorn runs the startup (a test
    client doesn't, so tests start it first); it opens its Fuseki client and the load job runner (`app.state.jobs`).
    On shutdown a running load job is cancelled. Pages are gated on the job state (`SiteGate`, §2.13).

    Args:
        settings: The settings; loaded from the environment if not given.
        fuseki_transport: The transport for the Fuseki client, which tests replace; httpx2's default if not given.
        start_jvm: Whether to start the JVM; tests that don't need it turn it off.
        stages: The load job's background stages; tests replace them. If not given: imports, reasoning and loading.

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
            if stages is None:
                functions: Mapping[Stage, StageFunction] = {
                    Stage.IMPORTS: imports.imports_stage,
                    Stage.REASONING: robot.reasoning_stage,
                    Stage.LOADING: functools.partial(loading.loading_stage, http=client),
                }
            else:
                functions = stages
            app.state.jobs = await JobRunner.open(settings, functions)
            try:
                yield
            finally:
                await app.state.jobs.close()

    reserved = f"/{settings.reserved_prefix}"
    # The API docs are admin routes (admin.py), not at FastAPI's default root paths, which would shadow entities.
    app = FastAPI(
        title=browl.DESCRIPTIVE_TITLE,
        version=browl.__version__,
        lifespan=lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.include_router(router, prefix=reserved)
    app.include_router(admin.router, prefix=reserved)
    app.mount(f"{reserved}/static", StaticFiles(directory=STATIC_DIR), name="static")
    exempt = [f"{reserved}/health", f"{reserved}/static", f"{reserved}/admin"]
    app.add_middleware(SiteGate, exempt=exempt)
    return app
