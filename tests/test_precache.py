"""Tests for the pre-caching stage: the derived database built from bulk queries, its hooks, and its cleanup."""

import asyncio
import functools
import itertools
import logging
import sqlite3
import sys
import threading
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import browl.precache.stage
from browl import derived, usage_tables
from browl.derived import EntityType, Meta, NotCurrentError, derived_path, entities_at, read_meta
from browl.fuseki import END_VARIABLE, TSV, UNION_FROM, FusekiClient, SparqlError
from browl.jobs import (
    INTERRUPTED_BY_SHUTDOWN,
    JOB_LOCK,
    REBUILDING,
    STATE_DB,
    JobContext,
    JobLock,
    JobRunner,
    JobState,
    JobStore,
    Site,
    Stage,
)
from browl.jobtypes import ImportResult, Reasoner, StageError
from browl.loading import dataset_name
from browl.pages import RETRY_AFTER_S, read_derived
from browl.precache import header, stage
from browl.precache.axiom_counts import axiom_counts
from browl.precache.core import Hook, PreCacheContext, batched, has_loaded_imports, hook
from browl.precache.entities import entities
from browl.precache.header import OntologyHeader, ontology_header
from browl.precache.keys import build_keys
from browl.precache.search import search
from browl.precache.stage import HOOKS, build, pre_cache, pre_caching_stage, rebuild, remove_previous
from browl.precache.values import annotation_values
from browl.settings import Settings
from tests.conftest import DATA
from tests.helpers import (
    DC_TITLE,
    DCTERMS_TITLE,
    END,
    ENTITY_END,
    ENTITY_HEADER,
    EX,
    HEADER,
    LABEL,
    LOADED_AT,
    ONTOLOGY,
    ONTOLOGY_RESULT,
    OWL,
    VERSION,
    VERSION_INFO,
    VERSION_IRI,
    answer,
    connect,
    fuseki,
    job,
    make_app,
    make_settings,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    """Return the test settings, with `DATA_DIR` an empty directory."""
    return make_settings()


def is_current(settings: Settings, generation: int, hooks: Sequence[Hook] = HOOKS) -> bool:
    """Return whether a generation's derived database is current for these settings and hooks (`derived.is_current`)."""
    return derived.is_current(settings.data_dir, generation, build_keys(settings, hooks))


def make_previous(settings: Settings, generation: int) -> Path:
    """Build an earlier generation's derived database, current for the app's hooks, returning its path."""
    with derived.build(settings.data_dir, Meta(generation, Reasoner.NONE, LOADED_AT), build_keys(settings, HOOKS)):
        pass
    return derived_path(settings.data_dir, generation)


def derived_files(settings: Settings) -> list[str]:
    """Return the names of the files in the derived databases' directory."""
    directory = derived.derived_dir(settings.data_dir)
    return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []


async def test_stage_builds_database(settings: Settings) -> None:
    """Check that the stage writes `meta` and runs the hooks in order, then swaps the file in at its final path.

    A hook's indexes are there for the hooks after it.
    """
    seen: list[str] = []
    queries: list[str] = []

    @hook()
    async def first(context: PreCacheContext) -> None:
        seen.append("first")
        assert (context.dataset, context.dataset_clause) == ("gen-7", UNION_FROM)
        assert context.job.generation == 7
        await context.db.insert(
            "INSERT INTO prefix VALUES (?, ?)", [("ex", EX), ("owl", "http://www.w3.org/2002/07/owl#")]
        )

        await context.db.insert("INSERT INTO entity (iri, type, path) VALUES (?, 'class', '/')", [(f"{EX}a",)])
        await context.db.run(lambda connection: None)  # a turn of the event loop with the changes made
        await context.db.create_indexes("entity")

    @hook()
    async def second(context: PreCacheContext) -> None:
        seen.append("second")
        assert await context.db.run(lambda db: db.execute("SELECT count(*) FROM prefix").fetchone()[0]) == 2
        indexes = "SELECT name FROM sqlite_schema WHERE type = 'index' AND tbl_name = 'entity' ORDER BY name"
        assert await context.db.run(lambda db: db.execute(indexes).fetchall()) == [
            ("entity_iri_type",),
            ("entity_path",),
        ]

    imports = [ImportResult(f"{EX}i", triples=3), ImportResult("file:///x", reason="dropped")]
    async with fuseki(queries=queries) as http:
        await pre_caching_stage(job(settings, 7, imports), http, hooks=[first, second])
    assert seen == ["first", "second"]
    assert derived_files(settings) == ["7.sqlite3"]
    with closing(connect(settings, 7, [first, second])) as connection:
        stored = read_meta(connection)
        assert connection.execute("SELECT prefix FROM prefix ORDER BY prefix").fetchall() == [("ex",), ("owl",)]
    assert (stored.generation, stored.reasoner, stored.ontology_iri, stored.version_iri) == (
        7,
        Reasoner.ELK,
        ONTOLOGY,
        VERSION,
    )
    assert (stored.title, stored.version_info) == ("Onto", "1.0")
    assert stored.loaded_at == LOADED_AT  # the job's start
    [query] = queries
    assert "owl:Ontology" in query and "FROM" not in query  # the ontology's IRIs come from its own graph


async def test_stage_reports_progress(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the stage reports reading the header, then each hook's number, name, time so far and phase.

    The phase is a bulk query's rows received, or the writing or indexing the hook waits for; the database changes
    are shown when they move, or when there's no phase.
    """
    monkeypatch.setattr(browl.precache.stage, "PROGRESS_S", 0)  # a report each turn of the event loop

    @hook()
    async def fill(context: PreCacheContext) -> None:
        async with context.bulk_select("SELECT ?entity ?type WHERE { ?entity a ?type }") as stream:
            await asyncio.sleep(0)  # as if no row had arrived yet
            async for _ in stream:
                await asyncio.sleep(0)  # a turn of the event loop per row
        await context.db.insert(
            "INSERT INTO prefix VALUES (?, ?)", [("ex", EX), ("owl", "http://www.w3.org/2002/07/owl#")]
        )
        await context.db.create_indexes("entity")
        await asyncio.sleep(0)

    reports: list[str] = []
    context = job(settings, 9)
    context.on_progress = lambda detail, fraction, iri: reports.append(detail)
    async with fuseki() as http:
        await pre_caching_stage(context, http, hooks=[fill])
    assert reports[0] == "Reading the ontology's header"
    assert reports[-1] == "Finishing the database"
    phases = list(dict.fromkeys(report.removeprefix("Hook 1 of 1, fill · 0 s · ") for report in reports[1:-1]))
    # The insert's changes are counted on the database thread, so the first report while writing can show them, and
    # the next, as they haven't moved, plain "writing tables" (#157).
    writing = ["writing tables", "writing tables · 2 database changes"]
    if phases[2:4] == writing[::-1]:
        phases[2:4] = writing
    assert phases == [
        "waiting for query results",
        "receiving query results: 1 rows",
        "writing tables",
        "writing tables · 2 database changes",
        "indexing",
        "2 database changes",
    ]


async def test_stage_reports_latest_stream(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that while two bulk queries stream at once, the rows received are counted from the latest to open."""
    monkeypatch.setattr(browl.precache.stage, "PROGRESS_S", 0)
    query = "SELECT ?entity ?type WHERE { ?entity a ?type }"

    @hook()
    async def fill(context: PreCacheContext) -> None:
        async with context.bulk_select(query) as outer:
            async for _ in outer:
                await asyncio.sleep(0)
                async with context.bulk_select(query) as inner:
                    await asyncio.sleep(0)
                    async for _ in inner:
                        await asyncio.sleep(0)

    reports: list[str] = []
    context = job(settings, 9)
    context.on_progress = lambda detail, fraction, iri: reports.append(detail)
    async with fuseki() as http:
        await pre_caching_stage(context, http, hooks=[fill])
    details = (report.removeprefix("Hook 1 of 1, fill · 0 s · ") for report in reports[1:-1])
    phases = [phase for phase, _ in itertools.groupby(details)]
    assert phases[:3] == [
        "receiving query results: 1 rows",
        "waiting for query results",  # the inner query's, not yet with a row
        "receiving query results: 1 rows",
    ]


async def test_stage_hook_fails(settings: Settings) -> None:
    """Check that a failing hook fails the stage, leaving no new file and the previous one in place."""
    previous = make_previous(settings, 3)

    @hook()
    async def failing(context: PreCacheContext) -> None:
        await context.db.insert("INSERT INTO prefix VALUES (?, ?)", [("ex", EX)])
        raise StageError("the entity list is broken")

    async with fuseki() as http:
        with pytest.raises(StageError, match=r"^the entity list is broken$"):
            await pre_caching_stage(job(settings, 4), http, hooks=[failing])
    assert derived_files(settings) == ["3.sqlite3"]
    assert previous.is_file()
    assert is_current(settings, 3)


async def test_stage_hook_query_fails(settings: Settings) -> None:
    """Check that a hook's incomplete result fails the stage, naming the hook, and the previous database stays."""
    make_previous(settings, 4)

    @hook()
    async def entities(context: PreCacheContext) -> None:
        async with context.bulk_select("SELECT ?s WHERE { ?s ?p ?o }") as stream:
            [row async for row in stream]

    async with fuseki(ONTOLOGY_RESULT, f"?s\t?{END_VARIABLE}\n<{EX}a>\t\n") as http:
        with pytest.raises(StageError, match=r"^pre-caching query failed \(.*entities\): the result is incomplete"):
            await pre_caching_stage(job(settings, 5), http, hooks=[entities])
    assert derived_files(settings) == ["4.sqlite3"]
    assert is_current(settings, 4)


@pytest.mark.parametrize(
    "body",
    [f"{HEADER}<{ONTOLOGY}>\t{VERSION_IRI}\t<{VERSION}>\t\n", HEADER],
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
        ("", OntologyHeader(), None),
        (f"<{ONTOLOGY}>\t\t\t\n", OntologyHeader(ONTOLOGY), None),
        (
            (
                f'<{EX}b>\t{DCTERMS_TITLE}\t"B"\t\n<{EX}a>\t{VERSION_IRI}\t<{EX}a/1>\t\n'
                f'<{EX}c>\t{DCTERMS_TITLE}\t"C"\t\n<{EX}b>\t{VERSION_INFO}\t"b"\t\n'
            ),
            OntologyHeader(f"{EX}a", f"{EX}a/1"),
            f"declares 3 ontology IRIs; taking '{EX}a', not ['{EX}b', '{EX}c']",
        ),
        (
            (
                f"<{ONTOLOGY}>\t{VERSION_IRI}\t<{EX}v2>\t\n<{ONTOLOGY}>\t{VERSION_IRI}\t<{EX}v1>\t\n"
                f'<{ONTOLOGY}>\t{VERSION_IRI}\t"v0"\t\n'
            ),
            OntologyHeader(ONTOLOGY, f"{EX}v1"),
            "several version IRIs",
        ),
        (
            (
                f'<{ONTOLOGY}>\t{LABEL}\t"label"@en\t\n<{ONTOLOGY}>\t{DC_TITLE}\t"dc"@en\t\n'
                f'<{ONTOLOGY}>\t{DCTERMS_TITLE}\t"zz"\t\n<{ONTOLOGY}>\t{DCTERMS_TITLE}\t"titre"@fr\t\n'
                f'<{ONTOLOGY}>\t{DCTERMS_TITLE}\t"Title"@en-gb\t\n'
            ),
            OntologyHeader(ONTOLOGY, title="Title"),
            None,
        ),
        (
            (
                f'<{ONTOLOGY}>\t{LABEL}\t"label"@en\t\n<{ONTOLOGY}>\t{DC_TITLE}\t"dc"\t\n'
                f"<{ONTOLOGY}>\t{DCTERMS_TITLE}\t<{EX}not-a-literal>\t\n"
            ),
            OntologyHeader(ONTOLOGY, title="dc"),
            None,
        ),
        (
            (
                f'<{ONTOLOGY}>\t{LABEL}\t"label"\t\n<{ONTOLOGY}>\t{LABEL}\t"Label"@en\t\n'
                f"<{ONTOLOGY}>\t{DC_TITLE}\t_:b0\t\n"
            ),
            OntologyHeader(ONTOLOGY, title="Label"),
            None,
        ),
        (
            f'<{ONTOLOGY}>\t{DCTERMS_TITLE}\t"B"@en\t\n<{ONTOLOGY}>\t{DCTERMS_TITLE}\t"A"@en-us\t\n',
            OntologyHeader(ONTOLOGY, title="A"),
            "several titles",
        ),
        (
            f'<{ONTOLOGY}>\t{VERSION_INFO}\t"4.4-dev"\t\n<{ONTOLOGY}>\t{VERSION_INFO}\t<{EX}not-a-literal>\t\n',
            OntologyHeader(ONTOLOGY, version_info="4.4-dev"),
            None,
        ),
        (
            (
                f'<{ONTOLOGY}>\t{VERSION_INFO}\t"2"\t\n<{ONTOLOGY}>\t{VERSION_INFO}\t"3"@en\t\n'
                f'<{ONTOLOGY}>\t{VERSION_INFO}\t"1"\t\n'
            ),
            OntologyHeader(ONTOLOGY, version_info="3"),
            "several owl:versionInfo values ['1', '2', '3']; taking '3'",
        ),
    ],
    ids=[
        "none",
        "no-version",
        "several-ontologies",
        "several-versions",
        "dcterms-title-by-language",
        "dc-title-fallback",
        "name-property-fallback",
        "tied-titles",
        "version-info",
        "several-version-infos",
    ],
)
async def test_ontology_header(
    rows: str, expected: OntologyHeader, warning: str | None, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """Check the ontology's IRIs, title and version info chosen, stably, from what the default graph declares."""
    async with fuseki(f"{HEADER}{rows}{END}") as http:
        with caplog.at_level(logging.WARNING, logger="browl.precache"):
            assert await ontology_header(FusekiClient(http), "gen-1", settings) == expected
    assert (warning is None) == (not caplog.records)
    if warning:
        assert warning in caplog.text


async def test_ontology_header_limits(
    settings: Settings, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that a header query result of too many rows fails, and that a warning lists only a few other IRIs."""
    monkeypatch.setattr(header, "HEADER_MAX_ROWS", 4)
    monkeypatch.setattr(header, "_LOGGED_IRIS", 2)
    rows = "".join(f"<{EX}{name}>\t\t\t\n" for name in "dcba")
    async with fuseki(f"{HEADER}{rows}{END}") as http:
        with caplog.at_level(logging.WARNING, logger="browl.precache"):
            assert await ontology_header(FusekiClient(http), "gen-1", settings) == OntologyHeader(f"{EX}a")
    assert f"declares 4 ontology IRIs; taking '{EX}a', not ['{EX}b', '{EX}c'] …" in caplog.text
    async with fuseki(f"{HEADER}{rows}<{EX}e>\t\t\t\n{END}") as http:
        with pytest.raises(StageError, match=r"^the ontology header query returned more than 4 rows$"):
            await ontology_header(FusekiClient(http), "gen-1", settings)


async def test_ontology_header_query(data_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Check that the query asks for the titles and the configured name properties, expanded, but no non-IRIs."""
    queries: list[str] = []
    names = ["skos:prefLabel", "ex:name", "dc:title", "not an IRI"]
    settings = make_settings(NAME_PROPERTIES=names, EXTRA_PREFIXES={"ex": EX})
    async with fuseki(queries=queries) as http:
        await ontology_header(FusekiClient(http), "gen-1", settings)
    properties = (
        f"{VERSION_IRI}, {VERSION_INFO}, {DCTERMS_TITLE}, {DC_TITLE}, "
        f"<http://www.w3.org/2004/02/skos/core#prefLabel>, <{EX}name>)"
    )
    assert properties in queries[0]
    assert "isn't an IRI or a known CURIE" in caplog.text


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
    async with build(settings.data_dir, Meta(9, Reasoner.NONE, LOADED_AT), build_keys(settings, HOOKS)) as db:
        for _ in range(3):
            await db.run(lambda connection: threads.add(threading.get_ident()))
    assert len(threads) == 1
    assert threading.get_ident() not in threads
    assert is_current(settings, 9)


async def test_build_cancelled(settings: Settings) -> None:
    """Check that a build cancelled while its thread is busy deletes its temporary file once the thread is done."""
    previous = make_previous(settings, 1)
    started = threading.Event()
    release = threading.Event()

    def busy(connection: object) -> None:
        started.set()
        release.wait(10)

    async def run() -> None:
        async with build(settings.data_dir, Meta(2, Reasoner.NONE, LOADED_AT), build_keys(settings, HOOKS)) as db:
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


@pytest.mark.parametrize("fails", [False, True], ids=["swapped", "failed"])
async def test_build_cancelled_at_end(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, fails: bool
) -> None:
    """Check that a build cancelled during its end, here the rename, waits for the end before the cancellation ends it.

    So the end can't outlive the job lock. The database it swapped in is whole; if the end fails, that's logged.
    """
    renaming, release = threading.Event(), threading.Event()
    rename = derived.os.replace

    def blocked(source: Path, target: Path) -> None:
        renaming.set()
        release.wait(10)
        if fails:
            raise OSError("disk gone")
        rename(source, target)

    monkeypatch.setattr(derived.os, "replace", blocked)

    async def run() -> None:
        async with build(settings.data_dir, Meta(2, Reasoner.NONE, LOADED_AT), build_keys(settings, HOOKS)):
            pass

    task = asyncio.create_task(run())
    assert await asyncio.to_thread(renaming.wait, 10)
    for _ in range(2):  # a second cancellation doesn't end the wait either
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    if fails:
        assert derived_files(settings) == []
        assert "The cancelled derived build's end failed" in caplog.text
    else:
        assert derived_files(settings) == ["2.sqlite3"]
        assert is_current(settings, 2)


async def test_build_failure_at_swap(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a failure at the swap, after the block, propagates and leaves no file."""

    def fail(*args: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(derived.os, "replace", fail)
    with pytest.raises(OSError, match="disk gone"):
        async with build(settings.data_dir, Meta(2, Reasoner.NONE, LOADED_AT), build_keys(settings, HOOKS)):
            pass
    assert derived_files(settings) == []


async def upload_stage(context: JobContext) -> None:
    """Stand in for the stages before pre-caching: give the job an ontology."""
    context.upload = context.settings.data_dir / "ontology.ttl"


async def test_runner_cleanup_after_ready(settings: Settings) -> None:
    """Check jobs end to end: a failed pre-caching keeps the previous database; a job that reaches ready removes it."""
    fail = False

    @hook()
    async def flaky(context: PreCacheContext) -> None:
        if fail:
            raise SparqlError("Fuseki answered 500: boom")

    async with fuseki() as http:
        stages = {
            Stage.IMPORTS: upload_stage,
            Stage.PRE_CACHING: functools.partial(pre_caching_stage, http=http, hooks=[flaky]),
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
            assert state.reason == "pre-caching query failed (flaky): Fuseki answered 500: boom"
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


async def test_stage_on_real_fuseki(settings: Settings, loaded: tuple[httpx2.AsyncClient, int]) -> None:
    """Check pre-caching on a real Fuseki: the ontology's IRIs, and bulk results that pass their completeness check.

    ARQ streams a `UNION`'s branches in order, so the end marker comes last, after a large result too, and after one
    that's grouped, distinct or ordered.
    """
    http, generation = loaded
    counts: dict[str, int] = {}

    @hook()
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
    with closing(connect(settings, generation, [count])) as connection:
        stored = read_meta(connection)
    assert (stored.ontology_iri, stored.version_iri) == ("http://example.org/family", None)


def pinged_fuseki(pings: list[float], *statuses: int) -> httpx2.AsyncClient:
    """Return an HTTP client whose stubbed Fuseki answers pings with these statuses in turn, then 200, recording when.

    It answers a query with `ONTOLOGY_RESULT`; one before a ping succeeds fails the test.
    """
    answers = list(statuses)

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/$/ping":
            pings.append(time.monotonic())
            status = answers.pop(0) if answers else 200
            if status == 0:
                raise httpx2.ConnectError("refused", request=request)
            return httpx2.Response(status)
        assert not answers, "queried before Fuseki answered"
        return httpx2.Response(200, headers={"Content-Type": TSV}, content=ONTOLOGY_RESULT)

    return httpx2.AsyncClient(base_url="http://fuseki:3030", transport=httpx2.MockTransport(handler))


async def test_rebuild_waits_for_fuseki(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a rebuild after a restart waits for Fuseki to answer its ping, pausing longer each time."""
    monkeypatch.setattr(stage, "FUSEKI_RETRY_S", (0.02, 0.04))
    pings: list[float] = []
    reports: list[tuple[str, int]] = []
    context = job(settings, 3)
    context.on_progress = lambda detail, fraction, iri: reports.append((detail, len(pings)))
    async with pinged_fuseki(pings, 0, 503, 0, 0) as http:
        await rebuild(context, http, hooks=[])
    assert len(pings) == 5
    assert reports[0] == ("Waiting for Fuseki", 0)
    assert reports[1] == ("Reading the ontology's header", 5)
    pauses = [later - earlier for earlier, later in itertools.pairwise(pings)]
    assert pauses[0] >= 0.02 and pauses[1] >= 0.04 and pauses[3] >= 0.04
    assert is_current(settings, 3, [])


async def test_rebuild_fuseki_down(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a rebuild fails, sending no query, if Fuseki doesn't answer its ping in time."""
    monkeypatch.setattr(stage, "FUSEKI_WAIT_S", 0.1)
    monkeypatch.setattr(stage, "FUSEKI_RETRY_S", (0.02, 0.02))
    pings: list[float] = []
    async with pinged_fuseki(pings, *[0] * 1000) as http:
        with pytest.raises(StageError, match=r"^Fuseki didn't answer its ping within 0.1 s$"):
            await rebuild(job(settings, 3), http, hooks=[])
    assert len(pings) > 2
    assert derived_files(settings) == []


async def test_ontology_header_on_real_fuseki(fuseki_url: str, settings: Settings) -> None:
    """Check on a real Fuseki that the ontology's IRIs and title come only from its own graph, not what it imports.

    The default graph also declares an ontology it imports, as an OFN upload's converted Turtle may, and a blank-node
    ontology; an import's named graph declares one more, and gives the main ontology a title. Each would come first in
    order, so would be taken, if the query let it through.
    """
    name = dataset_name(900_000 + uuid.uuid4().int % 100_000)
    own = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{EX}main> a owl:Ontology ; owl:versionIRI <{EX}main/1> ; owl:imports <{EX}a-imported> ;
    dcterms:title "Main"@en ; owl:versionInfo "1.0" .
<{EX}a-imported> a owl:Ontology ; owl:versionIRI <{EX}a-imported/1> .
[] a owl:Ontology ; owl:versionIRI <{EX}0-blank/1> .
"""
    imported = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
<{EX}0-in-import> a owl:Ontology .
<{EX}main> dcterms:title "A title from the import"@en .
"""
    # RadLex's header: a title and version info, but no version IRI.
    radlex = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
<http://radlex.org/RID/> a owl:Ontology ; dcterms:title "RadLex radiology ontology"@en ; owl:versionInfo "4.4-dev" .
"""
    async with httpx2.AsyncClient(base_url=fuseki_url, timeout=60) as http:
        datasets = [(name, [(None, own), (f"{EX}a-imported", imported)]), (f"{name}-radlex", [(None, radlex)])]
        try:
            for dataset, graphs in datasets:
                (await http.post("/$/datasets", data={"dbName": dataset, "dbType": "mem"})).raise_for_status()
                for graph, content in graphs:
                    params = {} if graph is None else {"graph": graph}
                    response = await http.post(
                        f"/{dataset}/data",
                        params=params,
                        content=content.encode(),
                        headers={"Content-Type": "text/turtle"},
                    )
                    response.raise_for_status()
            sparql = FusekiClient(http)
            assert await ontology_header(sparql, name, settings) == OntologyHeader(
                f"{EX}main", f"{EX}main/1", "Main", "1.0"
            )
            header = await ontology_header(sparql, f"{name}-radlex", settings)
            assert header == OntologyHeader("http://radlex.org/RID/", None, "RadLex radiology ontology", "4.4-dev")
        finally:
            for dataset, _ in datasets:
                await http.delete(f"/$/datasets/{dataset}")


async def test_build_fails_to_start(tmp_path: Path) -> None:
    """Check that a build that can't create its file fails with the error, and has nothing to clean up."""
    data_dir = tmp_path / "file"
    data_dir.write_text("")
    with pytest.raises(OSError, match="file"):
        async with build(data_dir, Meta(1, Reasoner.NONE, LOADED_AT), {}):
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
    """A stubbed Fuseki that answers every query with its `answer`, or `status`, once released, recording them.

    It answers a ping at once.

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
        """Answer a request once released; a ping at once."""
        self.requests.append(request.url.path)
        if request.url.path == "/$/ping":
            return httpx2.Response(200)
        while not self.released.is_set():
            await asyncio.sleep(0.01)
        if self.status != 200:
            return httpx2.Response(self.status, text="boom")
        query = urllib.parse.parse_qs(request.content.decode())["query"][0]
        return httpx2.Response(200, headers={"Content-Type": TSV}, content=answer(query))


def restart(fuseki: StubFuseki) -> FastAPI:
    """Return an app, with the app's own stages and the stubbed Fuseki, and a route that reads the derived database."""
    app = make_app(fuseki.transport, real_stages=True)

    @app.get("/probe")
    async def probe(request: Request) -> int:
        return await read_derived(request, lambda connection: read_meta(connection).generation)

    app.router.routes.insert(0, app.router.routes.pop())  # ahead of the catch-all route for entity URLs
    return app


def status(client: TestClient) -> str:
    """Return the admin status page."""
    return client.get("/_browl/admin/status").text


def test_restart_reuses_current_database(settings: Settings) -> None:
    """Check that a restart reuses a current derived database: no query, no rebuild, and pages read it.

    Shutdown closes the reader's connections.
    """
    JobStore(settings.data_dir / STATE_DB).write(READY)
    make_previous(settings, 2)
    fuseki = StubFuseki()
    app = restart(fuseki)
    with TestClient(app) as client:
        assert app.state.jobs.state == READY
        assert client.get("/probe").json() == 2
        [connection] = app.state.derived._idle
    assert fuseki.requests == []
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def stale_schema(settings: Settings) -> None:
    """Leave generation 2 a derived database built with another schema."""
    with sqlite3.connect(make_previous(settings, 2)) as connection:
        connection.execute("UPDATE build_key SET key = 'stale' WHERE component = 'schema'")


@pytest.mark.parametrize(
    "prepare",
    [
        pytest.param(lambda settings: None, id="missing"),
        pytest.param(stale_schema, id="stale schema"),
        pytest.param(lambda settings: make_previous(settings, 1), id="other generation"),
        pytest.param(
            lambda settings: make_previous(make_settings(BASE_URL="http://example.com/"), 2), id="other base url"
        ),
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
        assert '<span class="step-label">Pre-Caching</span>' in status(client)
        fuseki.released.set()
        assert client.portal is not None
        client.portal.call(app.state.jobs.wait)
        state = app.state.jobs.state
        assert (state.generation, state.stage, state.ready_generation, state.started_at) == (2, Stage.READY, 2, STARTED)
        assert client.get("/probe").json() == 2
    # The header's, then the hooks': none for search's, two for the annotation values (values, sub-properties)
    queries = 1 + sum({search: 0, annotation_values: 2, axiom_counts: 5}.get(hook, 1) for hook in HOOKS)
    assert fuseki.requests == ["/$/ping", *[f"/{dataset_name(2)}/sparql"] * queries]
    assert derived_files(settings) == ["2.sqlite3"]
    with closing(connect(settings, 2)) as connection:
        assert read_meta(connection) == Meta(2, Reasoner.ELK, STARTED, ONTOLOGY, VERSION, "Onto", "1.0")


@pytest.mark.parametrize(
    ("stored", "upload", "expected"),
    [
        pytest.param({"o": f"{EX}onto#"}, None, {"o": f"{EX}onto#"}, id="stored"),
        pytest.param({"o": f"{EX}onto#"}, f"@prefix f: <{EX}onto#> .\n", {"o": f"{EX}onto#"}, id="stored, not read"),
        pytest.param({}, f"@prefix f: <{EX}onto#> .\n", {"f": f"{EX}onto#"}, id="read again"),
        pytest.param({}, None, {}, id="none"),
    ],
)
def test_restart_rebuild_keeps_prefixes(
    settings: Settings, stored: dict[str, str], upload: str | None, expected: dict[str, str]
) -> None:
    """Check that a rebuild uses the job's stored prefixes, or reads them again from its upload if none are stored (S2).

    The job state then stores them, and the entities get their CURIEs.
    """
    JobStore(settings.data_dir / STATE_DB).write(replace(READY, prefixes=stored))
    if upload is not None:
        path = settings.data_dir / "uploads" / "2" / "ontology.ttl"
        path.parent.mkdir(parents=True)
        path.write_text(upload)
    app = restart(StubFuseki())
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(app.state.jobs.wait)
    assert JobStore(settings.data_dir / STATE_DB).read().prefixes == expected
    with closing(connect(settings, 2)) as connection:
        assert connection.execute("SELECT curie FROM entity WHERE iri = ?", (f"{EX}onto#A",)).fetchone() == (
            f"{next(iter(expected))}:A" if expected else None,
        )
        found = dict(connection.execute("SELECT prefix, namespace FROM prefix").fetchall())
    assert found.items() >= expected.items()


def test_restart_rebuild_fails(settings: Settings) -> None:
    """Check that a failed rebuild leaves the site "not available", and shows admins why."""
    JobStore(settings.data_dir / STATE_DB).write(READY)
    app = restart(StubFuseki(status=500))
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(app.state.jobs.wait)
        response = client.get("/probe")
        assert response.status_code == 503 and "<h1>Not available</h1>" in response.text
        assert "<strong>Pre-Caching Failed</strong>: rebuilding after a restart: pre-caching query" in (status(client))
    assert derived_files(settings) == []


def test_restart_rebuild_shutdown(settings: Settings) -> None:
    """Check that a shutdown during a rebuild cancels it, recording why, and leaves no file behind."""
    JobStore(settings.data_dir / STATE_DB).write(READY)
    fuseki = StubFuseki(released=False)
    with TestClient(restart(fuseki)):
        wait_until(lambda: len(fuseki.requests) == 2)  # the ping, then the query
    state = JobStore(settings.data_dir / STATE_DB).read()
    assert (state.failed_stage, state.reason) == (
        Stage.PRE_CACHING,
        f"{REBUILDING}: {INTERRUPTED_BY_SHUTDOWN}",
    )
    assert derived_files(settings) == []


async def test_restart_rebuild_shutdown_during_swap(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a shutdown while a rebuild swaps its database in waits for the swap, under the job lock.

    The rebuild is recorded as interrupted, but its database is current, so the next start records the job ready.
    """
    JobStore(settings.data_dir / STATE_DB).write(READY)
    renaming, release = threading.Event(), threading.Event()
    rename = derived.os.replace

    def blocked(source: Path, target: Path) -> None:
        renaming.set()
        release.wait(10)
        rename(source, target)

    monkeypatch.setattr(derived.os, "replace", blocked)
    current = functools.partial(derived.is_current, settings.data_dir, keys=build_keys(settings, HOOKS))
    async with fuseki() as http:
        function = functools.partial(pre_cache, http=http)
        runner = await JobRunner.open(settings, on_ready=remove_previous)
        assert await runner.rebuild(function, current)
        assert await asyncio.to_thread(renaming.wait, 10)
        closed = asyncio.create_task(runner.close())
        await asyncio.sleep(0.05)
        assert not closed.done()
        assert not JobLock(settings.data_dir / JOB_LOCK).acquire()
        release.set()
        await closed
        state = JobStore(settings.data_dir / STATE_DB).read()
        assert (state.failed_stage, state.reason) == (Stage.PRE_CACHING, f"{REBUILDING}: {INTERRUPTED_BY_SHUTDOWN}")
        assert derived_files(settings) == ["2.sqlite3"]
        monkeypatch.undo()
        restarted = await JobRunner.open(settings, on_ready=remove_previous)
        assert restarted.state.site == Site.NOT_AVAILABLE
        assert not await restarted.rebuild(function, current)
    assert (restarted.state.stage, restarted.state.ready_generation, restarted.state.site) == (
        Stage.READY,
        2,
        Site.AVAILABLE,
    )
    assert JobStore(settings.data_dir / STATE_DB).read() == restarted.state


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


def entity_rows(
    settings: Settings, generation: int, hooks: Sequence[Hook] = (entities,)
) -> list[tuple[str, str, str, str | None]]:
    """Return a generation's `entity` rows, as (IRI, type, path, fragment), in id order, built with these hooks."""
    with closing(connect(settings, generation, hooks)) as connection:
        return connection.execute("SELECT iri, type, path, fragment FROM entity ORDER BY id").fetchall()


def hash_prefixes(settings: Settings, generation: int, hooks: Sequence[Hook] = (entities,)) -> list[str]:
    """Return a generation's `hash_prefix` paths, in stored order, from a build with these hooks."""
    with closing(connect(settings, generation, hooks)) as connection:
        return [row[0] for row in connection.execute("SELECT path FROM hash_prefix")]


async def test_entities(settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    """Check that the entities hook lists the ontology and each declared entity with its URL, and the hash prefixes.

    IRIs that would be under the reserved prefix get fallback URLs, with a warning listing them (O-3); the `http` and
    `https` forms of an IRI share a URL, and both are found there (U-15).
    """
    base = "http://example.org"  # make_settings's BASE_URL
    assert str(settings.base_url) == f"{base}/"
    declared = [
        (f"{base}/onto#A", "Class"),
        (f"{base}/onto#A", "ObjectProperty"),  # punning
        (f"{base}/onto#p", "DatatypeProperty"),
        (f"{base}/onto/B", "AnnotationProperty"),
        ("http://example.net/x", "Class"),
        ("https://example.net/x", "Class"),
        ("http://example.net/other#C", "Class"),
        (f"{base}/_browl/admin", "Class"),
        ("urn:x:y", "Class"),
    ]
    rows = "".join(f"<{iri}>\t<{OWL}{kind}>\t\n" for iri, kind in declared)
    queries: list[str] = []
    caplog.set_level(logging.WARNING, logger="browl.precache")
    ontology = f"{HEADER}<{base}/onto>\t\t\t\n{END}"
    async with fuseki(ontology, f"{ENTITY_HEADER}{rows}{ENTITY_END}", queries=queries) as http:
        await pre_caching_stage(job(settings, 2, [ImportResult(f"{EX}i", triples=1)]), http, hooks=[entities])
    assert UNION_FROM in queries[1]  # the imports' entities too
    assert entity_rows(settings, 2) == [
        (f"{base}/onto", "ontology", "/onto", None),
        (f"{base}/onto#A", "class", "/onto", "A"),
        (f"{base}/onto#A", "object_property", "/onto", "A"),
        (f"{base}/onto#p", "data_property", "/onto", "p"),
        (f"{base}/onto/B", "annotation_property", "/onto/B", None),
        ("http://example.net/x", "class", "/example.net/x", None),
        ("https://example.net/x", "class", "/example.net/x", None),
        ("http://example.net/other#C", "class", "/example.net/other", "C"),
        (f"{base}/_browl/admin", "class", "/_browl/iri?iri=http%3A%2F%2Fexample.org%2F_browl%2Fadmin", None),
        ("urn:x:y", "class", "/_browl/iri?iri=urn%3Ax%3Ay", None),
    ]
    assert hash_prefixes(settings, 2) == ["/example.net/other", "/onto"]
    warning = "1 entity IRIs would have URLs under the reserved prefix /_browl, so they get fallback URLs:"
    assert caplog.messages == [f"{warning} ['{base}/_browl/admin']"]
    with closing(connect(settings, 2, [entities])) as connection:
        shared = entities_at(connection, "/example.net/x")
        assert [(entity.iri, entity.type) for entity in shared] == [
            ("http://example.net/x", EntityType.CLASS),
            ("https://example.net/x", EntityType.CLASS),
        ]
        assert [entity.type for entity in entities_at(connection, "/onto", "A")] == ["class", "object_property"]
        assert [entity.iri for entity in entities_at(connection, "/onto")] == [f"{base}/onto"]
        assert entities_at(connection, "/nothing") == []


async def test_entities_without_ontology_iri(settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    """Check that an ontology without an IRI has no row, and that a warning lists only the first reserved IRIs."""
    rows = "".join(f"<http://example.org/_browl/{n}>\t<{OWL}Class>\t\n" for n in range(12))
    caplog.set_level(logging.WARNING, logger="browl.precache")
    async with fuseki(HEADER + END, f"{ENTITY_HEADER}{rows}{ENTITY_END}") as http:
        await pre_caching_stage(job(settings, 3), http, hooks=[entities])
    assert len(entity_rows(settings, 3)) == 12
    assert hash_prefixes(settings, 3) == []
    [message] = caplog.messages
    assert message.startswith("12 entity IRIs would have URLs under the reserved prefix /_browl")
    assert message.endswith("_browl/9'] …") and "/10'" not in message


async def test_entities_in_batches(
    settings: Settings, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that entities in several batches are all stored, and reserved IRIs counted across them and the ontology."""
    sizes: list[int] = []

    async def small_batches(rows: AsyncIterator[object]) -> AsyncIterator[list[object]]:
        async for batch in batched(rows, size=2):
            sizes.append(len(batch))
            yield batch

    monkeypatch.setattr(sys.modules[entities.module], "batched", small_batches)
    reserved = "http://example.org/_browl/"
    iris = [f"{reserved}a", f"{EX}b", f"{EX}c", f"{reserved}d", f"{reserved}e"]
    rows = "".join(f"<{iri}>\t<{OWL}Class>\t\n" for iri in iris)
    caplog.set_level(logging.WARNING, logger="browl.precache")
    async with fuseki(f"{HEADER}<{reserved}onto>\t\t\t\n{END}", f"{ENTITY_HEADER}{rows}{ENTITY_END}") as http:
        await pre_caching_stage(job(settings, 4), http, hooks=[entities])
    assert sizes == [2, 2, 1]
    assert [row[0] for row in entity_rows(settings, 4)] == [f"{reserved}onto", *iris]
    reserved_iris = [f"{reserved}onto", f"{reserved}a", f"{reserved}d", f"{reserved}e"]
    assert caplog.messages == [
        f"4 entity IRIs would have URLs under the reserved prefix /_browl, so they get fallback URLs: {reserved_iris!r}"
    ]


async def test_entities_warning_shortens_iris(settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    """Check that the reserved-IRI warning shows only the start of a long IRI, so its line stays short."""
    long = f"http://example.org/_browl/{'x' * 10_000}"
    caplog.set_level(logging.WARNING, logger="browl.precache")
    async with fuseki(HEADER + END, f"{ENTITY_HEADER}<{long}>\t<{OWL}Class>\t\n{ENTITY_END}") as http:
        await pre_caching_stage(job(settings, 5), http, hooks=[entities])
    [message] = caplog.messages
    assert message.endswith(f"{[long[:300] + '…']!r}")
    assert len(message) < 500


async def test_entities_on_real_fuseki(fuseki_url: str, settings: Settings) -> None:
    """Check the entities hook on a real Fuseki: the ontology, and the classes and properties of it and its imports.

    Blank-node classes and named individuals are left out. The ontology is under `BASE_URL`, so its URLs are native.
    """
    generation = 900_000 + uuid.uuid4().int % 100_000
    name = dataset_name(generation)
    family = "http://example.org/family"
    imported = (
        f"<{family}#age> a <{OWL}DatatypeProperty> .\n<{family}#alice> a <{OWL}NamedIndividual>, <{family}#Person> .\n"
        f"<https://example.org/family#Person> a <{OWL}Class> .\n"
    )
    async with httpx2.AsyncClient(base_url=fuseki_url, timeout=60) as http:
        (await http.post("/$/datasets", data={"dbName": name, "dbType": "mem"})).raise_for_status()
        try:
            for graph, content in [(None, (DATA / "family.ttl").read_bytes()), (f"{EX}imported", imported.encode())]:
                response = await http.post(
                    f"/{name}/data",
                    params={} if graph is None else {"graph": graph},
                    content=content,
                    headers={"Content-Type": "text/turtle"},
                )
                response.raise_for_status()
            await pre_caching_stage(job(settings, generation, [ImportResult(f"{EX}imported", triples=4)]), http)
        finally:
            await http.delete(f"/$/datasets/{name}")
    rows = entity_rows(settings, generation, HOOKS)
    assert rows[0] == (family, "ontology", "/family", None)
    assert sorted(rows[1:]) == [
        (f"{family}#Female", "class", "/family", "Female"),
        (f"{family}#Male", "class", "/family", "Male"),
        (f"{family}#Mother", "class", "/family", "Mother"),
        (f"{family}#Parent", "class", "/family", "Parent"),
        (f"{family}#Person", "class", "/family", "Person"),
        (f"{family}#age", "data_property", "/family", "age"),
        (f"{family}#hasChild", "object_property", "/family", "hasChild"),
        (f"{family}#rank", "annotation_property", "/family", "rank"),
        ("https://example.org/family#Person", "class", "/family", "Person"),
    ]
    assert hash_prefixes(settings, generation, HOOKS) == ["/family"]


# The usage tables' builds (P-27c), which the app starts.


def usage_files(settings: Settings) -> list[str]:
    """Return the names of the files in the usage tables' directory, but the build lock."""
    directory = settings.data_dir / "usages"
    return sorted(path.name for path in directory.iterdir() if path.name != "build.lock") if directory.is_dir() else []


def test_restart_builds_usage_table(settings: Settings) -> None:
    """Check that a restart builds the ready generation's usage table once its derived database is current.

    The next restart, finding it current, sends no query.
    """
    JobStore(settings.data_dir / STATE_DB).write(READY)
    make_previous(settings, 2)
    fuseki = StubFuseki()
    app = make_app(fuseki.transport, real_stages=True, usage_tables=True)
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(app.state.usage_tables.wait)
        assert usage_tables.is_current(settings.data_dir, 2, app.state.usage_tables.key)
    assert fuseki.requests == [f"/{dataset_name(2)}/sparql"] * 2  # the count and the scan
    assert usage_files(settings) == ["2.sqlite3"]
    again = StubFuseki()
    app = make_app(again.transport, real_stages=True, usage_tables=True)
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(app.state.usage_tables.wait)
    assert again.requests == []


def test_restart_rebuild_then_usage_table(settings: Settings) -> None:
    """Check that a restart that rebuilds the derived database builds the usage table after, from the new file."""
    JobStore(settings.data_dir / STATE_DB).write(READY)
    fuseki = StubFuseki()
    app = make_app(fuseki.transport, real_stages=True, usage_tables=True)
    with TestClient(app) as client:
        assert client.portal is not None
        client.portal.call(app.state.jobs.wait)
        client.portal.call(app.state.usage_tables.wait)
        assert usage_tables.is_current(settings.data_dir, 2, app.state.usage_tables.key)
    assert fuseki.requests[-2:] == [f"/{dataset_name(2)}/sparql"] * 2


def test_restart_without_ready_generation(settings: Settings) -> None:
    """Check that a restart with no ready generation, or one whose derived database isn't current, builds no table."""
    fuseki = StubFuseki()
    app = make_app(fuseki.transport, usage_tables=True)  # stages given, so no rebuild
    with TestClient(app):
        assert not app.state.usage_tables.building(2)
    JobStore(settings.data_dir / STATE_DB).write(READY)
    with TestClient(make_app(fuseki.transport, usage_tables=True)) as client:
        assert not client.app.state.usage_tables.building(2)  # type: ignore[attr-defined]
    assert fuseki.requests == []
    assert usage_files(settings) == []


def test_ready_job_builds_usage_table(settings: Settings) -> None:
    """Check that each job reaching ready builds its usage table, and the older generation's is deleted."""
    fuseki = StubFuseki()
    app = make_app(fuseki.transport, usage_tables=True)
    with TestClient(app) as client:
        assert client.portal is not None
        jobs = app.state.jobs
        jobs.register(Stage.PRE_CACHING, functools.partial(pre_cache, http=app.state.fuseki_http))
        for _ in range(2):
            client.portal.call(jobs.start)
            client.portal.call(jobs.wait)
        client.portal.call(app.state.usage_tables.wait)
        generation = jobs.state.ready_generation
        assert usage_tables.is_current(settings.data_dir, generation, app.state.usage_tables.key)
    assert usage_files(settings) == [f"{generation}.sqlite3"]
