"""The FastAPI application. Run it with one worker: `uvicorn browl.app:create_app --factory --workers 1`."""

import asyncio
import functools
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Literal

import httpx2
from fastapi import APIRouter, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import browl
from browl import admin, derived, imports, loading, lookup, owlapi, precache, robot, trees
from browl.derived import DerivedReader, NotCurrentError
from browl.fuseki import FusekiClient, dataset_clause, ping
from browl.jobs import JobContext, JobRunner, JobState, Stage, StageFunction
from browl.pages import STATIC_DIR, PageFrame, SiteGate, derived_not_current
from browl.precache.core import has_loaded_imports
from browl.settings import Settings, load_settings
from browl.usage_tables import UsageTables
from browl.usages import UsageCounts, ready_store

logger = logging.getLogger(__name__)

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
    return Health(fuseki="up" if await ping(client, FUSEKI_TIMEOUT_S) else "down")


async def _start_usage_table(tables: UsageTables, state: JobState, settings: Settings, keys: Mapping[str, str]) -> None:
    """Build the ready generation's usage table, if it isn't current, once its derived database is."""
    if state.running:
        return  # a job's, or a rebuild's, end starts it
    try:
        generation, clause = ready_store(state)
    except NotCurrentError:
        return
    if await asyncio.to_thread(derived.is_current, settings.data_dir, generation, keys):
        await tables.start(generation, clause)


def create_app(
    settings: Settings | None = None,
    fuseki_transport: httpx2.AsyncBaseTransport | None = None,
    start_jvm: bool = True,
    stages: Mapping[Stage, StageFunction] | None = None,
    usage_tables: bool | None = None,
) -> FastAPI:
    """Build the application, loading the settings from the environment unless given.

    On startup the app starts the JVM (DESIGN.md §2.1), on the main thread, where Uvicorn runs the startup (a test
    client doesn't, so tests start it first); it opens its Fuseki client, the load job runner (`app.state.jobs`) and the
    derived database's reader (`app.state.derived`, read through `browl.pages.read_derived`). If the ready
    generation's derived database isn't current (`browl.precache.keys`), e.g. after an upgrade changed the code that
    builds it, it's rebuilt in the background (`JobRunner.rebuild`, §2.4). Its usage table (`app.state.usage_tables`)
    is built in the background once it's current, if it isn't already. On shutdown a running usage table build, load
    job or rebuild is cancelled, and the readers' connections closed. Pages are gated on the job state (`SiteGate`,
    §2.13).

    Args:
        settings: The settings; loaded from the environment if not given.
        fuseki_transport: The transport for the Fuseki client, which tests replace; httpx2's default if not given.
        start_jvm: Whether to start the JVM; tests that don't need it turn it off.
        stages: The load job's background stages; tests replace them. If not given: imports, reasoning, loading
            and pre-caching, and the derived database is rebuilt at startup if needed; if given, it never is. Either
            way, each job that reaches ready deletes the previous derived databases.
        usage_tables: Whether to build the usage tables in the background (`browl.usage_tables`): after each job
            that reaches ready, and at startup if the ready generation's isn't current. If not given, only when
            `stages` isn't.

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
            app.state.usage_counts = UsageCounts()
            if stages is None:
                functions: Mapping[Stage, StageFunction] = {
                    Stage.IMPORTS: imports.imports_stage,
                    Stage.REASONING: robot.reasoning_stage,
                    Stage.LOADING: functools.partial(loading.loading_stage, http=client),
                    Stage.PRE_CACHING: functools.partial(precache.pre_caching_stage, http=client),
                }
            else:
                functions = stages
            keys = precache.build_keys(settings, precache.HOOKS)
            tables = app.state.usage_tables = UsageTables(settings.data_dir, app.state.sparql, keys)
            build_tables = stages is None if usage_tables is None else usage_tables

            async def on_ready(context: JobContext) -> None:
                if build_tables:  # first, as it cancels an older generation's build, which reads its derived file
                    try:
                        await tables.start(context.generation, dataset_clause(has_loaded_imports(context.imports)))
                    except Exception:
                        logger.exception("Couldn't start the usage table build of generation %d", context.generation)
                await precache.remove_previous(context)

            app.state.jobs = await JobRunner.open(settings, functions, on_ready=on_ready)
            app.state.derived = DerivedReader(settings.data_dir, keys)
            try:
                rebuilding = False
                if stages is None:
                    rebuilding = await app.state.jobs.rebuild(
                        functools.partial(precache.rebuild, http=client),
                        functools.partial(derived.is_current, settings.data_dir, keys=keys),
                    )
                if build_tables and not rebuilding:  # else the rebuild's end starts it
                    await _start_usage_table(tables, app.state.jobs.state, settings, keys)
                yield
            finally:
                try:
                    try:
                        await app.state.jobs.close()  # first, as a job reaching ready starts a build
                    finally:
                        await tables.close()
                finally:
                    app.state.derived.close()

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
    app.include_router(lookup.reserved_router, prefix=reserved)
    app.include_router(trees.router, prefix=reserved)
    app.mount(f"{reserved}/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(lookup.catch_all_router)  # last, so every other route comes first (§2.3, Lookup)
    no_pages = [f"{reserved}/health", f"{reserved}/static"]
    app.add_middleware(SiteGate, exempt=[*no_pages, f"{reserved}/admin"])
    # Outside the gate, so its "loading" and "not available" pages have the frame too.
    app.add_middleware(PageFrame, passthrough=no_pages, csp_exempt=[f"{reserved}/admin/docs"])
    app.add_exception_handler(NotCurrentError, derived_not_current)
    return app
