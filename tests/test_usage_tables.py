"""Tests for the usage table (P-27c): its build, its rows against the streamed usages, and its background lifecycle."""

import asyncio
import sqlite3
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, closing
from pathlib import Path

import httpx2
import pytest

import browl.precache.usage_table
import browl.usage_tables
import browl.usages
from browl import owlapi
from browl.fuseki import UNION_FROM, FusekiClient, SparqlError
from browl.precache.keys import build_keys
from browl.precache.stage import HOOKS, on_thread
from browl.precache.usage_table import Progress, UsageKind
from browl.rdf import IRI, OWL, RDF, RDFS, Literal
from browl.settings import Settings
from browl.usage_tab import sorted_usages
from browl.usage_tables import (
    TableNotReadyError,
    UsageTables,
    build_file,
    connect,
    is_current,
    remove_others,
    table_path,
)
from browl.usages import UsageStore, count_usages, read_table_page, read_usages
from tests.helpers import connect as connect_derived
from tests.helpers import loaded, make_settings, ontology_ntriples

pytestmark = [pytest.mark.anyio]

NS = "http://example.org/axioms#"
OWL_CLASS = f"<{OWL}Class>"
RDF_TYPE = f"<{RDF}type>"


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the module's async tests and fixtures on asyncio, as the module-scoped fixtures need one backend."""
    return "asyncio"


@pytest.fixture(scope="module")
def ntriples(jvm: None) -> bytes:
    """Return the test ontology as N-Triples."""
    return asyncio.run(owlapi.run_java(ontology_ntriples))


@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Settings]:
    """Yield settings with a data directory of the module's own, as `DATA_DIR` too."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATA_DIR", str(tmp_path_factory.mktemp("data")))
        yield make_settings()


@asynccontextmanager
async def with_table(store: UsageStore, settings: Settings) -> AsyncIterator[UsageStore]:
    """Yield a store whose usage table is built."""
    tables = UsageTables(settings.data_dir, store.sparql, build_keys(settings, HOOKS))
    try:
        await tables.start(store.generation, store.dataset_clause)
        await tables.wait()
        assert not tables.failed(store.generation)
        yield UsageStore(store.sparql, store.derived, store.generation, store.dataset_clause, tables)
    finally:
        await tables.close()


@pytest.fixture(scope="module")
async def plain(fuseki_url: str, ntriples: bytes, settings: Settings) -> AsyncIterator[UsageStore]:
    """Yield a store of the test ontology."""
    async with loaded(fuseki_url, settings, {None: ntriples}, "") as found:
        yield found


@pytest.fixture
async def store(plain: UsageStore, settings: Settings) -> AsyncIterator[UsageStore]:
    """Yield the store of the test ontology with its usage table, built again if another test's build deleted it."""
    async with with_table(plain, settings) as tabled:
        yield tabled


def entity_iris(settings: Settings, store: UsageStore) -> list[str]:
    """Return the IRIs of the store's entities."""
    with closing(connect_derived(settings, store.generation)) as connection:
        return sorted({iri for (iri,) in connection.execute("SELECT iri FROM entity")})


async def streamed(store: UsageStore, iri: str) -> tuple[int, list]:
    """Return an entity's count and usages in the tab's order, as they're read before the table is ready."""
    plain = UsageStore(store.sparql, store.derived, store.generation, store.dataset_clause)
    usages = await read_usages(plain, iri)
    ordered = await store.derived.read(store.generation, lambda c: sorted_usages(c, usages))
    return await count_usages(plain, iri), ordered


async def assert_table_matches(store: UsageStore, iris: list[str]) -> None:
    """Check that each entity's count, and its usages at every position, are the table's as they're streamed."""
    for iri in iris:
        count, ordered = await streamed(store, iri)
        page = await read_table_page(store, iri, 0, len(ordered) + 5)
        assert page is not None
        assert (page.count, page.listed) == (count, len(ordered)), iri
        assert page.usages == ordered, iri
        assert await count_usages(store, iri) == count, iri  # read from the table
        for start in range(1, len(ordered)):
            part = await read_table_page(store, iri, start, start + 2)
            assert part is not None
            assert (part.previous, part.usages) == (ordered[start - 1], ordered[start : start + 2]), (iri, start)


@pytest.mark.fuseki
@pytest.mark.jvm
async def test_matches_streamed(store: UsageStore, settings: Settings) -> None:
    """Check every entity of the test ontology, which has every kind of axiom: the table holds what's streamed."""
    iris = entity_iris(settings, store)
    assert len(iris) > 40
    await assert_table_matches(store, iris)
    assert await read_table_page(store, "http://example.org/nothing", 0, 1) is None


@pytest.mark.fuseki
@pytest.mark.jvm
async def test_imports(fuseki_url: str, ntriples: bytes, settings: Settings) -> None:
    """Check that with some triples in a named graph, as an import's, the table sees them, as the stream does."""
    subjects = (f"<{NS}Dog> ", f"<{NS}Cat> ", f"<{NS}Liver> ", f"<{NS}remark> ")
    lines = ntriples.decode().splitlines(keepends=True)
    moved = [line.startswith(subjects) and " _:" not in line for line in lines]
    named = "".join(line for line, move in zip(lines, moved, strict=True) if move).encode()
    rest = "".join(line for line, move in zip(lines, moved, strict=True) if not move).encode()
    graphs = {None: rest, "http://example.org/imported": named}
    async with loaded(fuseki_url, settings, graphs, UNION_FROM) as split, with_table(split, settings) as tabled:
        await assert_table_matches(tabled, [f"{NS}{name}" for name in ("Animal", "remark", "Dog", "note", "age")])


@pytest.mark.fuseki
@pytest.mark.jvm
async def test_anonymous_individuals(fuseki_url: str, settings: Settings) -> None:
    """Check that usages in triples with a blank subject the owner map lacks are counted, not listed, as streamed."""
    a, remark = f"<{NS}A>", f"<{NS}remark>"
    triples = (
        f"{a} {RDF_TYPE} {OWL_CLASS} .\n{remark} {RDF_TYPE} <{OWL}AnnotationProperty> .\n"
        f'_:x {RDF_TYPE} {a} .\n_:x {remark} "hi" .\n<{NS}B> {RDF_TYPE} {OWL_CLASS} .\n<{NS}B> {remark} {a} .\n'
    )
    async with (
        loaded(fuseki_url, settings, {None: triples.encode()}, "") as found,
        with_table(found, settings) as tabled,
    ):
        await assert_table_matches(tabled, [f"{NS}A", f"{NS}remark"])
        page = await read_table_page(tabled, f"{NS}A", 0, 50)
        assert page is not None and (page.count, page.listed) == (2, 1)


@pytest.mark.fuseki
@pytest.mark.jvm
async def test_kinds(store: UsageStore) -> None:
    """Check that each usage stores its axiom's kind and predicate, for U-34's filters."""
    assert store.tables is not None

    def kinds(connection: sqlite3.Connection) -> set[tuple[int, str | None]]:
        return set(
            connection.execute(
                "SELECT u.kind, t.iri FROM usage AS u LEFT JOIN term AS t ON t.id = u.predicate"
            ).fetchall()
        )

    found = await store.tables.read(store.generation, kinds)
    assert (UsageKind.SUB_CLASS_OF, f"{RDFS}subClassOf") in found
    assert (UsageKind.TYPE, f"{RDF}type") in found
    assert (UsageKind.ASSERTION, f"{NS}remark") in found
    assert (UsageKind.AXIOM_ANNOTATION, f"{NS}remark") in found or any(
        kind == UsageKind.AXIOM_ANNOTATION for kind, _ in found
    )
    assert (UsageKind.NO_SUBJECT, None) in found
    assert {kind for kind, _ in found} >= {UsageKind.DOMAIN, UsageKind.RANGE, UsageKind.EQUIVALENT_CLASS}


# The files and the background builds, without Fuseki.


@pytest.fixture
def tables(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[UsageTables]:
    """Yield usage tables in a data directory of their own, whose builds fill nothing but wait on `GATES`."""
    monkeypatch.setattr(browl.usage_tables, "fill_usage_table", gated_fill)
    GATES.clear()
    yield UsageTables(tmp_path, FusekiClient(httpx2.AsyncClient()), {"schema": "x"})


GATES: dict[int, asyncio.Event] = {}
"""Per generation, what its stubbed build waits for; a generation without one fails."""


async def gated_fill(sparql: object, dataset: str, clause: str, db: object, progress: Progress) -> None:
    """Stand in for `fill_usage_table`: wait for the generation's gate, or fail if it has none."""
    generation = int(dataset.removeprefix("gen-"))
    if generation not in GATES:
        raise RuntimeError("no gate")
    progress.triples, progress.scanned = 100, 50
    await GATES[generation].wait()


def derived_file(data_dir: Path, generation: int) -> None:
    """Write an empty derived database for a generation, which the build attaches."""
    path = data_dir / "derived" / f"{generation}.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(path).close()


async def test_build_and_read(tables: UsageTables, tmp_path: Path) -> None:
    """Check that a build's file is read only once it's complete, and current for its generation and key only."""
    derived_file(tmp_path, 1)
    GATES[1] = asyncio.Event()
    await tables.start(1, "")
    await asyncio.sleep(0.05)
    assert tables.building(1)
    with pytest.raises(TableNotReadyError):
        await tables.read(1, lambda connection: None)
    assert not table_path(tmp_path, 1).exists()
    GATES[1].set()
    await tables.wait()
    assert not tables.building(1) and not tables.failed(1)
    assert await tables.read(1, lambda c: c.execute("SELECT generation FROM meta").fetchone()) == (1,)
    assert is_current(tmp_path, 1, tables.key)
    assert not is_current(tmp_path, 1, "other")
    assert not is_current(tmp_path, 2, tables.key)
    await tables.start(1, "")  # current: nothing to build
    assert not tables.building(1)
    await tables.close()


async def test_newer_cancels_older(tables: UsageTables, tmp_path: Path) -> None:
    """Check that a newer generation's build cancels an older one's, and deletes its files."""
    for generation in (1, 2):
        derived_file(tmp_path, generation)
        GATES[generation] = asyncio.Event()
    await tables.start(1, "")
    await asyncio.sleep(0.05)
    assert (tmp_path / "usages" / "1.sqlite3.tmp").exists()
    await tables.start(2, "")
    assert not tables.building(1)
    await asyncio.sleep(0.05)
    assert tables.building(2)
    assert not (tmp_path / "usages" / "1.sqlite3.tmp").exists()
    await tables.start(2, "")  # already building: nothing changes
    GATES[2].set()
    await tables.wait()
    assert is_current(tmp_path, 2, tables.key)
    await tables.close()


async def test_restart_builds_again(tables: UsageTables, tmp_path: Path, settings: Settings) -> None:
    """Check that a build interrupted by a restart leaves files that the next start deletes, building it again."""
    derived_file(tmp_path, 3)
    (tmp_path / "usages").mkdir()
    for name in ("3.sqlite3.tmp", "3.sqlite3.scratch.tmp", "2.sqlite3"):
        (tmp_path / "usages" / name).write_text("left")
    GATES[3] = asyncio.Event()
    GATES[3].set()
    await tables.start(3, "")
    await tables.wait()
    assert sorted(path.name for path in (tmp_path / "usages").iterdir()) == ["3.sqlite3", "build.lock"]
    assert is_current(tmp_path, 3, tables.key)


async def test_failure(tables: UsageTables, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Check that a failed build is logged and recorded, leaves no files, and is tried again only on a new start."""
    derived_file(tmp_path, 4)
    await tables.start(4, "")
    await tables.wait()
    assert tables.failed(4)
    assert "The usage table build of generation 4 failed" in caplog.text
    assert sorted(path.name for path in (tmp_path / "usages").iterdir()) == ["build.lock"]
    GATES[4] = asyncio.Event()
    GATES[4].set()
    await tables.start(4, "")
    assert not tables.failed(4)
    await tables.wait()
    assert is_current(tmp_path, 4, tables.key)


async def test_close_cancels(tables: UsageTables, tmp_path: Path) -> None:
    """Check that closing the tables cancels a running build, which leaves no files."""
    derived_file(tmp_path, 5)
    GATES[5] = asyncio.Event()
    await tables.start(5, "")
    await asyncio.sleep(0.05)
    await tables.close()
    assert not tables.building(5)
    assert sorted(path.name for path in (tmp_path / "usages").iterdir()) == ["build.lock"]


async def test_lock_waits(tables: UsageTables, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a build waits for the lock that another server worker's build holds."""
    monkeypatch.setattr(browl.usage_tables, "LOCK_RETRY_S", 0.01)
    derived_file(tmp_path, 6)
    (tmp_path / "usages").mkdir()
    other = browl.usage_tables.JobLock(tmp_path / "usages" / "build.lock")
    assert other.acquire()
    GATES[6] = asyncio.Event()
    GATES[6].set()
    await tables.start(6, "")
    await asyncio.sleep(0.1)
    assert tables.building(6)
    other.release()
    await tables.wait()
    assert is_current(tmp_path, 6, tables.key)


async def test_remaining(tables: UsageTables, tmp_path: Path) -> None:
    """Check the estimate of a build's time left: from the scan's rate, then the writing's share."""
    derived_file(tmp_path, 7)
    GATES[7] = asyncio.Event()
    assert tables.remaining_s(7) is None
    await tables.start(7, "")
    await asyncio.sleep(0.05)
    build = tables._build
    assert build is not None
    build.progress.started = 0.0
    # Half the triples in 10 s: 20 s of scan, `1 - WRITE_SHARE` of the whole.
    whole = 20 / (1 - browl.usage_tables.WRITE_SHARE)
    assert tables.remaining_s(7, now=10.0) == pytest.approx(whole - 10)
    build.progress.scanned_at = 20.0
    assert tables.remaining_s(7, now=25.0) == pytest.approx(whole - 25)
    assert tables.remaining_s(7, now=1000.0) == 0
    assert tables.remaining_s(8) is None
    build.progress.scanned_at, build.progress.scanned = None, 0
    assert tables.remaining_s(7) is None
    GATES[7].set()
    await tables.close()


def test_remove_others(tmp_path: Path) -> None:
    """Check that every table but one generation's is deleted, with every temporary file, and nothing else."""
    directory = tmp_path / "usages"
    directory.mkdir()
    names = ["1.sqlite3", "2.sqlite3", "3.sqlite3.tmp", "2.sqlite3.scratch.tmp", "build.lock", "notes.txt", "x.sqlite3"]
    for name in names:
        (directory / name).write_text("")
    remove_others(tmp_path, 2)
    assert sorted(path.name for path in directory.iterdir()) == ["2.sqlite3", "build.lock", "notes.txt", "x.sqlite3"]
    remove_others(tmp_path / "missing", 1)


def test_build_file_failure(tmp_path: Path) -> None:
    """Check that a build that fails leaves no files, and a file already in place as it was."""
    derived_file(tmp_path, 1)
    with build_file(tmp_path, 1, "k"):
        pass
    before = table_path(tmp_path, 1).read_bytes()
    with pytest.raises(RuntimeError), build_file(tmp_path, 1, "k2"):
        raise RuntimeError
    assert table_path(tmp_path, 1).read_bytes() == before
    assert sorted(path.name for path in (tmp_path / "usages").iterdir()) == ["1.sqlite3"]
    with closing(connect(tmp_path, 1, "k")) as connection:
        assert connection.execute("SELECT count(*) FROM usage").fetchone() == (0,)
    with pytest.raises(TableNotReadyError, match="another generation or with another key"):
        connect(tmp_path, 1, "k2")
    table_path(tmp_path, 1).write_text("not a database")
    with pytest.raises(TableNotReadyError, match="can't be read"):
        connect(tmp_path, 1, "k")


def test_kinds_cover_vocabulary() -> None:
    """Check that the predicates with a kind of their own are OWL's, RDF's and RDFS's axiom predicates."""
    assert all(predicate.startswith((OWL, RDF, RDFS)) for predicate in browl.precache.usage_table.KINDS)
    assert len(set(browl.precache.usage_table.KINDS.values())) == len(browl.precache.usage_table.KINDS)


LONG = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n LIMIT 100000000) SELECT count(*) FROM n"
"""A statement that runs for many seconds."""


async def test_cancel_interrupts(tmp_path: Path) -> None:
    """Check that cancelling a build interrupts the statement it's running, so it ends at once, leaving no files."""
    derived_file(tmp_path, 1)
    endless = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n"

    async def build() -> None:
        async with on_thread(build_file(tmp_path, 1, "k"), "test", interrupt=True) as db:
            await db.run(lambda connection: connection.execute(endless).fetchone())

    task = asyncio.create_task(build())
    await asyncio.sleep(0.2)
    task.cancel()
    async with asyncio.timeout(5):
        with pytest.raises(asyncio.CancelledError):
            await task
    assert list((tmp_path / "usages").iterdir()) == []


async def test_cancel_stops_later_statements(tmp_path: Path) -> None:
    """Check that a statement that starts after a build is cancelled, as its queued work goes on, stops at once."""
    derived_file(tmp_path, 1)

    def later(connection: sqlite3.Connection) -> None:
        time.sleep(0.3)  # Python work, e.g. a sort, which the interrupt doesn't stop
        connection.execute(LONG).fetchone()

    async def build() -> None:
        async with on_thread(build_file(tmp_path, 1, "k"), "test", interrupt=True) as db:
            await db.run(later)

    task = asyncio.create_task(build())
    await asyncio.sleep(0.1)
    task.cancel()
    cancelled = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - cancelled < 2
    assert list((tmp_path / "usages").iterdir()) == []


def test_scan_rows_checked() -> None:
    """Check that a scan row the query can't give, or a count that isn't one, fails the build."""
    scan = browl.precache.usage_table._Scan({}, [])
    with pytest.raises(SparqlError, match="gave a predicate or object it can't"):
        scan.add([(IRI(f"{NS}a"), IRI(f"{NS}p"), Literal("x"))], None)  # type: ignore[arg-type]
    with pytest.raises(SparqlError, match="the triple count query gave"):
        browl.precache.usage_table._count([(IRI(f"{NS}a"),)])


def test_scan_leaves_out_vocabulary_types() -> None:
    """Check that the scan leaves out the OWL vocabulary's types that aren't entities, and none if rdf:type is one."""
    scan = browl.precache.usage_table._Scan({f"{OWL}Thing": 0, f"{OWL}Class": 1}, [1, 2])
    query = browl.precache.usage_table._scan_query(scan)
    assert f"<{OWL}ObjectProperty>" in query
    assert f"<{OWL}Class>" not in query  # an entity: its class assertions are usages
    scan.ranks[f"{RDF}type"] = 2
    assert "NOT IN (<urn:x-browl:none>)" in browl.precache.usage_table._scan_query(scan)


async def test_reads_by_generation(tables: UsageTables, tmp_path: Path) -> None:
    """Check that reads share the newest generation's connection, closing an older one's, which opens its own."""
    for generation in (1, 2):
        derived_file(tmp_path, generation)
        with build_file(tmp_path, generation, tables.key):
            pass

    def generation_of(connection: sqlite3.Connection) -> int:
        return connection.execute("SELECT generation FROM meta").fetchone()[0]

    assert await tables.read(1, generation_of) == 1
    old = tables._reading
    assert await tables.read(2, generation_of) == 2
    assert old is not None
    with pytest.raises(sqlite3.ProgrammingError):
        old[1].execute("SELECT 1")
    assert await tables.read(1, generation_of) == 1
    assert tables._reading is not None and tables._reading[0] == 2
    await tables.close()


async def test_built_elsewhere_meanwhile(tables: UsageTables, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a build that waited for the lock builds nothing if another worker built the table meanwhile."""
    monkeypatch.setattr(browl.usage_tables, "LOCK_RETRY_S", 0.01)
    derived_file(tmp_path, 8)
    (tmp_path / "usages").mkdir()
    other = browl.usage_tables.JobLock(tmp_path / "usages" / "build.lock")
    assert other.acquire()
    await tables.start(8, "")  # no gate: it would fail if it built
    await asyncio.sleep(0.05)
    with build_file(tmp_path, 8, tables.key):
        pass
    other.release()
    await tables.wait()
    assert not tables.failed(8)
    assert is_current(tmp_path, 8, tables.key)


def test_remove_others_leaves_what_it_cant_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Check that a directory, or a file that can't be deleted, is left, and the rest deleted."""
    directory = tmp_path / "usages"
    (directory / "x.tmp").mkdir(parents=True)
    for name in ("1.sqlite3", "2.sqlite3", "3.sqlite3"):
        (directory / name).write_text("")
    unlink = Path.unlink

    def guarded(path: Path, missing_ok: bool = False) -> None:
        if path.name == "1.sqlite3":
            raise PermissionError("not allowed")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", guarded)
    remove_others(tmp_path, 3)
    assert sorted(path.name for path in directory.iterdir()) == ["1.sqlite3", "3.sqlite3", "x.tmp"]
    assert "Couldn't delete" in caplog.text and "not allowed" in caplog.text and "x.tmp" not in caplog.text


@pytest.mark.fuseki
@pytest.mark.jvm
async def test_count_table_failure(
    store: UsageStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Check that a count the usage table fails to give is logged, and counted by Fuseki instead."""
    _, ordered = await streamed(store, f"{NS}Animal")

    async def fail(*args: object) -> None:
        raise sqlite3.DatabaseError("disk gone")

    monkeypatch.setattr(browl.usages, "read_table_page", fail)
    assert await count_usages(store, f"{NS}Animal") == len(ordered)
    assert "Couldn't read the count of http://example.org/axioms#Animal from the usage table" in caplog.text
