"""Tests for the ontology's home page (U-1, `browl.home`)."""

import contextlib
import dataclasses
import logging
import re
import sqlite3
from collections.abc import Callable, Iterator

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import browl.home
import browl.overview
from browl.annotations import IRIValue, LiteralValue
from browl.derived import EntityType, derived_path, read_type_counts
from browl.fuseki import SparqlError
from browl.jobs import JobContext, JobState, Stage, StageFunction
from browl.jobtypes import ImportResult, Reasoner
from browl.pages import MAX_IRI_CHARS, MAX_LITERAL_CHARS
from browl.rdf import IRI, OWL, RDF, RDFS
from tests.helpers import connect, derived_stage, fuseki_dataset, make_app

pytestmark = pytest.mark.usefixtures("data_dir")

ONTOLOGY = "http://example.org/onto"
EX = "http://example.org/"
ENTITIES = [
    (f"{ONTOLOGY}#A", EntityType.CLASS),
    (f"{ONTOLOGY}#B", EntityType.CLASS),
    (f"{ONTOLOGY}#p", EntityType.CLASS),
    (f"{ONTOLOGY}#p", EntityType.OBJECT_PROPERTY),
    (f"{ONTOLOGY}#note", EntityType.ANNOTATION_PROPERTY),
]
"""Three classes, one punned as an object property, and an annotation property: no data properties."""

ANNOTATIONS = [
    (IRI(f"{RDFS}label"), LiteralValue(lexical="The Onto", datatype=IRI(f"{RDF}langString"), language="en")),
    (IRI(f"{RDFS}comment"), LiteralValue(lexical="An <example>.", datatype=IRI(f"{RDF}langString"), language="en")),
    (IRI(f"{ONTOLOGY}#note"), IRIValue(iri=IRI(f"{ONTOLOGY}#A"))),
]

IMPORTS = (
    ImportResult(f"{EX}loaded", triples=1_200),
    ImportResult(f"{EX}nested", imported_by=f"{EX}loaded", triples=34),
    ImportResult("file:///etc/passwd", reason="only http and https imports are fetched"),
    ImportResult(f"{EX}gone", imported_by=f"{EX}loaded", reason="HTTP 404 from http://example.org/gone"),
)
"""Two imports loaded, one direct and one indirect; two dropped, one direct and one indirect."""


def ontology_stage(iri: str = ONTOLOGY, title: str | None = "The <Onto>") -> StageFunction:
    """Return a pre-caching stage whose derived database holds the ontology and `ENTITIES`."""
    return derived_stage(ENTITIES, iri, title)


def ready(app: FastAPI, client: TestClient, stage: StageFunction | None = None) -> None:
    """Run a load job with this pre-caching stage, by default `ontology_stage()`."""
    app.state.jobs.register(Stage.PRE_CACHING, stage or ontology_stage())
    client.portal.call(app.state.jobs.start)
    client.portal.call(app.state.jobs.wait)


def _then(stage: StageFunction, change: Callable[[sqlite3.Connection], object]) -> StageFunction:
    """Return the stage with a change to its derived database after it runs."""

    async def changed(context: JobContext) -> None:
        await stage(context)
        path = derived_path(context.settings.data_dir, context.generation)
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            change(connection)

    return changed


def _with_rows(stage: StageFunction, table: str, rows: dict[str, object]) -> StageFunction:
    """Return the stage with these (key, value) rows in a two-column table after it runs, as a hook would write them."""
    return _then(stage, lambda connection: connection.executemany(f"INSERT INTO {table} VALUES (?, ?)", rows.items()))


def _with_meta(stage: StageFunction, **changes: object) -> StageFunction:
    """Return the stage with the `meta` row changed after it runs."""
    assignments = ", ".join(f"{name} = ?" for name in changes)
    return _then(
        stage, lambda connection: connection.execute(f"UPDATE meta SET {assignments}", tuple(changes.values()))
    )


@pytest.fixture
def annotations(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make the annotations' fetch return `ANNOTATIONS`, without a query, recording the IRI asked for."""
    asked: list[str] = []

    async def fetched(sparql: object, dataset: str, iri: str, clause: str = "") -> list[object]:
        asked.append(iri)
        return list(ANNOTATIONS)

    monkeypatch.setattr(browl.overview, "fetch_annotations", fetched)
    return asked


@pytest.fixture
def app() -> FastAPI:
    """Return an app with default settings."""
    return make_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Yield a test client for the app, with the ontology loaded."""
    with TestClient(app) as test_client:
        ready(app, test_client)
        yield test_client


def page(app: FastAPI, stage: StageFunction) -> str:
    """Return the home page after a load job with this pre-caching stage."""
    with TestClient(app) as client:
        ready(app, client, stage)
        return client.get("/onto").text


def with_state(client: TestClient, monkeypatch: pytest.MonkeyPatch, **changes: object) -> None:
    """Make the app's job state the current one with these changes, e.g. the imports the loading stage would record."""
    state: JobState = client.portal.call(client.app.state.jobs.current)
    changed = dataclasses.replace(state, **changes)

    async def current() -> JobState:
        return changed

    monkeypatch.setattr(client.app.state.jobs, "current", current)


def test_metadata(annotations: list[str], client: TestClient) -> None:
    """Check that the home page shows the ontology's IRI, reasoner, load time and entity counts by type."""
    text = client.get("/onto").text
    assert annotations == [ONTOLOGY]
    assert "<h1><bdi>The &lt;Onto&gt;</bdi></h1>" in text
    assert f'<a href="{ONTOLOGY}" rel="nofollow noreferrer"><code dir="ltr">{ONTOLOGY}</code></a>' in text
    assert "<dt>Reasoner</dt>\n    <dd>None</dd>" in text
    assert '<time datetime="2026-11-17T00:00:00+00:00">2026-11-17 00:00 UTC</time>' in text
    assert '<a href="/_browl/tree/classes">Classes</a>: 3</dd>' in text
    assert '<a href="/_browl/tree/object-properties">Object properties</a>: 1</dd>' in text
    assert '<a href="/_browl/tree/data-properties">Data properties</a>: 0</dd>' in text
    assert '<a href="/_browl/tree/annotation-properties">Annotation properties</a>: 1</dd>' in text
    assert "<dt>Version" not in text


@pytest.mark.parametrize(("reasoner", "label"), [(Reasoner.ELK, "ELK"), (Reasoner.HERMIT, "HermiT")])
def test_reasoner(annotations: list[str], app: FastAPI, reasoner: Reasoner, label: str) -> None:
    """Check that the reasoner applied is named."""
    text = page(app, _with_meta(ontology_stage(), reasoner=str(reasoner)))
    assert f"<dt>Reasoner</dt>\n    <dd>{label}</dd>" in text


def test_annotations(annotations: list[str], client: TestClient) -> None:
    """Check that the ontology's annotations are in the annotations panel, name properties first, IRIs linked."""
    text = client.get("/onto").text
    assert '<h2 id="annotations-heading">Annotations</h2>' in text
    # The name properties first, then the others by the name shown: here the IRI, as it has no label.
    label, note, comment = (text.index(each) for each in ("The Onto", f"{ONTOLOGY}#note", "An &lt;example&gt;."))
    assert label < note < comment
    assert f'<dd><a href="/onto#A"><bdi>{ONTOLOGY}#A</bdi></a></dd>' in text


def test_annotations_fail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Check that a failed annotations query fails only the annotations panel, which says so, and is logged."""

    async def failed(*args: object) -> list[object]:
        raise SparqlError("Fuseki is down")

    monkeypatch.setattr(browl.overview, "fetch_annotations", failed)
    with caplog.at_level(logging.ERROR, logger="browl.overview"):
        response = client.get("/onto")
    assert response.status_code == 200
    assert "The annotations can't be shown just now." in response.text
    assert "Classes</a>: 3" in response.text
    assert [record.getMessage() for record in caplog.records] == [f"Couldn't show the annotations of {ONTOLOGY}"]


@pytest.mark.parametrize(
    ("version_iri", "expected"),
    [
        (f"{EX}onto/1.0", f'<a href="{EX}onto/1.0" rel="nofollow noreferrer"><code dir="ltr">{EX}onto/1.0</code></a>'),
        ("javascript:alert(1)", '<dd><code dir="ltr">javascript:alert(1)</code></dd>'),
        ("urn:example:v1", '<dd><code dir="ltr">urn:example:v1</code></dd>'),
    ],
)
def test_version_iri(annotations: list[str], app: FastAPI, version_iri: str, expected: str) -> None:
    """Check that the version IRI is shown, and linked only if it's `http` or `https`, rather than the version info."""
    text = page(app, _with_meta(ontology_stage(), version_iri=version_iri, version_info="1.0"))
    assert "<dt>Version IRI</dt>" in text
    assert expected in text
    assert 'href="javascript' not in text
    assert "<dt>Version</dt>" not in text


def test_version_info(annotations: list[str], app: FastAPI) -> None:
    """Check that without a version IRI, the version shown is the version info, as text."""
    text = page(app, _with_meta(ontology_stage(title="Onto"), version_info="2026-11 <beta>"))
    assert '<dt>Version</dt>\n    <dd><bdi class="value">2026-11 &lt;beta&gt;</bdi></dd>' in text


def test_non_web_ontology_iri(annotations: list[str], app: FastAPI) -> None:
    """Check that an ontology IRI that isn't `http` or `https` is text, not a link."""
    iri = "urn:example:onto"
    with TestClient(app) as client:
        ready(app, client, ontology_stage(iri, "Onto"))
        text = client.get("/_browl/iri", params={"iri": iri}).text
    assert f'<dt>IRI</dt>\n    <dd><code dir="ltr">{iri}</code></dd>' in text


def test_no_imports(annotations: list[str], client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that an ontology without imports says so, with its triple count, and no warning."""
    with_state(client, monkeypatch, triples=12_345, imports=())
    text = client.get("/onto").text
    assert "<dt>Triples</dt>\n    <dd>12,345 in the ontology</dd>" in text
    assert "in its imports" not in text
    imports = text[text.index('<h2 id="imports-heading">') :]
    assert imports.split("</section>")[0].split() == [
        "<h2",
        'id="imports-heading">Imports</h2>',
        "<p",
        'class="note">None.</p>',
    ]
    assert "import-warning" not in text


def test_imports(annotations: list[str], client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the dropped imports are in a red warning, with why and what imports them, and the triple counts."""
    with_state(client, monkeypatch, triples=500, imports=IMPORTS)
    text = client.get("/onto").text
    warning = text[text.index('<section class="import-warning"') : text.index('<div class="home-panels">')]
    assert "2 of the ontology's 4 imports couldn't be\n    loaded" in warning
    assert "what they declare" in warning
    assert (
        '<code dir="ltr">file:///etc/passwd</code>:\n      <bdi>only http and https imports are fetched</bdi>'
        in warning
    )
    gone = f'<a href="{EX}gone" rel="nofollow noreferrer"><code dir="ltr">{EX}gone</code></a>'
    by = f'<a href="{EX}loaded" rel="nofollow noreferrer"><code dir="ltr">{EX}loaded</code></a>'
    assert f"{gone} (imported by {by}):\n      <bdi>HTTP 404 from http://example.org/gone</bdi>" in warning
    assert f"{EX}nested" not in warning
    assert "<dd>500 in the ontology</dd>" in text
    assert "<dd>1,234 in its imports</dd>" in text
    imports = text[text.index('<h2 id="imports-heading">') :]
    assert f"{gone} (imported by {by}):\n      not loaded" in imports
    assert "1,200 triples" in imports
    assert "34 triples" in imports
    assert imports.count('<li class="failure">') == 2


def test_import_reason_first_line(annotations: list[str], client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the warning shows only a reason's first line, cut short: the rest, a parser's output, isn't public."""
    first = f"parse error: {'x' * 2_000}"
    with_state(client, monkeypatch, imports=(ImportResult(f"{EX}bad", reason=f"{first}\n/data/jobs/7/x.ttl line 3"),))
    text = client.get("/onto").text
    assert f"<bdi>{first[: MAX_LITERAL_CHARS - 1]}…</bdi>" in text
    assert "/data/jobs" not in text


def test_import_triples_unknown(annotations: list[str], client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that an import loaded with no triple count says it's loaded, and no imports' total is shown."""
    with_state(client, monkeypatch, triples=500, imports=(ImportResult(f"{EX}x"),))
    text = client.get("/onto").text
    assert f'<code dir="ltr">{EX}x</code></a>:\n      loaded</li>' in text
    assert "in its imports" not in text


def test_one_dropped_import(annotations: list[str], client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the warning's wording is singular for one dropped import, and no import triples are counted."""
    with_state(client, monkeypatch, triples=500, imports=IMPORTS[2:3])
    text = client.get("/onto").text
    assert "1 of the ontology's 1 imports" in text
    assert "what it declares" in text
    assert "in its imports" not in text


def test_newer_job(annotations: list[str], client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a newer job's triples and imports aren't shown as the ready generation's."""
    state: JobState = client.portal.call(client.app.state.jobs.current)
    assert state.generation is not None
    with_state(client, monkeypatch, generation=state.generation + 1, triples=500, imports=IMPORTS)
    text = client.get("/onto").text
    assert "Triples" not in text
    assert "imports-heading" not in text
    assert "import-warning" not in text


def test_type_count(app: FastAPI, client: TestClient) -> None:
    """Check that `type_count` counts each type's entities, a punned IRI once per type, the ontology too."""
    generation = client.portal.call(app.state.jobs.current).ready_generation
    with connect(app.state.settings, generation) as connection:
        assert read_type_counts(connection) == {
            EntityType.ONTOLOGY: 1,
            EntityType.CLASS: 3,
            EntityType.OBJECT_PROPERTY: 1,
            EntityType.ANNOTATION_PROPERTY: 1,
        }


def test_fuseki_unreachable() -> None:
    """Check that the home page still serves when Fuseki can't be reached."""

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    unreachable = make_app(httpx2.MockTransport(refuse))
    with TestClient(unreachable) as client:
        ready(unreachable, client)
        response = client.get("/onto")
    assert response.status_code == 200
    assert "The annotations can't be shown just now." in response.text


ONTOLOGY_TRIPLES = f"""\
<{ONTOLOGY}> <{RDF}type> <{OWL}Ontology> .
<{ONTOLOGY}> <{OWL}imports> <{EX}loaded> .
<{ONTOLOGY}> <{OWL}versionIRI> <{EX}onto/1.0> .
<{ONTOLOGY}> <{OWL}versionInfo> "1.0" .
<{ONTOLOGY}> <http://purl.org/dc/terms/title> "The Onto"@en .
<{ONTOLOGY}> <{RDFS}comment> "About it." .
""".encode()


def test_annotations_from_fuseki(fuseki_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the ontology's annotations are its annotation triples, not its imports or version IRI."""
    with fuseki_dataset(fuseki_url, {None: ONTOLOGY_TRIPLES}) as dataset:
        monkeypatch.setattr(browl.home, "dataset_name", lambda generation: dataset)
        app = make_app(FUSEKI_URL=fuseki_url)
        with TestClient(app) as client:
            ready(app, client)
            text = client.get("/onto").text
    panel = text[text.index('<h2 id="annotations-heading">') : text.index('<h2 id="imports-heading">')]
    assert re.findall(r"<dt>(.*?)</dt>", panel) == [
        '<code dir="ltr">http://purl.org/dc/terms/title</code>',
        '<code dir="ltr">owl:versionInfo</code>',
        '<code dir="ltr">rdfs:comment</code>',
    ]


PREFIXES = {
    "obo": "http://purl.obolibrary.org/obo/",
    "": f"{ONTOLOGY}#",
    "rid": "http://example.org/rid/",
    "RID": "http://radlex.org/RID/",
    "x": "<x>",
}
"""Prefixes whose order by name is case-insensitive, `RID` before `rid` as they tie."""

PREFIX_NAMES = ["", "obo", "RID", "rid", "x"]


@pytest.mark.parametrize("most", [10, 5, 2])
def test_prefixes(annotations: list[str], app: FastAPI, monkeypatch: pytest.MonkeyPatch, most: int) -> None:
    """Check that the prefixes are listed by name, case-insensitively, at most `MAX_PREFIXES`, saying how many more."""
    monkeypatch.setattr(browl.home, "MAX_PREFIXES", most)
    text = page(app, _with_rows(ontology_stage(), "prefix", PREFIXES))
    panel = text[text.index('<h2 id="prefixes-heading">') :].split("</section>")[0]
    names = [f'<code dir="ltr">{name}:</code>' for name in PREFIX_NAMES]
    assert re.findall(r"<dt>(.*?)</dt>", panel) == names[:most]
    assert ("more.</p>" in panel) == (most < len(PREFIXES))
    if most == 2:
        assert '<p class="note">And 3 more.</p>' in panel
    else:
        assert '<dd><code dir="ltr">&lt;x&gt;</code></dd>' in panel


def test_long_prefix(annotations: list[str], app: FastAPI) -> None:
    """Check that a long prefix name and namespace are cut, as an ontology file's own can be any length."""
    name, namespace = "p" * 300, f"http://example.org/{'n' * 300}/"
    text = page(app, _with_rows(ontology_stage(), "prefix", {name: namespace}))
    assert f'<dt><code dir="ltr">{name[: MAX_IRI_CHARS - 1]}…:</code></dt>' in text
    assert f'<dd><code dir="ltr">{namespace[: MAX_IRI_CHARS - 1]}…</code></dd>' in text


def test_no_prefixes(annotations: list[str], client: TestClient) -> None:
    """Check that the Prefixes panel says so when there are none."""
    text = client.get("/onto").text
    panel = text[text.index('<h2 id="prefixes-heading">') :].split("</section>")[0]
    assert '<p class="note">None.</p>' in panel


@pytest.mark.parametrize("defined", [0, 2])
def test_axioms(annotations: list[str], app: FastAPI, defined: int) -> None:
    """Check that the axiom counts are shown, with their total, and datatype definitions only if there are any."""
    counts = {"logical": 1_234, "declaration": 56, "annotation": 7, "datatype_definition": defined}
    text = page(app, _with_rows(ontology_stage(), "axiom_count", counts))
    lines = [line.strip() for line in text[text.index("<dt>Axioms</dt>") :].splitlines()[:6]]
    assert lines[:5] == [
        "<dt>Axioms</dt>",
        f"<dd>Total: {1_297 + defined:,}</dd>",
        "<dd>Logical: 1,234</dd>",
        "<dd>Declarations: 56</dd>",
        "<dd>Annotation: 7</dd>",
    ]
    assert ("Datatype definitions: 2" in text) == bool(defined)


def test_no_axiom_counts(annotations: list[str], client: TestClient) -> None:
    """Check that a derived database without axiom counts shows no Axioms row, rather than zeros."""
    assert "<dt>Axioms</dt>" not in client.get("/onto").text
