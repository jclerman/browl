"""Tests for the usage table (P-27c): its build, its rows against the streamed usages, and its background lifecycle."""

import asyncio
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, closing
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest

import browl.precache.usage_table
import browl.usage_tab
import browl.usage_tables
import browl.usages
from browl import owlapi
from browl.fuseki import UNION_FROM, FusekiClient, SparqlError
from browl.jobs import JobLock, JobState
from browl.precache.core import DerivedWriter
from browl.precache.keys import build_keys
from browl.precache.stage import HOOKS, on_thread
from browl.precache.usage_table import Progress, Source, UsageKind, fill_usage_table
from browl.rdf import IRI, OWL, RDF, RDFS, Literal
from browl.settings import Settings
from browl.usage_tab import PageNotFoundError, UsageTab, read_usage_tab, sorted_usages
from browl.usage_tables import (
    TableNotReadyError,
    UsageTables,
    build_file,
    connect,
    derived_stamp,
    is_current,
    remove_others,
    table_path,
)
from browl.usages import UsageCounts, UsageSource, UsageStore, count_usages, read_table_page, read_usages
from tests.helpers import connect as connect_derived
from tests.helpers import loaded, make_settings, ontology_ntriples

pytestmark = pytest.mark.anyio

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


async def tab_page(store: UsageStore, settings: Settings, iri: str, page: int) -> UsageTab:
    """Return a page of an entity's Usage tab, as the app reads it from the store."""
    state = SimpleNamespace(
        sparql=store.sparql,
        derived=store.derived,
        usage_tables=store.tables,
        usage_counts=UsageCounts(),
        settings=settings,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state), scope={"type": "http"})
    jobs = JobState(generation=store.generation, ready_generation=store.generation)
    return await read_usage_tab(request, jobs, iri, page)  # type: ignore[arg-type]


async def test_matches_streamed(store: UsageStore, settings: Settings) -> None:
    """Check every entity of the test ontology, which has every kind of axiom: the table holds what's streamed."""
    iris = entity_iris(settings, store)
    assert len(iris) > 40
    await assert_table_matches(store, iris)
    assert await read_table_page(store, "http://example.org/nothing", 0, 1) is None


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


async def test_anonymous_individuals(
    jvm: None, fuseki_url: str, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that usages in triples with a blank subject the owner map lacks are counted, not listed, as streamed.

    The tab's pages are of those listed: a page past them is a 404, not blank.
    """
    a, remark = f"<{NS}A>", f"<{NS}remark>"
    triples = (
        f"{a} {RDF_TYPE} {OWL_CLASS} .\n{remark} {RDF_TYPE} <{OWL}AnnotationProperty> .\n"
        f"{''.join(f'_:{blank} {RDF_TYPE} {a} .\n' for blank in 'wxyz')}"
        f'_:x {remark} "hi" .\n<{NS}B> {RDF_TYPE} {OWL_CLASS} .\n<{NS}B> {remark} {a} .\n'
    )
    monkeypatch.setattr(browl.usage_tab, "PAGE_USAGES", 1)
    async with (
        loaded(fuseki_url, settings, {None: triples.encode()}, "") as found,
        with_table(found, settings) as tabled,
    ):
        await assert_table_matches(tabled, [f"{NS}A", f"{NS}remark"])
        page = await read_table_page(tabled, f"{NS}A", 0, 50)
        assert page is not None and (page.count, page.listed) == (5, 1)
        first = await tab_page(tabled, settings, f"{NS}A", 1)
        assert (first.count, first.pages) == (5, 1)
        with pytest.raises(PageNotFoundError):
            await tab_page(tabled, settings, f"{NS}A", 2)


async def test_several_values(jvm: None, fuseki_url: str, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check a subject's several values of one property, IRIs, a literal and a blank node, through the table.

    Each is a usage by its place among the subject's values (`Usage.index`), so each page of one shows another axiom.
    """
    a, note = f"<{NS}A>", f"<{NS}note>"
    values = (f"<{NS}C>", f"<{NS}B>", '"lit"@en', "_:b")
    triples = (
        f"{a} {RDF_TYPE} {OWL_CLASS} .\n{note} {RDF_TYPE} <{OWL}AnnotationProperty> .\n"
        f"{''.join(f'{a} {note} {value} .\n' for value in values)}"
    )
    monkeypatch.setattr(browl.usage_tab, "PAGE_USAGES", 1)
    async with (
        loaded(fuseki_url, settings, {None: triples.encode()}, "") as found,
        with_table(found, settings) as tabled,
    ):
        await assert_table_matches(tabled, [f"{NS}A", f"{NS}note"])
        page = await read_table_page(tabled, f"{NS}note", 0, 50)
        assert page is not None
        assert [usage.index for usage in page.usages] == [0, 1, 2, 3]
        shown = []
        for number in range(1, 5):
            tab = await tab_page(tabled, settings, f"{NS}note", number)
            assert tab.groups is not None
            [group] = tab.groups
            [axiom] = group.axioms
            shown.append(axiom.manchester.removeprefix(f"<{NS}A> <{NS}note> "))
    # In N-Triples order: a literal's quote, then an IRI's `<`, then a blank node's `_:`.
    assert shown[:3] == ['"lit"@en', f"<{NS}B>", f"<{NS}C>"]
    assert shown[3].startswith("_:")


async def test_subjects_ordered(fuseki_url: str, settings: Settings) -> None:
    """Check the order of an entity's subjects through the table, against the stream.

    IRIs that are no entity's sort among themselves by IRI. A punned IRI sorts by its first entity's name.
    """
    a = f"<{NS}A>"
    subclass = f"<{RDFS}subClassOf>"
    triples = (
        f"{a} {RDF_TYPE} {OWL_CLASS} .\n"
        f"{''.join(f'<{NS}{name}> {subclass} {a} .\n' for name in 'zam')}"
        f"<{NS}X> {RDF_TYPE} {OWL_CLASS} .\n<{NS}X> {RDF_TYPE} <{OWL}ObjectProperty> .\n<{NS}X> {subclass} {a} .\n"
        f"<{NS}Y> {RDF_TYPE} {OWL_CLASS} .\n<{NS}Y> {subclass} {a} .\n"
    )
    async with loaded(fuseki_url, settings, {None: triples.encode()}, "") as found:
        path = browl.usage_tables.derived.derived_path(settings.data_dir, found.generation)
        with closing(sqlite3.connect(path)) as connection, connection:  # before any read, as reads are immutable
            pun = [number for (number,) in connection.execute("SELECT id FROM entity WHERE iri = ?", (f"{NS}X",))]
            assert len(pun) == 2
            names = [("zed", min(pun)), ("alpha", max(pun))]  # its first entity's name sorts it after Y's
            connection.executemany("UPDATE entity SET display_name = ? WHERE id = ?", names)
            connection.execute("UPDATE entity SET display_name = 'mid' WHERE iri = ?", (f"{NS}Y",))
        async with with_table(found, settings) as tabled:
            await assert_table_matches(tabled, [f"{NS}A"])
            page = await read_table_page(tabled, f"{NS}A", 0, 50)
    assert page is not None
    assert [usage.subject for usage in page.usages] == [f"{NS}{name}" for name in ("Y", "X", "a", "m", "z")]


async def test_kinds(store: UsageStore) -> None:
    """Check that each usage stores its axiom's kind and predicate, for U-34's filters, a root's from what it is."""
    tables = store.tables
    assert tables is not None
    names = await store.derived.read(store.generation, lambda c: dict(c.execute("SELECT id, iri FROM entity")))

    async def rows(name: str) -> list[tuple[str | None, Source, UsageKind, str | None]]:
        """Return an entity's usages' subjects, by local name if they're entities, sources, kinds and predicates."""
        entity = await store.derived.read(store.generation, lambda c: browl.usages._entity_id(c, f"{NS}{name}"))
        found = await tables.read(
            store.generation,
            lambda c: c.execute(
                "SELECT u.subject, s.iri, u.source, u.kind, p.iri FROM usage AS u"
                " LEFT JOIN term AS s ON s.id = u.subject_term LEFT JOIN term AS p ON p.id = u.predicate"
                " WHERE u.entity = ? ORDER BY u.position",
                (entity,),
            ).fetchall(),
        )
        return [
            (term if subject is None else names[subject].removeprefix(NS), Source(source), UsageKind(kind), predicate)
            for subject, term, source, kind, predicate in found
        ]

    subclass, general = f"{RDFS}subClassOf", (None, Source.ROOT, UsageKind.NO_SUBJECT, None)
    assert await rows("partOf") == [
        ("Heart", Source.ROOT, UsageKind.SUB_CLASS_OF, subclass),
        ("Fruit", Source.ROOT, UsageKind.SUB_CLASS_OF, subclass),
        ("hasPart", Source.OBJECT, UsageKind.INVERSE_OF, f"{OWL}inverseOf"),
        ("Kidney", Source.ROOT, UsageKind.SUB_CLASS_OF, subclass),
        ("Kidney", Source.ROOT, UsageKind.SUB_CLASS_OF, subclass),
        ("Liver", Source.ROOT, UsageKind.SUB_CLASS_OF, subclass),
        ("Mineral", Source.ROOT, UsageKind.DISJOINT_CLASS, f"{OWL}disjointWith"),
        general,
        general,
        general,
    ]
    annotation, comment = UsageKind.AXIOM_ANNOTATION, f"{NS}comment"
    assert await rows("note") == [
        ("Bladder", Source.ROOT, annotation, comment),
        ("Bladder", Source.ROOT, annotation, f"{OWL}disjointWith"),
        ("Cat", Source.ROOT, annotation, subclass),
        ("Kidney", Source.ROOT, annotation, comment),
        ("Kidney", Source.ROOT, annotation, comment),
        ("Kidney", Source.ROOT, annotation, subclass),
        ("Liver", Source.ROOT, annotation, subclass),
        general,
        general,
        (f"{NS}carol", Source.ROOT, annotation, f"{NS}knows"),  # individuals have no entity row until P-47
        (f"{NS}carol", Source.ROOT, annotation, f"{NS}weight"),
    ]
    animal = await rows("Animal")
    assert ("Dog", Source.OBJECT, UsageKind.TYPE, f"{RDF}type") in animal
    assert ("note", Source.OBJECT, UsageKind.DOMAIN, f"{RDFS}domain") in animal
    assert animal[-1] == general
    assert ("Cat", Source.PREDICATE, UsageKind.ASSERTION, f"{NS}remark") in await rows("remark")
    kinds = await tables.read(store.generation, lambda c: {kind for (kind,) in c.execute("SELECT kind FROM usage")})
    assert kinds >= {UsageKind.RANGE, UsageKind.EQUIVALENT_CLASS}


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
    build = tables._build
    await tables.start(2, "")  # already building: nothing changes
    assert tables._build is build and build is not None and not build.task.done()
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
    module = browl.precache.usage_table
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("ATTACH ':memory:' AS derived")
        connection.execute("CREATE TABLE derived.entity (id INTEGER PRIMARY KEY, iri TEXT, display_name TEXT)")
        connection.executemany("INSERT INTO derived.entity (iri) VALUES (?)", [(f"{OWL}Thing",), (f"{OWL}Class",)])
        listed = module._not_in(module._skipped_types(connection))
        assert f"<{OWL}ObjectProperty>" in listed
        assert f"<{OWL}Class>" not in listed  # an entity: its class assertions are usages
        connection.execute("INSERT INTO derived.entity (iri) VALUES (?)", (f"{RDF}type",))
        assert module._not_in(module._skipped_types(connection)) == "NOT IN (<urn:x-browl:none>)"


async def test_count_is_scanned(plain: UsageStore, settings: Settings, tmp_path: Path) -> None:
    """Check that the build's count, for its estimate, is of the triples its scan reads, not of every triple."""
    (tmp_path / "derived").mkdir()
    source = browl.usage_tables.derived.derived_path(settings.data_dir, plain.generation)
    (tmp_path / "derived" / source.name).symlink_to(source)
    progress = Progress()
    async with on_thread(build_file(tmp_path, plain.generation, "k"), "test") as db:
        await fill_usage_table(plain.sparql, plain.dataset, plain.dataset_clause, db, progress)
    assert progress.scanned > 0
    assert progress.triples == progress.scanned
    assert progress.scanned_at is not None and progress.scanned_at >= progress.started


def test_sources_in_tab_order() -> None:
    """Check that the table's sources are the stream's, in the order the tab sorts them (`sorted_usages`)."""
    assert [UsageSource[source.name] for source in sorted(Source)] == sorted(UsageSource)


async def test_derived_rebuilt(tables: UsageTables, tmp_path: Path) -> None:
    """Check that a table built from one file of a generation's derived database isn't current once it's replaced."""
    derived_file(tmp_path, 1)
    with build_file(tmp_path, 1, tables.key):
        pass
    assert is_current(tmp_path, 1, tables.key)
    path = browl.usage_tables.derived.derived_path(tmp_path, 1)
    copy = path.with_name("copy")
    copy.write_bytes(path.read_bytes())
    copy.replace(path)  # rebuilt, as a build renames its file into place
    with pytest.raises(TableNotReadyError, match="another file of the derived database"):
        connect(tmp_path, 1, tables.key)
    assert not is_current(tmp_path, 1, tables.key)
    path.unlink()
    with pytest.raises(TableNotReadyError, match="derived database of generation 1 can't be read"):
        connect(tmp_path, 1, tables.key)
    derived_file(tmp_path, 1)
    stamp = derived_stamp(tmp_path, 1)
    GATES[1] = asyncio.Event()
    GATES[1].set()
    await tables.start(1, "")
    await tables.wait()
    with closing(connect(tmp_path, 1, tables.key)) as connection:
        assert connection.execute("SELECT derived FROM meta").fetchone() == (stamp,)


async def test_reads_by_generation(tables: UsageTables, tmp_path: Path) -> None:
    """Check that reads share the newest generation's connection on its own thread; an older one's opens its own."""
    for generation in (1, 2):
        derived_file(tmp_path, generation)
        with build_file(tmp_path, generation, tables.key):
            pass

    def generation_of(connection: sqlite3.Connection) -> tuple[int, str]:
        return connection.execute("SELECT generation FROM meta").fetchone()[0], threading.current_thread().name

    first, thread = await tables.read(1, generation_of)
    assert first == 1 and thread.startswith("usage-read")
    second, thread = await tables.read(2, generation_of)
    assert second == 2 and thread.startswith("usage-read")
    assert tables._reader is not None and tables._reader.generation == 2
    older, thread = await tables.read(1, generation_of)
    assert older == 1 and not thread.startswith("usage-read")
    await tables.close()
    after, thread = await tables.read(2, generation_of)  # after closing: a connection of its own
    assert after == 2 and not thread.startswith("usage-read") and tables._reader is None


async def test_reads_queued_before_newer(tables: UsageTables, tmp_path: Path) -> None:
    """Check that reads queued on a generation's connection end before a newer generation's read closes it."""
    for generation in (1, 2):
        derived_file(tmp_path, generation)
        with build_file(tmp_path, generation, tables.key):
            pass
    gate = threading.Event()

    def held(connection: sqlite3.Connection) -> int:
        gate.wait(5)
        return connection.execute("SELECT generation FROM meta").fetchone()[0]

    queued = [asyncio.create_task(tables.read(1, held)) for _ in range(20)]
    await asyncio.sleep(0.05)
    newer = asyncio.create_task(tables.read(2, held))
    await asyncio.sleep(0.05)
    gate.set()
    assert await asyncio.gather(*queued, newer) == [1] * 20 + [2]
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


async def test_older_start_ignored(tables: UsageTables, tmp_path: Path) -> None:
    """Check that starting an older generation's build changes nothing once a newer one's has started."""
    for generation in (1, 2):
        derived_file(tmp_path, generation)
        GATES[generation] = asyncio.Event()
    await tables.start(2, "")
    build = tables._build
    await tables.start(1, "")
    assert tables._build is build and build is not None and not build.task.done()
    assert not tables.building(1)
    GATES[2].set()
    await tables.wait()
    await tables.start(1, "")  # the newer one's built: still nothing
    assert tables._build is build and not tables.building(1)
    assert is_current(tmp_path, 2, tables.key)


async def test_stale_key_builds_again(tables: UsageTables, tmp_path: Path) -> None:
    """Check that a table built with another key, e.g. by older code, is built again at the next start."""
    derived_file(tmp_path, 1)
    with build_file(tmp_path, 1, "old key"):
        pass
    GATES[1] = asyncio.Event()
    GATES[1].set()
    await tables.start(1, "")
    assert tables.building(1)
    await tables.wait()
    assert is_current(tmp_path, 1, tables.key)


async def test_lock_held_throughout(tables: UsageTables, tmp_path: Path) -> None:
    """Check that a build holds the build lock until it ends."""
    derived_file(tmp_path, 1)
    GATES[1] = asyncio.Event()
    await tables.start(1, "")
    await asyncio.sleep(0.05)
    other = browl.usage_tables.JobLock(tmp_path / "usages" / "build.lock")
    assert not other.acquire()
    GATES[1].set()
    await tables.wait()
    assert other.acquire()
    other.release()


async def test_start_after_close(tables: UsageTables, tmp_path: Path) -> None:
    """Check that once closed, as at shutdown, a start builds nothing."""
    derived_file(tmp_path, 1)
    GATES[1] = asyncio.Event()
    await tables.close()
    await tables.start(1, "")
    assert tables._build is None
    assert not (tmp_path / "usages").exists()


async def test_overlapping_starts(tables: UsageTables, tmp_path: Path) -> None:
    """Check that two starts at once leave one build running, the newer generation's, and none orphaned."""
    for generation in (1, 2):
        derived_file(tmp_path, generation)
        GATES[generation] = asyncio.Event()
    await asyncio.gather(tables.start(1, ""), tables.start(2, ""))
    await asyncio.sleep(0.05)
    running = [task.get_name() for task in asyncio.all_tasks() if task.get_name().startswith("usage table")]
    assert running == ["usage table 2"]
    assert tables.building(2)
    await tables.close()


async def test_cancel_while_taking_lock(tables: UsageTables, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a build cancelled while it takes the build lock doesn't keep it."""
    derived_file(tmp_path, 1)
    GATES[1] = asyncio.Event()

    locks: list[JobLock] = []  # kept, so only a release, not garbage collection, frees the lock

    class SlowLock(JobLock):
        def acquire(self) -> bool:
            locks.append(self)
            time.sleep(0.2)
            return super().acquire()

    monkeypatch.setattr(browl.usage_tables, "JobLock", SlowLock)
    await tables.start(1, "")
    await asyncio.sleep(0.05)
    await tables.cancel()
    await asyncio.sleep(0.3)
    other = JobLock(tmp_path / "usages" / "build.lock")
    assert other.acquire()
    other.release()


async def test_cancel_cancellable(tables: UsageTables, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a `cancel` that is itself cancelled, while the build ends, is cancelled, not swallowed."""
    derived_file(tmp_path, 1)

    async def slow_end(*args: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.3)
            raise

    monkeypatch.setattr(browl.usage_tables, "fill_usage_table", slow_end)
    await tables.start(1, "")
    await asyncio.sleep(0.05)
    caller = asyncio.create_task(tables.cancel())
    await asyncio.sleep(0.05)
    caller.cancel()
    await asyncio.wait({caller})
    assert caller.cancelled()
    await asyncio.sleep(0.5)


async def test_cancel_interrupts_build(tables: UsageTables, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that cancelling a build interrupts its SQL statement, so the cancel returns at once."""
    derived_file(tmp_path, 1)

    async def long_sql(sparql: object, dataset: str, clause: str, db: DerivedWriter, progress: Progress) -> None:
        await db.run(lambda connection: connection.execute(LONG).fetchone())

    monkeypatch.setattr(browl.usage_tables, "fill_usage_table", long_sql)
    await tables.start(1, "")
    await asyncio.sleep(0.2)
    async with asyncio.timeout(2):
        await tables.cancel()
    assert list((tmp_path / "usages").iterdir()) == [tmp_path / "usages" / "build.lock"]


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


async def test_count_table_failure(
    store: UsageStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Check that a count the usage table fails to give is logged, and counted by Fuseki instead."""
    _, ordered = await streamed(store, f"{NS}Animal")

    async def fail(*args: object) -> None:
        raise sqlite3.DatabaseError("disk gone")

    monkeypatch.setattr(browl.usages, "read_table_count", fail)
    assert await count_usages(store, f"{NS}Animal") == len(ordered)
    assert "Couldn't read the count of http://example.org/axioms#Animal from the usage table" in caplog.text


async def test_count_reads_no_rows(store: UsageStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a count from the table reads only its count, not the usages' rows, and is the page's."""
    page = await read_table_page(store, f"{NS}Animal", 0, 0)
    assert page is not None

    def no_rows(*args: object) -> None:
        raise AssertionError("read the usages' rows")

    async def no_fuseki(*args: object) -> None:
        raise AssertionError("counted by Fuseki")

    monkeypatch.setattr(browl.usages, "_table_rows", no_rows)
    monkeypatch.setattr(store.sparql, "select", no_fuseki)
    assert await count_usages(store, f"{NS}Animal") == page.count > 0
    assert await browl.usages.read_table_count(store, "http://example.org/nothing") is None
