"""Tests for axiom annotations (U-35, P-49; DESIGN.md §2.8, Axiom annotations).

The annotations on annotation values and logical axioms, on the Overview's panels; the pruning of cyclic or too deeply
nested annotations; the caps that leave them out with a note. The Usage tab's are in `tests/test_usage_tab.py`.
"""

import asyncio
import functools
import logging
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx2
import jpype
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

import browl.annotations
import browl.overview
from browl import axioms, frames, nesting, owl, owlapi
from browl.annotations import (
    IRIValue,
    LiteralValue,
    TooManyAxiomAnnotationsError,
    annotated,
    fetch_value_annotations,
    value_annotations,
)
from browl.axioms import LeftOut, fetch_fragment, pruned_annotations
from browl.derived import EntityType
from browl.fuseki import FusekiClient
from browl.jobs import Stage
from browl.links import EntityLink
from browl.nesting import annotation_sources
from browl.pages import templates
from browl.rdf import IRI, OWL, RDF, RDFS, XSD, BlankNode, Literal
from tests.helpers import boxed, derived_stage, fuseki_dataset, make_app, ontology_ntriples

NS = "http://example.org/axioms#"
NOTE, REMARK, COMMENT = (IRI(f"{NS}{name}") for name in ("note", "remark", "comment"))
KIDNEY, BLADDER = IRI(f"{NS}Kidney"), IRI(f"{NS}Bladder")
XSD_STRING = f"{XSD}string"


def owl_literal(text: str) -> owl.Literal:
    """Return a plain string, as an annotation node's value."""
    return owl.Literal(lexical=text, datatype=IRI(XSD_STRING))


def note(value: str, *nested: owl.Annotation) -> owl.Annotation:
    """Return a `:note` annotation with a string value, and its own annotations."""
    return owl.Annotation(
        property=owl.Entity(kind=owl.EntityKind.ANNOTATION_PROPERTY, iri=NOTE),
        value=owl_literal(value),
        annotations=nested,
    )


def depth(annotations: tuple[owl.Annotation, ...]) -> int:
    """Return how deep annotations nest: 1 for annotations with none of their own."""
    return max((1 + depth(each.annotations) for each in annotations), default=0)


# The trees annotation nodes form (`annotation_sources`), for the Overview's query and the fragments alike.


def test_sources_chain() -> None:
    """Check that a well-formed chain keeps each node's one source."""
    sources = {"ax": ["E"], "n1": ["ax"], "n2": ["n1"]}
    assert annotation_sources(sources) == {"ax": "E", "n1": "ax", "n2": "n1"}


def test_sources_cycle() -> None:
    """Check that a cycle ends: a node keeps the source on the shallowest path, and one nothing outside reaches goes."""
    # n1 is on the axiom and on n2, which is on n1; m1 and m2 are on each other alone.
    sources = {"ax": ["E"], "n1": ["n2", "ax"], "n2": ["n1"], "m1": ["m2"], "m2": ["m1"]}
    assert annotation_sources(sources) == {"ax": "E", "n1": "ax", "n2": "n1"}


def test_sources_shared() -> None:
    """Check that a node with two sources at one depth keeps the first by the key, so it's shown once."""
    sources = {"a": ["E"], "b": ["E"], "n": ["b", "a"]}
    assert annotation_sources(sources)["n"] == "a"


def test_sources_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that nodes deeper than `MAX_ANNOTATION_DEPTH` are left out."""
    monkeypatch.setattr(nesting, "MAX_ANNOTATION_DEPTH", 3)
    sources = {f"n{i}": [f"n{i - 1}" if i else "E"] for i in range(6)}
    assert sorted(annotation_sources(sources)) == ["n0", "n1", "n2"]


AXIOM = f"""<{NS}A> <{RDF}type> <{OWL}Class> .
<{NS}B> <{RDF}type> <{OWL}Class> .
<{NOTE}> <{RDF}type> <{OWL}AnnotationProperty> .
<{NS}A> <{RDFS}subClassOf> <{NS}B> .
_:ax <{RDF}type> <{OWL}Axiom> .
_:ax <{OWL}annotatedSource> <{NS}A> .
_:ax <{OWL}annotatedProperty> <{RDFS}subClassOf> .
_:ax <{OWL}annotatedTarget> <{NS}B> .
_:ax <{NOTE}> "0" .
"""
"""`A SubClassOf B`, annotated `:note "0"`."""


def nested_chain(length: int) -> str:
    """Return `owl:Annotation`s nested `length` deep on `AXIOM`'s annotation, each `:note` the next number."""
    lines = []
    for i in range(1, length + 1):
        source = "_:ax" if i == 1 else f"_:n{i - 1}"
        lines += [
            f"_:n{i} <{RDF}type> <{OWL}Annotation> .",
            f"_:n{i} <{OWL}annotatedSource> {source} .",
            f"_:n{i} <{OWL}annotatedProperty> <{NOTE}> .",
            f'_:n{i} <{OWL}annotatedTarget> "{i - 1}" .',
            f'_:n{i} <{NOTE}> "{i}" .',
        ]
    return "\n".join(lines) + "\n"


CYCLE = (
    AXIOM + nested_chain(2) + f"_:n1 <{OWL}annotatedSource> _:n2 .\n"  # n1 is on n2 too, which is on n1
)
"""`AXIOM`, its annotation annotated twice nested, the inner one also the outer's source: a cycle."""


def test_pruned() -> None:
    """Check that pruning drops a cycle's back link and leaves well-formed N-Triples as they are."""
    pruned = pruned_annotations(CYCLE.encode())[0].decode()
    assert f"_:n1 <{OWL}annotatedSource> _:n2 ." not in pruned
    assert f"_:n1 <{OWL}annotatedSource> _:ax ." in pruned
    assert len(pruned.splitlines()) == len(CYCLE.splitlines()) - 1
    well_formed = (AXIOM + nested_chain(3)).encode()
    assert pruned_annotations(well_formed)[0] is well_formed


def many_annotations(count: int) -> str:
    """Return `AXIOM`'s reification with `count` `:note`s in all, as an uploaded ontology could hold."""
    return AXIOM + "".join(f'_:ax <{NOTE}> "{i}" .\n' for i in range(1, count))


def test_annotation_count() -> None:
    """Check that pruning counts the annotations the kept nodes hold, nested ones too, not their links or type."""
    assert pruned_annotations(AXIOM.encode())[1] == 1
    assert pruned_annotations((AXIOM + nested_chain(3)).encode())[1] == 4
    assert pruned_annotations(CYCLE.encode())[1] == 3
    assert pruned_annotations(many_annotations(3_000).encode())[1] == 3_000
    assert pruned_annotations(OWN.encode()) == (OWN.encode(), 0)


def parsed(ntriples: bytes) -> list[owl.AnyAxiom]:
    """Return `A`'s axioms in N-Triples, pruned as a page's fragment is."""
    return asyncio.run(owlapi.run_java(axioms.entity_axioms, pruned_annotations(ntriples)[0], f"{NS}A"))


@pytest.mark.usefixtures("jvm")
def test_cycle_parses() -> None:
    """Check that a cycle of annotations, which overflows the OWLAPI parser's stack, shows once pruned."""
    with pytest.raises(axioms.FragmentError):  # the OWLAPI's StackOverflowError, unpruned
        asyncio.run(owlapi.run_java(axioms.entity_axioms, CYCLE.encode(), f"{NS}A"))
    [axiom] = parsed(CYCLE.encode())
    assert axiom.annotations == (note("0", note("1", note("2"))),)


@pytest.mark.usefixtures("jvm")
def test_deep_fragment() -> None:
    """Check that annotations nested past `MAX_ANNOTATION_DEPTH` are left out, and the axiom still shows."""
    [axiom] = parsed((AXIOM + nested_chain(nesting.MAX_DEPTH + 10)).encode())
    assert depth(axiom.annotations) == nesting.MAX_ANNOTATION_DEPTH


def deep_axiom() -> list[owl.AnyAxiom]:
    """Convert `A SubClassOf B` with annotations nested `MAX_DEPTH` + 10 deep, as the OWLAPI's data factory makes it."""
    factory = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").getOWLDataFactory()
    create = jpype.JClass("org.semanticweb.owlapi.model.IRI").create
    singleton = jpype.JClass("java.util.Collections").singleton
    prop = factory.getOWLAnnotationProperty(create(NOTE))
    annotation = factory.getOWLAnnotation(prop, factory.getOWLLiteral("deepest"))
    for i in range(nesting.MAX_DEPTH + 10):
        annotation = factory.getOWLAnnotation(prop, factory.getOWLLiteral(str(i)), singleton(annotation))
    a, b = (factory.getOWLClass(create(f"{NS}{name}")) for name in "AB")
    return axioms.convert_axioms([factory.getOWLSubClassOfAxiom(a, b, singleton(annotation))])


@pytest.mark.usefixtures("jvm")
def test_deep_conversion() -> None:
    """Check that converting stops at `MAX_ANNOTATION_DEPTH`, rather than failing the axiom at `MAX_DEPTH`."""
    [axiom] = asyncio.run(owlapi.run_java(deep_axiom))
    assert depth(axiom.annotations) == nesting.MAX_ANNOTATION_DEPTH
    TypeAdapter(owl.AnyAxiom).validate_json(TypeAdapter(owl.AnyAxiom).dump_json(axiom))  # within JSON's depth


# The annotations panel's (`browl.annotations.value_annotations`).


def rows(*triples: tuple[str, str, str, str, str, object]) -> list[tuple[object, ...]]:
    """Return the query's rows: node, its source, property and target, an annotation's property and value."""

    def term(text: object) -> object:
        if not isinstance(text, str):
            return text
        return BlankNode(text[2:]) if text.startswith("_:") else IRI(text)

    return [tuple(map(term, row)) for row in triples]


def test_value_annotations() -> None:
    """Check values' annotations: nested ones under theirs, repeats and blank values dropped, reifications merged.

    A logical axiom's annotations are kept by their key, which no value has, so `annotated` never adds them.
    """
    text = Literal("filters blood")
    found = value_annotations(
        KIDNEY,
        rows(
            ("_:ax", KIDNEY, COMMENT, text, NOTE, Literal("a source")),
            ("_:ax", KIDNEY, COMMENT, text, NOTE, Literal("a source")),  # a repeat, as from the imports' merge
            ("_:ax", KIDNEY, COMMENT, text, NOTE, "_:anonymous"),  # a blank value, left out
            ("_:n", "_:ax", NOTE, Literal("a source"), REMARK, BLADDER),
            ("_:ax2", KIDNEY, COMMENT, text, NOTE, Literal("second")),  # the same assertion reified again
            ("_:logical", KIDNEY, f"{RDFS}subClassOf", f"{NS}Organ", NOTE, Literal("logical")),
        ),
    )
    remark = owl.Annotation(
        property=owl.Entity(kind=owl.EntityKind.ANNOTATION_PROPERTY, iri=REMARK), value=owl.IRIReference(iri=BLADDER)
    )
    value = LiteralValue(lexical="filters blood", datatype=IRI(XSD_STRING))
    assert found[COMMENT, value] == (note("a source", remark), note("second"))
    pairs = annotated([(COMMENT, value), (IRI(f"{RDFS}label"), value)], found)
    assert pairs == [
        (COMMENT, value.model_copy(update={"annotations": (note("a source", remark), note("second"))})),
        (IRI(f"{RDFS}label"), value),
    ]


def test_value_annotations_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a cycle of annotations ends, and nesting stops at `MAX_ANNOTATION_DEPTH`."""
    text = Literal("x")
    cycle = rows(
        ("_:ax", KIDNEY, COMMENT, text, NOTE, Literal("1")),
        ("_:n1", "_:ax", NOTE, Literal("1"), NOTE, Literal("2")),
        ("_:n2", "_:n1", NOTE, Literal("2"), NOTE, Literal("1")),
        ("_:n1", "_:n2", NOTE, Literal("1"), NOTE, Literal("2")),  # n1 on n2 too
    )
    value = LiteralValue(lexical="x", datatype=IRI(XSD_STRING))
    assert value_annotations(KIDNEY, cycle)[COMMENT, value] == (note("1", note("2", note("1"))),)
    monkeypatch.setattr(nesting, "MAX_ANNOTATION_DEPTH", 2)
    assert value_annotations(KIDNEY, cycle)[COMMENT, value] == (note("1", note("2")),)


def answer(body: str) -> FusekiClient:
    """Return a client of a stubbed Fuseki that answers every query with this TSV result."""

    def fuseki(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text=body, headers={"Content-Type": "text/tab-separated-values"})

    return FusekiClient(httpx2.AsyncClient(transport=httpx2.MockTransport(fuseki), base_url="http://fuseki"))


HEADER = "?node\t?source\t?about\t?target\t?property\t?value\n"
ROW = f'_:ax\t<{KIDNEY}>\t<{COMMENT}>\t"x"\t<{NOTE}>\t"y"\n'


@pytest.mark.anyio
async def test_too_many_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that more rows than `MAX_AXIOM_ANNOTATIONS`, or more characters than allowed, raise."""
    monkeypatch.setattr(browl.annotations, "MAX_AXIOM_ANNOTATIONS", 1)
    assert len(await fetch_value_annotations(answer(HEADER + ROW), "ds", KIDNEY)) == 1
    with pytest.raises(TooManyAxiomAnnotationsError, match="more than 1 axiom annotations"):
        await fetch_value_annotations(answer(HEADER + ROW + ROW), "ds", KIDNEY)
    monkeypatch.setattr(browl.annotations, "MAX_ANNOTATIONS_CHARS", 10)
    with pytest.raises(TooManyAxiomAnnotationsError, match="longer than 10 characters"):
        await fetch_value_annotations(answer(HEADER + ROW), "ds", KIDNEY)


# Against Fuseki, on `tests/data/axioms.ofn`.


@pytest.fixture(scope="module")
def dataset(fuseki_url: str, jvm: None) -> Iterator[str]:
    """Yield the name of a Fuseki dataset holding the test ontology."""
    with fuseki_dataset(fuseki_url, {None: ontology_ntriples()}) as name:
        yield name


@pytest.mark.anyio
async def test_fetch(fuseki_url: str, dataset: str) -> None:
    """Check Kidney's values' annotations, nested, from the store."""
    async with httpx2.AsyncClient(base_url=fuseki_url) as http:
        found = await fetch_value_annotations(FusekiClient(http), dataset, KIDNEY)
    remark = owl.Annotation(
        property=owl.Entity(kind=owl.EntityKind.ANNOTATION_PROPERTY, iri=REMARK), value=owl.IRIReference(iri=BLADDER)
    )
    filters = LiteralValue(lexical="filters blood", datatype=IRI(XSD_STRING))
    assert found[COMMENT, filters] == (note("a source", remark), note("second"))
    rein = LiteralValue(lexical="rein", datatype=IRI(f"{RDF}langString"), language="fr")
    assert found[COMMENT, rein] == (note("en français"),)
    see_also = found[IRI(f"{RDFS}seeAlso"), IRIValue(iri=BLADDER)]
    assert [(each.property.iri, each.value) for each in see_also] == [(REMARK, owl.IRIReference(iri=f"{NS}Organ"))]


@pytest.mark.anyio
async def test_panels_own_annotations(fuseki_url: str, dataset: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that each Overview panel's query reads only its own axioms' annotations, so neither counts the other's.

    Cat's one annotated axiom is logical; Kidney's comment is annotated, and so is its restriction.
    """
    monkeypatch.setattr(browl.annotations, "MAX_AXIOM_ANNOTATIONS", 0)
    async with httpx2.AsyncClient(base_url=fuseki_url) as http:
        assert await fetch_value_annotations(FusekiClient(http), dataset, f"{NS}Cat") == {}
        fragment = await fetch_fragment(FusekiClient(http), dataset, KIDNEY)
    assert b'"restricted"' in fragment.annotations
    assert b'"a source"' not in fragment.ntriples and b'"en fran' not in fragment.ntriples


def fuseki_stub(answers: dict[str, httpx2.Response]) -> FusekiClient:
    """Return a client of a stubbed Fuseki that answers a query holding a key's text with its response."""

    def fuseki(request: httpx2.Request) -> httpx2.Response:
        query = httpx2.QueryParams(request.content.decode())["query"]
        for text, response in answers.items():
            if text in query:
                return response
        return ntriples("")

    return FusekiClient(httpx2.AsyncClient(transport=httpx2.MockTransport(fuseki), base_url="http://fuseki"))


def ntriples(text: str) -> httpx2.Response:
    """Return a stubbed Fuseki's N-Triples response."""
    return httpx2.Response(200, text=text, headers={"Content-Type": "application/n-triples"})


OWN = f"<{NS}A> <{RDFS}subClassOf> <{NS}B> .\n"
ANNOTATION = f"_:ax <{OWL}annotatedSource> <{NS}A> .\n_:ax <{NOTE}> <{NS}C> .\n_:ax <{NOTE}> <{NS}D> .\n"
"""An axiom's annotation that mentions two more IRIs than `OWN`, and its note."""
ANNOTATIONS_QUERY = "owl:annotatedSource* ?axiom"
TYPES_QUERY = "VALUES ?iri"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("max_iris", "max_bytes", "left_out"),
    [(10, 10**6, None), (5, 10**6, LeftOut.TOO_MANY), (10, len(OWN) + 10, LeftOut.TOO_MANY)],
    ids=["shown", "too-many-iris", "too-large"],
)
async def test_fragment_caps(
    monkeypatch: pytest.MonkeyPatch, max_iris: int, max_bytes: int, left_out: LeftOut | None
) -> None:
    """Check that annotations that would take the fragment past its caps are left out, and the axioms still fetched."""
    monkeypatch.setattr(axioms, "MAX_FRAGMENT_IRIS", max_iris)
    monkeypatch.setattr(axioms, "CONSTRUCT_MAX_BYTES", max_bytes)
    sparql = fuseki_stub(
        {ANNOTATIONS_QUERY: ntriples(ANNOTATION), TYPES_QUERY: ntriples(""), "DESCRIBE": ntriples(OWN)}
    )
    fragment = await fetch_fragment(sparql, "ds", f"{NS}A")
    assert fragment.annotations_left_out == left_out
    assert (f"<{NOTE}>".encode() in fragment.ntriples) == (left_out is None)


@pytest.mark.anyio
async def test_fragment_too_many_annotations(caplog: pytest.LogCaptureFixture) -> None:
    """Check that more annotations than `MAX_FRAGMENT_ANNOTATIONS`, on one axiom, within the other caps, are left out.

    The OWLAPI's parse of them grows with the square of their number.
    """
    assert axioms.MAX_FRAGMENT_ANNOTATIONS < 3_000
    annotations = many_annotations(3_000)
    sparql = fuseki_stub(
        {ANNOTATIONS_QUERY: ntriples(annotations), TYPES_QUERY: ntriples(""), "DESCRIBE": ntriples(OWN)}
    )
    fragment = await fetch_fragment(sparql, "ds", f"{NS}A")
    assert fragment == axioms.Fragment(OWN.encode(), annotations_left_out=LeftOut.TOO_MANY)
    assert "without their annotations, which are too many" in caplog.text


@pytest.mark.anyio
async def test_fragment_result_too_large(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Check that annotations whose query's result is larger than `FusekiClient.construct` holds are too many."""
    small = functools.partialmethod(FusekiClient.construct, max_bytes=len(OWN))
    monkeypatch.setattr(FusekiClient, "construct", small)
    sparql = fuseki_stub(
        {ANNOTATIONS_QUERY: ntriples(ANNOTATION), TYPES_QUERY: ntriples(""), "DESCRIBE": ntriples(OWN)}
    )
    fragment = await fetch_fragment(sparql, "ds", f"{NS}A")
    assert fragment == axioms.Fragment(OWN.encode(), annotations_left_out=LeftOut.TOO_MANY)
    assert "without their annotations, which are too many" in caplog.text


@pytest.mark.anyio
async def test_fragment_scans_off_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a fragment's scans, of its IRIs and its annotations, run off the event loop."""
    on_loop = []

    def recorded(function: Callable[..., object]) -> Callable[..., object]:
        def call(*args: object) -> object:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                on_loop.append(False)
            else:
                on_loop.append(True)
            return function(*args)

        return call

    for name in ("pruned_annotations", "ntriples_iris"):
        monkeypatch.setattr(axioms, name, recorded(getattr(axioms, name)))
    sparql = fuseki_stub(
        {ANNOTATIONS_QUERY: ntriples(ANNOTATION), TYPES_QUERY: ntriples(""), "DESCRIBE": ntriples(OWN)}
    )
    fragment = await fetch_fragment(sparql, "ds", f"{NS}A")
    assert fragment.annotations
    assert on_loop and not any(on_loop)


@pytest.mark.anyio
async def test_fragment_query_fails(caplog: pytest.LogCaptureFixture) -> None:
    """Check that a failed annotations query leaves them out, logged; one that fails with the axioms isn't logged."""
    failed = httpx2.Response(500, text="Error 500")
    sparql = fuseki_stub({ANNOTATIONS_QUERY: failed, "DESCRIBE": ntriples(OWN)})
    with caplog.at_level(logging.ERROR, logger="browl.axioms"):
        fragment = await fetch_fragment(sparql, "ds", f"{NS}A")
    assert fragment.annotations_left_out == LeftOut.FAILED
    assert "without their annotations, which couldn't be read" in caplog.text
    caplog.clear()
    with pytest.raises(axioms.SparqlError):
        await fetch_fragment(fuseki_stub({"": failed}), "ds", f"{NS}A")
    assert "without their annotations" not in caplog.text


# The page.

ENTITIES = [
    (KIDNEY, EntityType.CLASS),
    (BLADDER, EntityType.CLASS),
    (f"{NS}Organ", EntityType.CLASS),
    (NOTE, EntityType.ANNOTATION_PROPERTY),
    (COMMENT, EntityType.ANNOTATION_PROPERTY),
    (REMARK, EntityType.ANNOTATION_PROPERTY),
]
NAMES = {KIDNEY: ("kidney", None), BLADDER: ("bladder", None), NOTE: ("note", None), REMARK: ("remark", None)}


def link(name: str) -> str:
    """Return the link to a test class's page, by its display name."""
    return f'<a href="/axioms#{name.capitalize()}"><bdi>{name}</bdi></a>'


def overviews(dataset: str, fuseki_url: str, monkeypatch: pytest.MonkeyPatch, *fragments: str) -> list[str]:
    """Return the Overview of each test class, by its IRI's fragment, from the store, checking each is served."""
    monkeypatch.setattr(browl.overview, "dataset_name", lambda generation: dataset)
    app = make_app(FUSEKI_URL=fuseki_url)
    with TestClient(app) as client:
        app.state.jobs.register(Stage.PRE_CACHING, derived_stage(ENTITIES, names=NAMES))
        client.portal.call(app.state.jobs.start)
        client.portal.call(app.state.jobs.wait)
        pages = [
            client.get("/_browl/hash", params={"path": "/axioms", "fragment": fragment, "panel": "1"})
            for fragment in fragments
        ]
    assert [page.status_code for page in pages] == [200] * len(fragments)
    return [page.text for page in pages]


def panel(page: str, name: str) -> str:
    """Return an Overview panel's HTML, by its class: `annotations` or `axioms`."""
    found = re.search(rf'<section class="{name} panel".*?</section>', page, re.DOTALL)
    assert found is not None
    return found.group()


note_link = '<a href="/axioms#note"><bdi>note</bdi></a>'
value = '<bdi class="value">{}</bdi>'.format
LEFT_OUT_NOTES = {"too_many": "are too many to show here", "failed": "can't be shown just now"}


def test_page(data_dir: Path, dataset: str, fuseki_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check the Overview's axiom annotations: after their value or axiom, in boxes, nested ones within.

    A symmetric axiom stated on Bladder shows its annotations on Kidney's page too.
    """
    kidney, bladder = overviews(dataset, fuseki_url, monkeypatch, "Kidney", "Bladder")
    remark_link = '<a href="/axioms#remark"><bdi>remark</bdi></a>'
    nested = boxed(f"{remark_link}: {link('bladder')}")
    filters = boxed(f"{note_link}: {value('a source')}{nested}, {value('second')}")
    assert f"<dd>{value('filters blood')}{filters}</dd>" in kidney
    assert '<dd><bdi class="value" lang="fr">rein</bdi> <small class="lang">fr</small>' in kidney
    assert boxed(f'{remark_link}: <a href="/axioms#Organ"><bdi>{NS}Organ</bdi></a>') in kidney
    apart = boxed(f"{note_link}: {value('apart')}")
    assert re.search(rf'<dt>Disjoint With</dt>\s*<dd dir="ltr">{re.escape(link("bladder") + apart)}</dd>', kidney)
    assert re.search(rf'<dt>Disjoint With</dt>\s*<dd dir="ltr">{re.escape(link("kidney") + apart)}</dd>', bladder)
    assert boxed(f"{note_link}: {value('restricted')}") in kidney
    assert "too many to show" not in kidney and "can't be shown" not in kidney


@pytest.mark.usefixtures("data_dir")
@pytest.mark.parametrize("left_out", ["too_many", "failed"])
def test_values_left_out(
    dataset: str, fuseki_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, left_out: str
) -> None:
    """Check that the annotations panel's values are shown without their axiom annotations, with a note, if need be.

    That's when they're too many or their query fails. The axioms panel keeps its own.
    """
    if left_out == "too_many":
        monkeypatch.setattr(browl.annotations, "MAX_AXIOM_ANNOTATIONS", 0)
    else:
        monkeypatch.setattr(browl.annotations, "_VALUE_ANNOTATIONS", browl.annotations._VALUE_ANNOTATIONS + " nonsense")
    [kidney] = overviews(dataset, fuseki_url, monkeypatch, "Kidney")
    annotations = panel(kidney, "annotations")
    assert f"<dd>{value('filters blood')}</dd>" in annotations
    assert "axiom-annotation" not in annotations
    assert f"These annotations' own annotations {LEFT_OUT_NOTES[left_out]}." in annotations
    axioms_panel = panel(kidney, "axioms")
    assert boxed(f"{note_link}: {value('restricted')}") in axioms_panel
    assert "These axioms' annotations" not in axioms_panel
    logged = {"too_many": "without their axiom annotations", "failed": "Couldn't show the axiom annotations"}
    assert logged[left_out] in caplog.text


@pytest.mark.usefixtures("data_dir")
@pytest.mark.parametrize("left_out", ["too_many", "failed"])
def test_axioms_left_out(dataset: str, fuseki_url: str, monkeypatch: pytest.MonkeyPatch, left_out: str) -> None:
    """Check that the axioms panel's axioms are shown without their annotations, with a note, if need be.

    That's when they're too many or their query fails. The annotations panel keeps its values' own.
    """
    if left_out == "too_many":
        monkeypatch.setattr(axioms, "MAX_FRAGMENT_ANNOTATIONS", 0)
    else:
        monkeypatch.setattr(axioms, "_AXIOM_ANNOTATIONS", axioms._AXIOM_ANNOTATIONS + " nonsense")
    [kidney] = overviews(dataset, fuseki_url, monkeypatch, "Kidney")
    axioms_panel = panel(kidney, "axioms")
    assert link("bladder") in axioms_panel and "Disjoint With" in axioms_panel
    assert "axiom-annotation" not in axioms_panel
    assert f"These axioms' annotations {LEFT_OUT_NOTES[left_out]}." in axioms_panel
    annotations = panel(kidney, "annotations")
    assert boxed(f"{note_link}: {value('en français')}") in annotations
    assert "own annotations" not in annotations


def render(source: str, **context: object) -> str:
    """Render a template's text."""
    return templates.env.from_string(source).render(**context)


def test_left_out_notes() -> None:
    """Check each panel's note when its axiom annotations are left out, too many or failed."""
    panel = '{% from "annotations.html" import annotation_list %}{{ annotation_list(panel, {}) }}'
    group = browl.annotations.AnnotationGroup(property=NOTE, values=(IRIValue(iri=BLADDER),))
    a, b = (owl.Entity(kind=owl.EntityKind.CLASS, iri=f"{NS}{name}") for name in "AB")
    lines = frames.frame_groups([owl.SubClassOf(sub=a, super=b)], a.iri, [], str)
    for left_out, text in [("too_many", "are too many to show here"), ("failed", "can't be shown just now")]:
        shown = render(panel, panel=browl.overview.AnnotationsPanel(groups=[group], annotations_left_out=left_out))
        assert f"These annotations' own annotations {text}." in shown
        axioms_panel = browl.overview.AxiomsPanel(groups=lines, annotations_left_out=left_out)
        shown = render('{% from "axioms.html" import axiom_frame %}{{ axiom_frame(p, {}, "x") }}', p=axioms_panel)
        assert f"These axioms' annotations {text}." in shown


def test_rendering() -> None:
    """Check the markup: boxes outside each value's `<bdi>`, blank values left out, the entity marked."""
    anonymous = owl.Annotation(
        property=owl.Entity(kind=owl.EntityKind.ANNOTATION_PROPERTY, iri=REMARK),
        value=owl.AnonymousIndividual(id="_:b1"),
    )
    marked = owl.Annotation(
        property=owl.Entity(kind=owl.EntityKind.ANNOTATION_PROPERTY, iri=REMARK), value=owl.IRIReference(iri=KIDNEY)
    )
    template = '{% from "annotations.html" import axiom_annotations %}{{ axiom_annotations(a, links, mark) }}'
    links = {KIDNEY: EntityLink(KIDNEY, "kidney", "/axioms#Kidney")}
    assert render(template, a=(anonymous,), links=links, mark=None) == ""
    shown = render(template, a=(note("]evil["), anonymous, marked), links=links, mark=KIDNEY)
    assert shown == boxed(
        f'<code dir="ltr">{NOTE}</code>: <bdi class="value">]evil[</bdi>',
        f'<code dir="ltr">{REMARK}</code>: <mark><a href="/axioms#Kidney"><bdi>kidney</bdi></a></mark>',
    )


def test_merged_equivalents() -> None:
    """Check that an equivalent stated by two axioms, each annotated, is one line with both annotations, each once."""
    a, b, c = (owl.Entity(kind=owl.EntityKind.CLASS, iri=f"{NS}{name}") for name in "ABC")
    two = owl.EquivalentClasses(members=(a, b), annotations=(note("one"), note("both")))
    three = owl.EquivalentClasses(members=(a, b, c), annotations=(note("both"), note("three")))
    [group] = frames.frame_groups([two, three], a.iri, [], str)
    assert [(line.operands, line.annotations) for line in group.lines] == [
        ((b,), (note("one"), note("both"), note("three"))),
        ((c,), (note("both"), note("three"))),
    ]
    assert group.lines[0].axiom == two.without_annotations()


def test_json() -> None:
    """Check that values and frame lines keep their annotations through JSON, for the API (U-42)."""
    value = IRIValue(iri=BLADDER, annotations=(note("a", note("b")),))
    group = browl.annotations.AnnotationGroup(property=REMARK, values=(value,))
    panel = browl.overview.AnnotationsPanel(groups=[group])
    adapter = TypeAdapter(browl.overview.AnnotationsPanel)
    assert adapter.validate_json(adapter.dump_json(panel)) == panel
    a, b = (owl.Entity(kind=owl.EntityKind.CLASS, iri=f"{NS}{name}") for name in "AB")
    groups = frames.frame_groups([owl.SubClassOf(sub=a, super=b, annotations=(note("x"),))], a.iri, [], str)
    axioms_panel = browl.overview.AxiomsPanel(groups=groups)
    axioms_adapter = TypeAdapter(browl.overview.AxiomsPanel)
    assert axioms_adapter.validate_json(axioms_adapter.dump_json(axioms_panel)) == axioms_panel
    assert groups[0].lines[0].annotations == (note("x"),)
