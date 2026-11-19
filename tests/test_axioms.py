"""Tests for entities' axioms: fragments fetched from Fuseki, closures, Manchester rendering, and the entity page."""

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx2
import jpype
import pytest
from fastapi.testclient import TestClient

import browl.overview
from browl import axioms, manchester, owlapi
from browl.axioms import Axiom, Syntax
from browl.derived import EntityType
from browl.fuseki import UNION_FROM, FusekiClient
from browl.jobs import Stage
from browl.manchester import SegmentKind
from browl.rdf import RDF, XSD, XSD_LEXICAL_FORMS
from tests.helpers import derived_stage, make_app

pytestmark = [pytest.mark.jvm, pytest.mark.usefixtures("jvm")]

ONTOLOGY = Path(__file__).parent / "data" / "axioms.ofn"
NS = "http://example.org/axioms#"

KINDS = {
    "SubClassOf",
    "EquivalentClasses",
    "DisjointClasses",
    "DisjointUnion",
    "HasKey",
    "SubObjectPropertyOf",
    "SubPropertyChainOf",
    "EquivalentObjectProperties",
    "DisjointObjectProperties",
    "InverseObjectProperties",
    "ObjectPropertyDomain",
    "ObjectPropertyRange",
    "FunctionalObjectProperty",
    "InverseFunctionalObjectProperty",
    "ReflexiveObjectProperty",
    "IrreflexiveObjectProperty",
    "SymmetricObjectProperty",
    "AsymmetricObjectProperty",
    "TransitiveObjectProperty",
    "SubDataPropertyOf",
    "EquivalentDataProperties",
    "DisjointDataProperties",
    "DataPropertyDomain",
    "DataPropertyRange",
    "FunctionalDataProperty",
    "DatatypeDefinition",
    "ClassAssertion",
    "ObjectPropertyAssertion",
    "NegativeObjectPropertyAssertion",
    "DataPropertyAssertion",
    "NegativeDataPropertyAssertion",
    "SameIndividual",
    "DifferentIndividuals",
    "SubAnnotationPropertyOf",
    "AnnotationPropertyDomain",
    "AnnotationPropertyRange",
}
"""Every kind of OWL 2 axiom but declarations and annotation assertions, as `Axiom.kind` names them."""


@pytest.fixture(scope="module")
def ntriples() -> bytes:
    """Return the test ontology as N-Triples, as the OWLAPI writes it: the RDF that browl's store would hold."""
    manager = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").createOWLOntologyManager()
    ontology = manager.loadOntologyFromOntologyDocument(jpype.JClass("java.io.File")(str(ONTOLOGY)))
    out = jpype.JClass("java.io.ByteArrayOutputStream")()
    manager.saveOntology(ontology, jpype.JClass("org.semanticweb.owlapi.formats.NTriplesDocumentFormat")(), out)
    return bytes(out.toByteArray())


def whole(ntriples: bytes) -> tuple[list[Axiom], dict[str, list[Axiom]]]:
    """Parse the whole ontology; return all its axioms but declarations and annotation assertions, and each entity's."""
    with owlapi.ntriples_ontology(ntriples) as ontology:
        every = [
            axiom.getAxiomWithoutAnnotations()
            for axiom in ontology.getAxioms()
            if str(axiom.getAxiomType().getName()) not in axioms._LEFT_OUT
        ]
        iris = {str(entity.getIRI()) for entity in ontology.getSignature()}
        return axioms.render_axioms(every), {
            iri: axioms.render_axioms(axioms.entity_closure(ontology, iri)) for iri in iris
        }


@pytest.fixture(scope="module")
def rendered(ntriples: bytes) -> tuple[list[Axiom], dict[str, list[Axiom]]]:
    """Return `whole` for the test ontology."""
    return asyncio.run(owlapi.run_java(whole, ntriples))


@pytest.fixture(scope="module")
def dataset(fuseki_url: str, ntriples: bytes) -> Iterator[str]:
    """Yield the name of an in-memory Fuseki dataset holding the test ontology, removed afterwards."""
    name = f"test-{uuid.uuid4().hex[:8]}"
    with httpx2.Client(base_url=fuseki_url, timeout=30) as client:
        client.post("/$/datasets", data={"dbName": name, "dbType": "mem"}).raise_for_status()
        try:
            headers = {"Content-Type": "application/n-triples"}
            client.post(f"/{name}/data", content=ntriples, headers=headers).raise_for_status()
            yield name
        finally:
            client.delete(f"/$/datasets/{name}")


def test_keywords() -> None:
    """Check that `browl.manchester.KEYWORDS` holds the words of the OWLAPI's own Manchester syntax keywords."""
    syntax = jpype.JClass("org.semanticweb.owlapi.manchestersyntax.parser.ManchesterOWLSyntax")
    words = {str(keyword.keyword()).removesuffix(":") for keyword in syntax.values()}
    assert {word for word in words if word.isalpha()} == manchester.KEYWORDS


def test_every_kind(rendered: tuple[list[Axiom], dict[str, list[Axiom]]]) -> None:
    """Check that every kind of axiom renders, in Manchester syntax but `DatatypeDefinition`, and has operands."""
    every, _ = rendered
    assert {axiom.kind for axiom in every} == KINDS
    for axiom in every:
        assert axiom.text
        assert axiom.syntax == (Syntax.FUNCTIONAL if axiom.kind == "DatatypeDefinition" else Syntax.MANCHESTER)
        assert len(axiom.operands) >= (1 if axiom.kind.endswith("Property") else 2), axiom
        assert all(axiom.operands), axiom
        assert "\n" not in axiom.text


def test_closure(rendered: tuple[list[Axiom], dict[str, list[Axiom]]]) -> None:
    """Check some entities' axioms: n-ary ones, general class axioms, property chains, keys, annotations stripped."""
    _, closures = rendered

    def texts(name: str) -> list[str]:
        return [axiom.text.replace(NS, "") for axiom in closures[f"{NS}{name}"]]

    assert texts("Cat") == ["<Cat> DisjointWith <Dog>", "<Cat> SubClassOf <Animal>"]  # its annotation stripped
    assert "DisjointClasses: <Animal>, <Fungus>, <Plant>" in texts("Plant")
    assert "<Organ> and (<partOf> some <Plant>) SubClassOf <http://www.w3.org/2002/07/owl#Nothing>" in texts("Plant")
    assert "<Person> HasKey <hasSSN>, <ssn>" in texts("Person")  # an object and a data property
    assert texts("hasUncle") == ["<hasParent> o <hasBrother> SubPropertyOf: <hasUncle>"]
    assert "not (<alice> <hates> <rex>)" in texts("alice")
    assert texts("partOf") == ["<hasPart> InverseOf <partOf>", "Transitive: <partOf>"]  # not the GCAs it's in


def test_operands(rendered: tuple[list[Axiom], dict[str, list[Axiom]]]) -> None:
    """Check that operands come in functional-syntax order, members one each, a property chain as one."""
    _, closures = rendered

    def operands(name: str, kind: str) -> list[tuple[str, ...]]:
        found = [axiom for axiom in closures[f"{NS}{name}"] if axiom.kind == kind]
        return [tuple(operand.replace(NS, "") for operand in axiom.operands) for axiom in found]

    assert operands("Heart", "SubClassOf") == [
        ("<Heart>", "<Organ>"),
        ("<Heart>", "<partOf> some (<Animal> and (not (<Plant>)))"),
        ("<hasPart> some <Heart>", "<Vertebrate>"),
    ]
    assert operands("hasAncestor", "SubPropertyChainOf") == [("<hasParent> o <hasParent>", "<hasAncestor>")]
    assert operands("Animal", "DisjointClasses") == [("<Animal>", "<Fungus>", "<Plant>")]
    assert operands("Adulthood", "DatatypeDefinition") == [
        ("<Adulthood>", "<http://www.w3.org/2001/XMLSchema#integer>[>= 18]")
    ]
    assert operands("Person", "HasKey") == [("<Person>", "<hasSSN>", "<ssn>")]
    [person] = [axiom for axiom in closures[f"{NS}Person"] if "nickname" in axiom.text]
    # Not the IRI in the literal; the literal's datatype, not in the text, comes last.
    assert person.iris == (f"{NS}Person", f"{NS}nickname", f"{RDF}PlainLiteral")


HOSTILE = '5 " <http://example.org/B> \u202e and'
"""A literal's text that would forge an IRI, a keyword, a bidi override and a quote if written unquoted."""
DATA_PROPERTY = f"<{NS}p> <{RDF}type> <http://www.w3.org/2002/07/owl#DatatypeProperty> .\n"


def hostile_assertions() -> tuple[set[str], list[tuple[str, Axiom]]]:
    """Return the built-in datatypes whose literals the renderer writes unquoted, and `HOSTILE` in each datatype.

    The built-in datatypes are OWLAPI's `OWL2Datatype`; `HOSTILE` is rendered in a data property assertion, for each of
    them and one that isn't built in.
    """
    factory = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").getOWLDataFactory()
    create = jpype.JClass("org.semanticweb.owlapi.model.IRI").create
    built_in = [
        each.getDatatype(factory) for each in jpype.JClass("org.semanticweb.owlapi.vocab.OWL2Datatype").values()
    ]
    renderer = axioms._Renderer()
    unquoted = {
        str(datatype.getIRI())
        for datatype in built_in
        if not renderer.manchester(factory.getOWLLiteral("1", datatype)).startswith('"')
    }
    prop, individual = factory.getOWLDataProperty(create(f"{NS}p")), factory.getOWLNamedIndividual(create(f"{NS}i"))
    rendered = []
    for datatype in [*built_in, factory.getOWLDatatype(create(f"{NS}Adulthood"))]:
        literal = factory.getOWLLiteral(HOSTILE, datatype)
        [axiom] = axioms.render_axioms([factory.getOWLDataPropertyAssertionAxiom(prop, individual, literal)])
        rendered.append((str(datatype.getIRI()), axiom))
    return unquoted, rendered


def check_quoted(axiom: Axiom) -> None:
    """Check that no text of `HOSTILE` is outside a literal in an axiom's text, nor its IRI in the axiom's IRIs."""
    for segment in manchester.segments(axiom.text):
        if segment.kind != SegmentKind.LITERAL:
            assert not any(mark in segment.text for mark in ('"', "\u202e", "example.org/B")), axiom.text
            assert segment.kind != SegmentKind.KEYWORD or segment.text != "and", axiom.text
    assert "http://example.org/B" not in axiom.iris


def test_unquoted_literals() -> None:
    """Check that, for every datatype, an ill-typed literal can't escape its quotes.

    Either the renderer quotes it, or the axiom is shown in functional syntax, which quotes every literal (DESIGN.md
    §2.8).
    """
    unquoted, rendered = asyncio.run(owlapi.run_java(hostile_assertions))
    assert unquoted == set(XSD_LEXICAL_FORMS)
    for _, axiom in rendered:
        check_quoted(axiom)
    # The data factory reads an ill-typed boolean as false, which is valid.
    functional = {datatype for datatype, axiom in rendered if axiom.syntax == Syntax.FUNCTIONAL}
    assert functional == {f"{XSD}integer", f"{XSD}decimal", f"{XSD}float"}
    for datatype, axiom in rendered:
        if datatype in functional:
            assert axiom.iris == (f"{NS}p", f"{NS}i", datatype)  # the datatype isn't in `<…>` in functional syntax
            assert axiom.operands == (f"<{NS}i>", f"<{NS}p>", axiom.operands[2])
            assert axiom.operands[2].startswith('"5 \\"')


def test_ill_typed_from_ntriples() -> None:
    """Check ill-typed literals read from N-Triples, in an assertion and nested in a class expression."""
    escaped = HOSTILE.replace('"', '\\"')
    ntriples = f"""<{NS}i> <{NS}p> "{escaped}"^^<{XSD}integer> .
{DATA_PROPERTY}<{NS}A> <http://www.w3.org/2000/01/rdf-schema#subClassOf> _:r .
_:r <{RDF}type> <http://www.w3.org/2002/07/owl#Restriction> .
_:r <http://www.w3.org/2002/07/owl#onProperty> <{NS}p> .
_:r <http://www.w3.org/2002/07/owl#hasValue> "1 \\" and <http://example.org/B>"^^<{XSD}decimal> .
""".encode()
    found = [
        *asyncio.run(owlapi.run_java(axioms.entity_axioms, ntriples, f"{NS}i")),
        *asyncio.run(owlapi.run_java(axioms.entity_axioms, ntriples, f"{NS}A")),
    ]
    assert [axiom.kind for axiom in found] == ["DataPropertyAssertion", "SubClassOf"]
    for axiom in found:
        assert axiom.syntax == Syntax.FUNCTIONAL
        check_quoted(axiom)


@pytest.mark.parametrize(
    ("datatype", "lexical", "shown"),
    [("integer", "-5", "-5"), ("decimal", "1.5", "1.5"), ("float", "2.0", "2.0f"), ("boolean", "true", "true")],
)
def test_valid_unquoted(datatype: str, lexical: str, shown: str) -> None:
    """Check that valid literals the renderer writes unquoted stay in Manchester syntax."""
    ntriples = f'<{NS}i> <{NS}p> "{lexical}"^^<{XSD}{datatype}> .\n{DATA_PROPERTY}'.encode()
    [axiom] = asyncio.run(owlapi.run_java(axioms.entity_axioms, ntriples, f"{NS}i"))
    assert (axiom.syntax, axiom.text) == (Syntax.MANCHESTER, f"<{NS}i> <{NS}p> {shown}")
    assert axiom.iris == (f"{NS}i", f"{NS}p", f"{XSD}{datatype}")


@pytest.mark.fuseki
@pytest.mark.anyio
@pytest.mark.parametrize("clause", ["", UNION_FROM], ids=["default-graph", "union"])
async def test_fragment_matches_whole(
    fuseki_url: str, dataset: str, rendered: tuple[list[Axiom], dict[str, list[Axiom]]], clause: str
) -> None:
    """Check that, for every entity, the axioms rendered from its fragment are those from the whole ontology."""
    _, closures = rendered
    assert sum(map(len, closures.values())) > 90
    async with httpx2.AsyncClient(base_url=fuseki_url) as http:
        sparql = FusekiClient(http)
        for iri, expected in closures.items():
            fragment = await axioms.fetch_fragment(sparql, dataset, iri, clause)
            assert await owlapi.run_java(axioms.entity_axioms, fragment, iri) == expected, iri


ENTITIES = [
    (f"{NS}Heart", EntityType.CLASS),
    (f"{NS}Organ", EntityType.CLASS),
    (f"{NS}Animal", EntityType.CLASS),
    (f"{NS}Plant", EntityType.CLASS),
    (f"{NS}Person", EntityType.CLASS),
    (f"{NS}Dog", EntityType.CLASS),
    (f"{NS}partOf", EntityType.OBJECT_PROPERTY),
    (f"{NS}hasPart", EntityType.OBJECT_PROPERTY),
    (f"{NS}nickname", EntityType.DATA_PROPERTY),
]
NAMES = {
    f"{NS}Heart": ('<b>Heart</b> & "soul"', None),
    f"{NS}Organ": ("organ", None),
    f"{NS}partOf": ("part of", None),
}


@pytest.mark.fuseki
@pytest.mark.usefixtures("data_dir")
def test_page(fuseki_url: str, dataset: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check an entity page's axioms: entity links with escaped names, keywords, other IRIs and literals as text."""
    monkeypatch.setattr(browl.overview, "dataset_name", lambda generation: dataset)
    app = make_app(FUSEKI_URL=fuseki_url)
    with TestClient(app) as client:
        app.state.jobs.register(Stage.PRE_CACHING, derived_stage(ENTITIES, names=NAMES))
        client.portal.call(app.state.jobs.start)
        client.portal.call(app.state.jobs.wait)
        page = client.get("/_browl/hash", params={"path": "/axioms", "fragment": "Heart"}).text
        person = client.get("/_browl/hash", params={"path": "/axioms", "fragment": "Person", "panel": "1"}).text
    heart_link = '<a href="/axioms#Heart"><bdi>&lt;b&gt;Heart&lt;/b&gt; &amp; &#34;soul&#34;</bdi></a>'
    keyword = '<span class="keyword">{}</span>'.format
    assert (
        f'<li dir="ltr">{heart_link} {keyword("SubClassOf")} <a href="/axioms#Organ"><bdi>organ</bdi></a></li>' in page
    )
    assert (
        f'<li dir="ltr"><a href="/axioms#hasPart"><bdi>http://example.org/axioms#hasPart</bdi></a> {keyword("some")} '
        f'{heart_link} {keyword("SubClassOf")} <code dir="ltr">&lt;{NS}Vertebrate&gt;</code></li>'
    ) in page
    assert f"{keyword('value')} <bdi>&#34;see &lt;{NS}Dog&gt; &amp; \\&#34;friends\\&#34;&#34;@en</bdi>" in person
    assert "The entity's annotations will be shown here." in page
    assert f'{keyword("some")} <code dir="ltr">xsd:integer</code>[&gt;= ' in person  # a built-in datatype
    code = '<code dir="ltr">&lt;{}&gt;</code>'.format
    assert f"{keyword('HasKey')} {code(f'{NS}hasSSN')}, {code(f'{NS}ssn')}</li>" in person
