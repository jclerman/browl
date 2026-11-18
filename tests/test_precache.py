"""Tests for the pre-caching stage: the derived database built from bulk queries, its hooks, and its cleanup."""

import asyncio
import functools
import logging
import sqlite3
import threading
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from browl import derived
from browl.derived import Meta, NotCurrentError, derived_path, read_meta
from browl.fuseki import END_VARIABLE, TSV, UNION_FROM, FusekiClient, SparqlError
from browl.jobs import (
    INTERRUPTED_BY_SHUTDOWN,
    JOB_LOCK,
    REBUILDING,
    STATE_DB,
    ImportResult,
    JobContext,
    JobLock,
    JobRunner,
    JobState,
    JobStore,
    Reasoner,
    Site,
    Stage,
    StageError,
)
from browl.loading import dataset_name
from browl.pages import RETRY_AFTER_S, read_derived
from browl.precache import (
    PreCacheContext,
    batched,
    build,
    has_loaded_imports,
    ontology_iris,
    pre_caching_stage,
    remove_previous,
)
from browl.settings import Settings
from tests.conftest import DATA
from tests.helpers import make_app, make_settings

pytestmark = pytest.mark.anyio

EX = "http://example.org/"
ONTOLOGY = f"{EX}onto"
VERSION = f"{EX}onto/1.0"
HEADER = f"?ontology\t?version\t?{END_VARIABLE}\n"
END = "\t\ttrue\n"


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    """Return the test settings, with `DATA_DIR` an empty directory."""
    return make_settings()


ONTOLOGY_RESULT = f"{HEADER}<{ONTOLOGY}>\t<{VERSION}>\t\n{END}"
LOADED_AT = datetime(2026, 11, 17, tzinfo=UTC)


def fuseki(*bodies: str, queries: list[str] | None = None) -> httpx2.AsyncClient:
    """Return an HTTP client whose stubbed Fuseki answers the queries with these TSV bodies in turn, recording them.

    Without bodies, every query gets `ONTOLOGY_RESULT`; the last body answers the rest of the queries.
    """
    answers = list(bodies or [ONTOLOGY_RESULT])

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if queries is not None:
            queries.append(urllib.parse.parse_qs(request.content.decode())["query"][0])
        body = answers.pop(0) if len(answers) > 1 else answers[0]
        return httpx2.Response(200, headers={"Content-Type": TSV}, content=body)

    return httpx2.AsyncClient(base_url="http://fuseki:3030", transport=httpx2.MockTransport(handler))


def job(settings: Settings, generation: int, imports: list[ImportResult] | None = None) -> JobContext:
    """Return a job's context as the loading stage leaves it."""
    upload = settings.data_dir / "uploads" / str(generation) / "ontology.ttl"
    return JobContext(generation, settings, Reasoner.ELK, upload=upload, imports=imports or [], started_at=LOADED_AT)


def make_previous(settings: Settings, generation: int) -> Path:
    """Build an earlier generation's derived database, returning its path."""
    with derived.build(settings.data_dir, Meta(generation, Reasoner.NONE, LOADED_AT)):
        pass
    return derived_path(settings.data_dir, generation)


def derived_files(settings: Settings) -> list[str]:
    """Return the names of the files in the derived databases' directory."""
    directory = derived.derived_dir(settings.data_dir)
    return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []


async def test_stage_builds_database(settings: Settings) -> None:
    """Check that the stage writes `meta` and runs the hooks in order, then swaps the file in at its final path."""
    seen: list[str] = []
    queries: list[str] = []

    async def first(context: PreCacheContext) -> None:
        seen.append("first")
        assert (context.dataset, context.has_imports, context.dataset_clause) == ("gen-7", True, UNION_FROM)
        assert context.job.generation == 7
        await context.db.insert(
            "INSERT INTO prefix VALUES (?, ?)", [("ex", EX), ("owl", "http://www.w3.org/2002/07/owl#")]
        )

    async def second(context: PreCacheContext) -> None:
        seen.append("second")
        assert await context.db.run(lambda db: db.execute("SELECT count(*) FROM prefix").fetchone()[0]) == 2

    imports = [ImportResult(f"{EX}i", triples=3), ImportResult("file:///x", reason="dropped")]
    async with fuseki(queries=queries) as http:
        await pre_caching_stage(job(settings, 7, imports), http, hooks=[first, second])
    assert seen == ["first", "second"]
    assert derived_files(settings) == ["7.sqlite3"]
    with closing(derived.connect(settings.data_dir, 7)) as connection:
        stored = read_meta(connection)
        assert connection.execute("SELECT prefix FROM prefix ORDER BY prefix").fetchall() == [("ex",), ("owl",)]
    assert (stored.generation, stored.reasoner, stored.ontology_iri, stored.version_iri) == (
        7,
        Reasoner.ELK,
        ONTOLOGY,
        VERSION,
    )
    assert stored.loaded_at == LOADED_AT  # the job's start
    [query] = queries
    assert "owl:Ontology" in query and "FROM" not in query  # the ontology's IRIs come from its own graph


async def test_stage_hook_fails(settings: Settings) -> None:
    """Check that a failing hook fails the stage, leaving no new file and the previous one in place."""
    previous = make_previous(settings, 3)

    async def failing(context: PreCacheContext) -> None:
        await context.db.insert("INSERT INTO prefix VALUES (?, ?)", [("ex", EX)])
        raise StageError("the entity list is broken")

    async with fuseki() as http:
        with pytest.raises(StageError, match=r"^the entity list is broken$"):
            await pre_caching_stage(job(settings, 4), http, hooks=[failing])
    assert derived_files(settings) == ["3.sqlite3"]
    assert previous.is_file()
    assert derived.is_current(settings.data_dir, 3)


async def test_stage_hook_query_fails(settings: Settings) -> None:
    """Check that a hook's failed query fails the stage with a reason naming the hook."""

    async def entities(context: PreCacheContext) -> None:
        async with context.bulk_select("SELECT ?s WHERE { ?s ?p ?o }") as stream:
            [row async for row in stream]

    async with fuseki(ONTOLOGY_RESULT, f"?s\t?{END_VARIABLE}\n<{EX}a>\t\n") as http:
        with pytest.raises(StageError, match=r"^pre-caching query failed \(.*entities\): "):
            await pre_caching_stage(job(settings, 5), http, hooks=[entities])
    assert derived_files(settings) == []


@pytest.mark.parametrize(
    "body",
    [f"{HEADER}<{ONTOLOGY}>\t<{VERSION}>\t\n", HEADER],
    ids=["no-marker", "no-rows"],
)
async def test_stage_incomplete_result(settings: Settings, body: str) -> None:
    """Check that a result cut cleanly at a row boundary fails pre-caching, never passing as complete."""
    async with fuseki(body) as http:
        with pytest.raises(StageError, match=r"^pre-caching query failed: the result is incomplete"):
            await pre_caching_stage(job(settings, 6), http)
    assert derived_files(settings) == []


async def test_stage_skipped_without_ontology(settings: Settings) -> None:
    """Check that the stage does nothing if no ontology was loaded, as the loading stage does."""
    async with fuseki() as http:
        await pre_caching_stage(JobContext(8, settings), http)
    assert derived_files(settings) == []


@pytest.mark.parametrize(
    ("rows", "expected", "warning"),
    [
        ("", (None, None), None),
        (f"<{ONTOLOGY}>\t\t\n", (ONTOLOGY, None), None),
        (f"<{EX}b>\t\t\n<{EX}a>\t<{EX}a/1>\t\n", (f"{EX}a", f"{EX}a/1"), "several ontology IRIs"),
        (f"<{ONTOLOGY}>\t<{EX}v2>\t\n<{ONTOLOGY}>\t<{EX}v1>\t\n", (ONTOLOGY, f"{EX}v1"), "several version IRIs"),
    ],
    ids=["none", "no-version", "several-ontologies", "several-versions"],
)
async def test_ontology_iris(
    rows: str, expected: tuple[str | None, str | None], warning: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    """Check the ontology IRI and version IRI chosen, stably, from what the default graph declares."""
    async with fuseki(f"{HEADER}{rows}{END}") as http:
        with caplog.at_level(logging.WARNING, logger="browl.precache"):
            assert await ontology_iris(FusekiClient(http), "gen-1") == expected
    assert (warning is None) == (not caplog.records)
    if warning:
        assert warning in caplog.text


def test_has_loaded_imports() -> None:
    """Check that only an import that was loaded calls for the merged dataset."""
    assert not has_loaded_imports([])
    assert not has_loaded_imports([ImportResult(f"{EX}i", reason="dropped")])
    assert has_loaded_imports([ImportResult(f"{EX}i", reason="dropped"), ImportResult(f"{EX}j", triples=0)])


async def test_batched() -> None:
    """Check that rows are handed over in batches of the size asked, the last one shorter."""

    async def rows() -> AsyncIterator[int]:
        for row in range(7):
            yield row

    assert [batch async for batch in batched(rows(), 3)] == [[0, 1, 2], [3, 4, 5], [6]]
    assert [batch async for batch in batched(rows(), 7)] == [list(range(7))]


async def test_build_runs_on_one_thread(settings: Settings) -> None:
    """Check that every write runs on the build's own thread, not the event loop's."""
    threads: set[int] = set()
    async with build(settings.data_dir, Meta(9, Reasoner.NONE, LOADED_AT)) as db:
        for _ in range(3):
            await db.run(lambda connection: threads.add(threading.get_ident()))
    assert len(threads) == 1
    assert threading.get_ident() not in threads
    assert derived.is_current(settings.data_dir, 9)


async def test_build_cancelled(settings: Settings) -> None:
    """Check that a build cancelled while its thread is busy deletes its temporary file once the thread is done."""
    previous = make_previous(settings, 1)
    started = threading.Event()
    release = threading.Event()

    def busy(connection: object) -> None:
        started.set()
        release.wait(10)

    async def run() -> None:
        async with build(settings.data_dir, Meta(2, Reasoner.NONE, LOADED_AT)) as db:
            await db.run(busy)

    task = asyncio.create_task(run())
    await asyncio.to_thread(started.wait, 10)
    assert "2.sqlite3.tmp" in derived_files(settings)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()  # waiting for the thread to finish the build's end
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert derived_files(settings) == ["1.sqlite3"]
    assert previous.is_file()


async def test_build_failure_at_swap(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a failure at the swap, after the block, propagates and leaves no file."""

    def fail(*args: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(derived.os, "replace", fail)
    with pytest.raises(OSError, match="disk gone"):
        async with build(settings.data_dir, Meta(2, Reasoner.NONE, LOADED_AT)):
            pass
    assert derived_files(settings) == []


async def upload_stage(context: JobContext) -> None:
    """Stand in for the stages before pre-caching: give the job an ontology."""
    context.upload = context.settings.data_dir / "ontology.ttl"


async def test_runner_cleanup_after_ready(settings: Settings) -> None:
    """Check jobs end to end: a failed pre-caching keeps the previous database; a job that reaches ready removes it."""
    fail = False

    async def hook(context: PreCacheContext) -> None:
        if fail:
            raise SparqlError("Fuseki answered 500: boom")

    async with fuseki() as http:
        stages = {
            Stage.IMPORTS: upload_stage,
            Stage.PRE_CACHING: functools.partial(pre_caching_stage, http=http, hooks=[hook]),
        }
        runner = await JobRunner.open(settings, stages, on_ready=remove_previous)
        try:
            await runner.start()
            await runner.wait()
            assert (runner.state.stage, runner.state.ready_generation) == (Stage.READY, 1)
            assert derived_files(settings) == ["1.sqlite3"]

            fail = True
            await runner.start()
            await runner.wait()
            state = runner.state
            assert (state.failed_stage, state.ready_generation, state.site) == (
                Stage.PRE_CACHING,
                1,
                Site.NOT_AVAILABLE,
            )
            assert state.reason == "pre-caching query failed (test_runner_cleanup_after_ready.<locals>.hook): " + (
                "Fuseki answered 500: boom"
            )
            assert derived_files(settings) == ["1.sqlite3"]

            fail = False
            await runner.start()
            await runner.wait()
            assert (runner.state.stage, runner.state.ready_generation) == (Stage.READY, 3)
            assert derived_files(settings) == ["3.sqlite3"]
        finally:
            await runner.close()


async def test_remove_previous(settings: Settings) -> None:
    """Check that the after-ready work keeps only the ready generation's database."""
    make_previous(settings, 1)
    make_previous(settings, 2)
    await remove_previous(JobContext(2, settings))
    assert derived_files(settings) == ["2.sqlite3"]


@pytest.fixture
async def loaded(fuseki_url: str) -> AsyncIterator[tuple[httpx2.AsyncClient, int]]:
    """Yield a client for the tests' Fuseki and a generation whose dataset holds `family.ttl` and an import's graph.

    The dataset is in memory, so the test needs no Jena tools; it's removed afterwards.
    """
    generation = 900_000 + uuid.uuid4().int % 100_000
    name = dataset_name(generation)
    triples = "".join(f'<{EX}s{n}> <{EX}p{n % 7}> "v{n}" .\n' for n in range(20_000))  # several MB of results
    async with httpx2.AsyncClient(base_url=fuseki_url, timeout=60) as http:
        (await http.post("/$/datasets", data={"dbName": name, "dbType": "mem"})).raise_for_status()
        try:
            for graph, content, content_type in [
                (None, (DATA / "family.ttl").read_bytes(), "text/turtle"),
                (f"{EX}imported", triples.encode(), "application/n-triples"),
            ]:
                params = {} if graph is None else {"graph": graph}
                response = await http.post(
                    f"/{name}/data", params=params, content=content, headers={"Content-Type": content_type}
                )
                response.raise_for_status()
            yield http, generation
        finally:
            await http.delete(f"/$/datasets/{name}")


@pytest.mark.fuseki
async def test_stage_on_real_fuseki(settings: Settings, loaded: tuple[httpx2.AsyncClient, int]) -> None:
    """Check pre-caching on a real Fuseki: the ontology's IRIs, and bulk results that pass their completeness check.

    ARQ streams a `UNION`'s branches in order, so the end marker comes last, after a large result too, and after one
    that's grouped, distinct or ordered.
    """
    http, generation = loaded
    counts: dict[str, int] = {}

    async def count(context: PreCacheContext) -> None:
        queries = {
            "all": "SELECT ?s ?p ?o WHERE { ?s ?p ?o }",
            "ordered": "SELECT ?o ?s WHERE { ?s ?p ?o } ORDER BY ?o",
            "distinct": "SELECT DISTINCT ?p WHERE { ?s ?p ?o }",
            "grouped": "SELECT ?p (COUNT(*) AS ?n) WHERE { ?s ?p ?o } GROUP BY ?p",
        }
        for name, query in queries.items():
            async with context.bulk_select(query) as stream:
                rows = [row async for batch in batched(stream) for row in batch]
            counts[name] = len(rows)
            if name == "ordered":
                assert stream.variables == ("o", "s")
        async with context.bulk_select("SELECT ?s WHERE { ?s ?p ?o }", imports=False) as stream:
            counts["own"] = len([row async for row in stream])
        plain = await context.sparql.select(
            context.dataset, f"SELECT (COUNT(*) AS ?n) {UNION_FROM} WHERE {{ ?s ?p ?o }}"
        )
        counts["expected"] = int(plain.rows[0][0].value)  # type: ignore[union-attr]

    context = job(settings, generation, [ImportResult(f"{EX}imported", triples=20_000)])
    await pre_caching_stage(context, http, hooks=[count])
    assert counts["all"] == counts["ordered"] == counts["expected"] > 20_000
    assert counts["grouped"] == counts["distinct"] > 7
    assert counts["own"] == counts["expected"] - 20_000
    with closing(derived.connect(settings.data_dir, generation)) as connection:
        stored = read_meta(connection)
    assert (stored.ontology_iri, stored.version_iri) == ("http://example.org/family", None)


async def test_build_fails_to_start(tmp_path: Path) -> None:
    """Check that a build that can't create its file fails with the error, and has nothing to clean up."""
    data_dir = tmp_path / "file"
    data_dir.write_text("")
    with pytest.raises(OSError, match="file"):
        async with build(data_dir, Meta(1, Reasoner.NONE, LOADED_AT)):
            pass


STARTED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
READY = JobState(
    generation=2,
    stage=Stage.READY,
    started_at=STARTED,
    finished_at=STARTED + timedelta(minutes=1),
    ready_generation=2,
    reasoner=Reasoner.ELK,
    triples=5,
)
"""A job that reached ready before a restart."""


class StubFuseki:
    """A stubbed Fuseki that answers every query with `ONTOLOGY_RESULT`, or `status`, once released, recording them.

    Attributes:
        requests: The paths requested.
        released: Set to let answers through.
        status: The status of the answers.
    """

    def __init__(self, released: bool = True, status: int = 200) -> None:
        """Answer at once if `released`, else once `released` is set."""
        self.requests: list[str] = []
        self.released = threading.Event()
        if released:
            self.released.set()
        self.status = status
        self.transport = httpx2.MockTransport(self.handle)

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        """Answer a request once released."""
        self.requests.append(request.url.path)
        while not self.released.is_set():
            await asyncio.sleep(0.01)
        if self.status != 200:
            return httpx2.Response(self.status, text="boom")
        return httpx2.Response(200, headers={"Content-Type": TSV}, content=ONTOLOGY_RESULT)


def restart(fuseki: StubFuseki) -> FastAPI:
    """Return an app, with the app's own stages and the stubbed Fuseki, and a route that reads the derived database."""
    app = make_app(fuseki.transport, real_stages=True)

    @app.get("/probe")
    async def probe(request: Request) -> int:
        return await read_derived(request, lambda connection: read_meta(connection).generation)

    return app


def status(client: TestClient) -> str:
    """Return the admin status page."""
    return client.get("/_browl/admin/status").text


def test_restart_reuses_current_database(settings: Settings) -> None:
    """Check that a restart reuses a current derived database: no query, no rebuild, and pages read it."""
    JobStore(settings.data_dir / STATE_DB).write(READY)
    make_previous(settings, 2)
    fuseki = StubFuseki()
    app = restart(fuseki)
    with TestClient(app) as client:
        assert app.state.jobs.state == READY
        assert client.get("/probe").json() == 2
    assert fuseki.requests == []


def stale_schema(settings: Settings) -> None:
    """Leave generation 2 a derived database of another schema version."""
    with sqlite3.connect(make_previous(settings, 2)) as connection:
        connection.execute("UPDATE meta SET schema_version = 0")


@pytest.mark.parametrize(
    "prepare",
    [
        pytest.param(lambda settings: None, id="missing"),
        pytest.param(stale_schema, id="stale schema"),
        pytest.param(lambda settings: make_previous(settings, 1), id="other generation"),
    ],
)
def test_restart_rebuilds(settings: Settings, prepare: Callable[[Settings], object]) -> None:
    """Check that a restart rebuilds a derived database that isn't current, from the same store.

    The site serves "loading" meanwhile, and the database keeps the job's start as its load time.
    """
    JobStore(settings.data_dir / STATE_DB).write(READY)
    prepare(settings)
    fuseki = StubFuseki(released=False)
    app = restart(fuseki)
    with TestClient(app) as client:
        response = client.get("/probe")
        assert (response.status_code, response.headers["Retry-After"]) == (503, str(RETRY_AFTER_S))
        assert '<li class="current" aria-current="step">pre-caching</li>' in status(client)
        fuseki.released.set()
        assert client.portal is not None
        client.portal.call(app.state.jobs.wait)
        state = app.state.jobs.state
        assert (state.generation, state.stage, state.ready_generation, state.started_at) == (2, Stage.READY, 2, STARTED)
        assert client.get("/probe").json() == 2
    assert fuseki.requests == [f"/{dataset_name(2)}/sparql"]
    assert derived_files(settings) == ["2.sqlite3"]
    with closing(derived.connect(settings.data_dir, 2)) as connection:
        assert read_meta(connection) == Meta(2, Reasoner.ELK, STARTED, ONTOLOGY, VERSION)


def test_restart_rebuild_fails(settings: Settings) -> None:
    """Check that a failed rebuild leaves the site "not available", and shows admins why."""
    JobStore(settings.data_dir / STATE_DB).write(READY)
    app = restart(StubFuseki(status=500))
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(app.state.jobs.wait)
        response = client.get("/probe")
        assert response.status_code == 503 and "<h1>Not available</h1>" in response.text
        assert "Failed in stage <strong>pre-caching</strong>: rebuilding after a restart: pre-caching query" in (
            status(client)
        )
    assert derived_files(settings) == []


def test_restart_rebuild_shutdown(settings: Settings) -> None:
    """Check that a shutdown during a rebuild cancels it, recording why, and leaves no file behind."""
    JobStore(settings.data_dir / STATE_DB).write(READY)
    fuseki = StubFuseki(released=False)
    with TestClient(restart(fuseki)):
        wait_until(lambda: bool(fuseki.requests))
    state = JobStore(settings.data_dir / STATE_DB).read()
    assert (state.failed_stage, state.reason) == (
        Stage.PRE_CACHING,
        f"{REBUILDING}: {INTERRUPTED_BY_SHUTDOWN}",
    )
    assert derived_files(settings) == []


def test_restart_while_another_worker_rebuilds(settings: Settings) -> None:
    """Check that a worker that can't take the job lock doesn't rebuild.

    It serves "loading" for a page that reads the derived database before it sees the other worker's rebuild.
    """
    JobStore(settings.data_dir / STATE_DB).write(READY)
    fuseki = StubFuseki()
    lock = JobLock(settings.data_dir / JOB_LOCK)
    assert lock.acquire()
    try:
        with TestClient(restart(fuseki)) as client:
            response = client.get("/probe")
            assert (response.status_code, response.headers["Retry-After"]) == (503, str(RETRY_AFTER_S))
            assert "The ontology is being loaded" in response.text
    finally:
        lock.release()
    assert fuseki.requests == []
    assert JobStore(settings.data_dir / STATE_DB).read() == READY


def wait_until(condition: Callable[[], bool]) -> None:
    """Wait up to 5 s for the condition, polling it."""
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.01)


async def test_read_derived_without_ready_generation() -> None:
    """Check that a derived read with no generation ready fails as not current, which the app serves as "loading"."""

    class Jobs:
        async def current(self) -> JobState:
            return JobState()

    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(jobs=Jobs()))})
    with pytest.raises(NotCurrentError, match="no generation is ready"):
        await read_derived(request, read_meta)
