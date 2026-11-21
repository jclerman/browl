"""Tests for the Overview tab's panels when their data can't be read: the page still works, the log has why."""

import asyncio
import dataclasses
import logging
import re
import threading
import urllib.parse
from collections.abc import Iterator

import httpx2
import jpype
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

import browl.axioms
import browl.lookup
import browl.overview
from browl import owl
from browl.annotations import AnnotationGroup, IRIValue
from browl.axioms import fetch_fragment
from browl.derived import EntityType
from browl.frames import frame_groups
from browl.fuseki import UNION_FROM, FusekiClient, SparqlError
from browl.jobs import JobState, Stage
from browl.jobtypes import ImportResult
from browl.links import EntityLink
from browl.loading import dataset_name
from browl.rdf import IRI as RDFIRI
from browl.rdf import OWL, RDF, RDFSyntaxError
from tests.helpers import derived_stage, make_app, no_axioms

pytestmark = pytest.mark.usefixtures("data_dir")

IRI = "http://example.org/onto#A"
MESSAGE = "The axioms can't be shown just now."
ANNOTATIONS_MESSAGE = "The annotations can't be shown just now."
PANEL = {"path": "/onto", "fragment": "A", "panel": "1"}


def fuseki_error(request: httpx2.Request) -> httpx2.Response:
    """Answer every query as Fuseki does one that fails."""
    return httpx2.Response(500, text="Error 500: Server Error")


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Yield a test client for an app whose Fuseki fails every query, with the entity loaded."""
    app = make_app(httpx2.MockTransport(fuseki_error))
    with TestClient(app) as test_client:
        app.state.jobs.register(Stage.PRE_CACHING, derived_stage([(IRI, EntityType.CLASS)]))
        test_client.portal.call(app.state.jobs.start)
        test_client.portal.call(app.state.jobs.wait)
        yield test_client


@pytest.fixture
def no_annotations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the annotations panel's fetch find none, without a query, so tests of the axioms panel see only its own."""

    async def fetched(*args: object) -> list[object]:
        return []

    monkeypatch.setattr(browl.overview, "fetch_annotations", fetched)


def test_query_fails(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """Check that failed queries fail only the panels: the page shows a message for each, and the log the errors."""
    with caplog.at_level(logging.ERROR, logger="browl.overview"):
        page = client.get("/_browl/hash", params={"path": "/onto", "fragment": "A"})
    assert page.status_code == 200
    assert MESSAGE in page.text
    assert ANNOTATIONS_MESSAGE in page.text
    assert "Error 500" not in page.text
    assert sorted(record.getMessage() for record in caplog.records) == [
        f"Couldn't show the annotations of {IRI}",
        f"Couldn't show the axioms of {IRI}",
    ]
    assert all(record.exc_info is not None for record in caplog.records)
    assert "Error 500: Server Error" in caplog.text


@pytest.mark.parametrize(
    "error", [SparqlError("the annotations failed"), RDFSyntaxError("not a valid IRI")], ids=["query", "iri"]
)
def test_annotations_fail_alone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    """Check that a failed annotations query, or an invalid IRI, leaves the axioms panel alone."""

    async def failed(*args: object) -> list[object]:
        raise error

    monkeypatch.setattr(browl.overview, "_read_axioms", no_axioms)
    monkeypatch.setattr(browl.overview, "fetch_annotations", failed)
    with caplog.at_level(logging.ERROR, logger="browl.overview"):
        page = client.get("/_browl/hash", params=PANEL)
    assert page.status_code == 200
    assert ANNOTATIONS_MESSAGE in page.text
    assert MESSAGE not in page.text
    [record] = caplog.records
    assert record.getMessage() == f"Couldn't show the annotations of {IRI}"


def test_axioms_fail_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the annotations, and their links, still show when the axioms are too many (DESIGN.md §2.8)."""
    prop = RDFIRI("http://example.org/onto#note")

    async def too_many(*args: object) -> tuple[None, bool]:
        return None, True

    async def fetched(*args: object) -> list[object]:
        return [(prop, IRIValue(iri=RDFIRI(IRI)))]

    monkeypatch.setattr(browl.overview, "_read_axioms", too_many)
    monkeypatch.setattr(browl.overview, "fetch_annotations", fetched)
    app = make_app()
    with TestClient(app) as test_client:
        entities = [(IRI, EntityType.CLASS), (prop, EntityType.ANNOTATION_PROPERTY)]
        app.state.jobs.register(Stage.PRE_CACHING, derived_stage(entities))
        test_client.portal.call(app.state.jobs.start)
        test_client.portal.call(app.state.jobs.wait)
        page = test_client.get("/_browl/hash", params=PANEL).text
    assert "This entity has too many axioms to show here." in page
    assert '<dt><a href="/onto#note">' in page
    assert '<dd><a href="/onto#A">' in page


@pytest.mark.usefixtures("no_annotations")
def test_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check the axioms panel's frame: sections and their lines, the class's implied equivalents linked and marked."""
    a, b, c, d = (owl.Entity(kind=owl.EntityKind.CLASS, iri=f"{IRI}{name}") for name in ("", "B", "C", "D"))

    async def axioms(*args: object) -> tuple[list[owl.AnyAxiom], bool]:
        return [owl.SubClassOf(sub=a, super=b), owl.EquivalentClasses(members=(a, c))], False

    monkeypatch.setattr(browl.overview, "_read_axioms", axioms)
    app = make_app()
    with TestClient(app) as test_client:
        entities = [(each.iri, EntityType.CLASS) for each in (a, b, c, d)]
        stage = derived_stage(
            entities, names={c.iri: ("cee", None), d.iri: ("dee", None)}, equivalence_groups=[[a.iri, c.iri, d.iri]]
        )
        app.state.jobs.register(Stage.PRE_CACHING, stage)
        test_client.portal.call(app.state.jobs.start)
        test_client.portal.call(app.state.jobs.wait)
        page = test_client.get("/_browl/hash", params=PANEL).text
    assert re.search(r'<dt>SubClass Of</dt>\s*<dd dir="ltr"><a href="/onto#AB">', page)
    implied = '<dd dir="ltr"><a href="/onto#AD"><bdi>dee</bdi></a> <small class="implied">(implied)</small></dd>'
    assert re.search(rf'<dt>Equivalent To</dt>\s*<dd dir="ltr"><a href="/onto#AC">.*</dd>\s*{re.escape(implied)}', page)
    assert page.count("(implied)") == 1  # not C, which an axiom states
    assert "more equivalents" not in page


@pytest.mark.usefixtures("no_annotations")
@pytest.mark.parametrize(
    ("viewed", "cap", "implied", "more"), [("C", 5, 2, False), ("C", 1, 1, True), ("B", 5, 0, False)]
)
def test_implied_equivalents(monkeypatch: pytest.MonkeyPatch, viewed: str, cap: int, implied: int, more: bool) -> None:
    """Check a class's implied equivalents: on a member that isn't the representative, cut at the cap, or none."""
    monkeypatch.setattr(browl.overview, "_read_axioms", no_axioms)
    monkeypatch.setattr(browl.overview, "MAX_EQUIVALENTS", cap)
    a, b, c, d = (f"{IRI}{name}" for name in ("", "B", "C", "D"))
    app = make_app()
    with TestClient(app) as test_client:
        stage = derived_stage([(each, EntityType.CLASS) for each in (a, b, c, d)], equivalence_groups=[[a, c, d]])
        app.state.jobs.register(Stage.PRE_CACHING, stage)
        test_client.portal.call(app.state.jobs.start)
        test_client.portal.call(app.state.jobs.wait)
        page = test_client.get("/_browl/hash", params={**PANEL, "fragment": f"A{viewed}"}).text
    assert page.count("(implied)") == implied
    assert ("This class has more equivalents than can be shown here." in page) == more
    assert f'href="/onto#A{viewed}"' not in page  # not itself


def test_usage_tab_untouched(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """Check that the Usage tab doesn't fetch the axioms."""
    page = client.get("/_browl/hash", params={"path": "/onto", "fragment": "A", "tab": "usage"})
    assert page.status_code == 200
    assert MESSAGE not in page.text
    assert not [record for record in caplog.records if record.name == "browl.overview"]


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
@pytest.mark.usefixtures("no_annotations")
def test_fragment_error(client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Check that a fragment the OWLAPI can't parse fails only the axioms panel, and its parser's error is logged."""

    async def fetched(*args: object) -> bytes:
        return b"<http://example.org/onto#A> <http://exa\n"

    monkeypatch.setattr(browl.overview, "fetch_fragment", fetched)
    with caplog.at_level(logging.ERROR, logger="browl.overview"):
        page = client.get("/_browl/hash", params={"path": "/onto", "fragment": "A", "panel": "1"})
    assert page.status_code == 200
    assert MESSAGE in page.text
    assert "parse" not in page.text
    [record] = caplog.records
    assert record.exc_info is not None
    assert "FragmentError: the OWLAPI couldn't parse the fragment" in caplog.text
    assert "Detailed logs:" in caplog.text  # the Java exception's, with its parsers' errors
    assert "Stack trace:" in caplog.text


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
@pytest.mark.usefixtures("no_annotations")
def test_java_error(client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Check that any other Java exception, here from the conversion, fails only the axioms panel, and is logged."""

    async def fetched(*args: object) -> bytes:
        return f"<{IRI}> <{RDF}type> <{OWL}Class> .\n".encode()

    def fail(axioms: object) -> None:
        raise jpype.JClass("java.lang.IllegalStateException")("the conversion broke")

    monkeypatch.setattr(browl.overview, "fetch_fragment", fetched)
    monkeypatch.setattr(browl.axioms, "convert_axioms", fail)
    with caplog.at_level(logging.ERROR, logger="browl.overview"):
        page = client.get("/_browl/hash", params=PANEL)
    assert page.status_code == 200
    assert MESSAGE in page.text
    assert "broke" not in page.text
    [record] = caplog.records
    assert record.exc_info is not None
    assert "FragmentError: the OWLAPI failed on the fragment" in caplog.text
    assert "IllegalStateException: the conversion broke" in caplog.text


@pytest.mark.usefixtures("no_annotations")
def test_rdf_syntax_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Check that an IRI the fetch finds invalid fails only the axioms panel, and is logged."""
    error = RDFSyntaxError("not a valid IRI")

    async def fetched(*args: object) -> bytes:
        raise error

    monkeypatch.setattr(browl.overview, "fetch_fragment", fetched)
    with caplog.at_level(logging.ERROR, logger="browl.overview"):
        page = client.get("/_browl/hash", params=PANEL)
    assert page.status_code == 200
    assert MESSAGE in page.text
    assert str(error) not in page.text
    [record] = caplog.records
    assert record.exc_info is not None and record.exc_info[1] is error


@pytest.mark.usefixtures("no_annotations")
def test_escaped_illegal_iri(caplog: pytest.LogCaptureFixture) -> None:
    """Check that a stored IRI with an escaped illegal character, which Jena writes escaped, fails only the panel."""

    def fuseki(request: httpx2.Request) -> httpx2.Response:
        line = f"<{IRI}> <http://example.org/p> <http://example.org/a\\u0020b> .\n"
        return httpx2.Response(200, text=line, headers={"Content-Type": "application/n-triples"})

    app = make_app(httpx2.MockTransport(fuseki))
    with TestClient(app) as test_client:
        app.state.jobs.register(Stage.PRE_CACHING, derived_stage([(IRI, EntityType.CLASS)]))
        test_client.portal.call(app.state.jobs.start)
        test_client.portal.call(app.state.jobs.wait)
        with caplog.at_level(logging.ERROR, logger="browl.overview"):
            page = test_client.get("/_browl/hash", params=PANEL)
    assert page.status_code == 200
    assert MESSAGE in page.text
    assert len(caplog.records) == 1
    assert "not a valid IRI: 'http://example.org/a b'" in caplog.text


def ready_state(client: TestClient) -> JobState:
    """Return the client's app's job state, with its generation ready."""
    state: JobState = client.portal.call(client.app.state.jobs.current)
    assert state.ready_generation is not None and state.generation == state.ready_generation
    return state


def record_fetches(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Make the panel's fetch fail, recording the dataset and dataset clause of each call."""
    fetches = []

    async def fetched(sparql: object, dataset: str, iri: str, clause: str = "") -> bytes:
        fetches.append((dataset, clause))
        raise SparqlError("recorded")

    monkeypatch.setattr(browl.overview, "fetch_fragment", fetched)
    return fetches


@pytest.mark.usefixtures("no_annotations")
def test_one_generation(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the panel reads the generation the page found its entity in, though a newer one is ready since."""
    state = ready_state(client)
    assert state.generation is not None and state.ready_generation is not None
    newer = dataclasses.replace(state, generation=state.generation + 1, ready_generation=state.ready_generation + 1)
    found = False
    find = browl.lookup._find
    current = client.app.state.jobs.current

    def found_then_newer(*args: object) -> object:
        nonlocal found
        found = True
        return find(*args)

    async def state_now() -> JobState:
        return newer if found else await current()

    monkeypatch.setattr(browl.lookup, "_find", found_then_newer)
    monkeypatch.setattr(client.app.state.jobs, "current", state_now)
    fetches = record_fetches(monkeypatch)
    page = client.get("/_browl/hash", params=PANEL)
    assert page.status_code == 200
    assert [dataset for dataset, _ in fetches] == [dataset_name(state.ready_generation)]


@pytest.mark.parametrize(
    ("imports", "newer_job", "clause"),
    [
        ((), False, ""),
        ((ImportResult("http://example.org/dropped", reason="not allowed"),), False, ""),
        ((ImportResult("http://example.org/loaded", triples=3),), False, UNION_FROM),
        ((), True, UNION_FROM),  # a newer job's imports aren't the ready generation's
    ],
    ids=["no-imports", "dropped-import", "loaded-import", "newer-job"],
)
@pytest.mark.usefixtures("no_annotations")
def test_dataset_clause(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    imports: tuple[ImportResult, ...],
    newer_job: bool,
    clause: str,
) -> None:
    """Check that the panel's queries see the imports when the ready generation has any, or may have."""
    state = ready_state(client)
    assert state.generation is not None
    changed = dataclasses.replace(state, imports=imports, generation=state.generation + newer_job)

    async def state_now() -> JobState:
        return changed

    monkeypatch.setattr(client.app.state.jobs, "current", state_now)
    fetches = record_fetches(monkeypatch)
    assert client.get("/_browl/hash", params=PANEL).status_code == 200
    assert [each for _, each in fetches] == [clause]


@pytest.mark.anyio
async def test_failed_query_cancels_other() -> None:
    """Check that when one of the fragment's two first queries fails, the other is cancelled, and the error raised."""
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def fuseki(request: httpx2.Request) -> httpx2.Response:
        query = urllib.parse.parse_qs((await request.aread()).decode())["query"][0]
        if "VALUES ?predicate" in query:  # entity-symmetric.rq: hangs
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        await started.wait()
        return httpx2.Response(500, text="Error 500: Server Error")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(fuseki), base_url="http://fuseki") as http:
        with pytest.raises(SparqlError):
            await fetch_fragment(FusekiClient(http), "ds", IRI)
        assert cancelled.is_set()


@pytest.mark.usefixtures("no_annotations")
def test_too_many(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a fragment that mentions too many IRIs isn't fetched further, and the panel says so."""
    queries = []

    def fuseki(request: httpx2.Request) -> httpx2.Response:
        queries.append(request)
        line = f"<{IRI}> <http://example.org/p> <http://example.org/o> .\n"
        return httpx2.Response(200, text=line, headers={"Content-Type": "application/n-triples"})

    monkeypatch.setattr(browl.axioms, "MAX_FRAGMENT_IRIS", 2)
    app = make_app(httpx2.MockTransport(fuseki))
    with TestClient(app) as test_client:
        app.state.jobs.register(Stage.PRE_CACHING, derived_stage([(IRI, EntityType.CLASS)]))
        test_client.portal.call(app.state.jobs.start)
        test_client.portal.call(app.state.jobs.wait)
        with caplog.at_level(logging.WARNING, logger="browl.overview"):
            page = test_client.get("/_browl/hash", params=PANEL)
    assert page.status_code == 200
    assert "This entity has too many axioms to show here." in page.text
    assert MESSAGE not in page.text
    assert len(queries) == 2  # not the types query
    [record] = caplog.records
    assert record.getMessage() == f"Not showing the axioms of {IRI}: the fragment mentions 3 IRIs, more than 2"


def test_panel_json() -> None:
    """Check that a panel's axioms serialize with their fields, which typing them as the base `Axiom` would drop."""
    a, b = (owl.Entity(kind=owl.EntityKind.CLASS, iri=f"{IRI}{name}") for name in "12")
    groups = frame_groups([owl.SubClassOf(sub=a, super=b)], a.iri, [f"{IRI}3"], str)
    panel = browl.overview.AxiomsPanel(groups=groups)
    adapter = TypeAdapter(browl.overview.AxiomsPanel)
    assert adapter.validate_json(adapter.dump_json(panel)) == panel


def test_overview_json() -> None:
    """Check that the Overview tab's data round-trips through JSON: both panels, and its links."""
    group = AnnotationGroup(property=RDFIRI(f"{IRI}p"), values=(IRIValue(iri=RDFIRI(IRI)),))
    overview = browl.overview.Overview(
        annotations=browl.overview.AnnotationsPanel(groups=[group]),
        axioms=browl.overview.AxiomsPanel(groups=None, too_many=True),
        links={IRI: EntityLink(IRI, "A", "/onto#A"), f"{IRI}p": EntityLink(f"{IRI}p", f"{IRI}p", None)},
    )
    assert browl.overview.Overview.model_validate_json(overview.model_dump_json()) == overview


def test_panels_checked() -> None:
    """Check that the panels' models validate their data and are frozen."""
    with pytest.raises(ValidationError):
        browl.overview.AnnotationsPanel(groups=["not a group"])
    panel = browl.overview.AxiomsPanel(groups=None)
    with pytest.raises(ValidationError):
        panel.too_many = True  # type: ignore[misc]


def test_panels_read_concurrently(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the two panels' data are read at the same time: each read waits for the other to start."""
    annotations_started, axioms_started = asyncio.Event(), asyncio.Event()

    async def read_annotations(*args: object) -> tuple[list[object], bool]:
        annotations_started.set()
        async with asyncio.timeout(5):
            await axioms_started.wait()
        return [], False

    async def read_axioms(*args: object) -> tuple[list[object], bool]:
        axioms_started.set()
        async with asyncio.timeout(5):
            await annotations_started.wait()
        return [], False

    monkeypatch.setattr(browl.overview, "_read_annotations", read_annotations)
    monkeypatch.setattr(browl.overview, "_read_axioms", read_axioms)
    page = client.get("/_browl/hash", params=PANEL)
    assert page.status_code == 200
    assert page.text.count('<p class="note">None.</p>') == 2


def test_unexpected_error_cancels_other(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that an unexpected error reading one panel cancels the other's read, and is raised itself."""
    started = asyncio.Event()
    cancelled = threading.Event()

    async def read_annotations(*args: object) -> tuple[list[object], bool]:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return [], False

    async def read_axioms(*args: object) -> tuple[list[object], bool]:
        await started.wait()
        raise RuntimeError("unexpected")

    monkeypatch.setattr(browl.overview, "_read_annotations", read_annotations)
    monkeypatch.setattr(browl.overview, "_read_axioms", read_axioms)
    with pytest.raises(RuntimeError, match="unexpected"):
        client.get("/_browl/hash", params=PANEL)
    assert cancelled.is_set()
