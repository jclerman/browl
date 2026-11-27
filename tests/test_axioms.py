"""Tests for entities' axioms: fetched fragments, closures, nodes against the OWLAPI's rendering, the entity page."""

import asyncio
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import httpx2
import jpype
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

import browl.overview
from browl import axioms, frames, nesting, owl, owlapi
from browl.derived import EntityType
from browl.fuseki import UNION_FROM, FusekiClient
from browl.jobs import Stage
from browl.owl import TokenKind
from browl.rdf import OWL, RDF, RDFS, XSD, XSD_LEXICAL_FORMS
from tests.helpers import boxed, derived_stage, fuseki_dataset, make_app

pytestmark = pytest.mark.usefixtures("jvm")

ONTOLOGY = Path(__file__).parent / "data" / "axioms.ofn"
NS = "http://example.org/axioms#"


def kinds() -> set[str]:
    """Return every kind of OWL 2 axiom but declarations and rules, by its OWLAPI interface.

    The OWLAPI's own names for two kinds are misspelt (`IrrefexiveObjectProperty`, `AnnotationPropertyRangeOf`).
    """
    return {
        str(each.getActualClass().getSimpleName()).removeprefix("OWL").removesuffix("Axiom")
        for each in jpype.JClass("org.semanticweb.owlapi.model.AxiomType").AXIOM_TYPES
        if str(each.getName()) not in {"Declaration", "Rule"}
    }


@pytest.fixture(scope="module")
def ntriples() -> bytes:
    """Return the test ontology as N-Triples, as the OWLAPI writes it: the RDF that browl's store would hold."""
    manager = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").createOWLOntologyManager()
    ontology = manager.loadOntologyFromOntologyDocument(jpype.JClass("java.io.File")(str(ONTOLOGY)))
    out = jpype.JClass("java.io.ByteArrayOutputStream")()
    manager.saveOntology(ontology, jpype.JClass("org.semanticweb.owlapi.formats.NTriplesDocumentFormat")(), out)
    return bytes(out.toByteArray())


def whole(ntriples: bytes) -> tuple[list[owl.Axiom], dict[str, list[owl.Axiom]]]:
    """Parse the whole ontology; return all its axioms but declarations and annotation assertions, and each entity's."""
    with owlapi.ntriples_ontology(ntriples) as ontology:
        every = [
            axiom.getAxiomWithoutAnnotations()
            for axiom in ontology.getAxioms()
            if str(axiom.getAxiomType().getName()) not in axioms._LEFT_OUT
        ]
        iris = {str(entity.getIRI()) for entity in ontology.getSignature()}
        return axioms.convert_axioms(every), {
            iri: axioms.convert_axioms(axioms.entity_closure(ontology, iri)) for iri in iris
        }


@pytest.fixture(scope="module")
def rendered(ntriples: bytes) -> tuple[list[owl.Axiom], dict[str, list[owl.Axiom]]]:
    """Return `whole` for the test ontology."""
    return asyncio.run(owlapi.run_java(whole, ntriples))


@pytest.fixture(scope="module")
def dataset(fuseki_url: str, ntriples: bytes) -> Iterator[str]:
    """Yield the name of an in-memory Fuseki dataset holding the test ontology, removed afterwards."""
    with fuseki_dataset(fuseki_url, {None: ntriples}) as name:
        yield name


SPLIT = (f"<{NS}Dog> ", f"<{NS}Cat> ", f"<{NS}likes> ")
"""The subjects of the triples `split_dataset` puts in a named graph: their declarations, punning and axioms, such as
`DisjointClasses(:Dog :Cat)`. Other entities' fragments, such as `:loves`'s, need `:likes`'s type, which only the
types query fetches. Blank nodes are left in the default graph, since each load names its own."""


@pytest.fixture(scope="module")
def split_dataset(fuseki_url: str, ntriples: bytes) -> Iterator[str]:
    """Yield the name of a dataset holding the test ontology, `SPLIT`'s triples in a named graph, as an import's."""
    lines = ntriples.decode().splitlines(keepends=True)
    named = [line for line in lines if line.startswith(SPLIT)]
    rest = [line for line in lines if not line.startswith(SPLIT)]
    assert len(named) >= 6
    graphs = {None: "".join(rest).encode(), "http://example.org/imported": "".join(named).encode()}
    with fuseki_dataset(fuseki_url, graphs) as name:
        yield name


def reference_renderer() -> Any:
    """Return the OWLAPI's Manchester renderer, writing every entity as its full IRI: the reference for `owl`'s text."""
    prefixes = jpype.JClass("org.semanticweb.owlapi.util.DefaultPrefixManager")()
    prefixes.clear()
    renderer = jpype.JClass(
        "org.semanticweb.owlapi.manchestersyntax.renderer.ManchesterOWLSyntaxOWLObjectRendererImpl"
    )()
    renderer.setShortFormProvider(prefixes)
    return renderer


def normalized(text: str) -> str:
    """Return text with single spaces, and none before a comma or closing parenthesis.

    The OWLAPI breaks lines, and writes `{a , b}` and `(p Self )`.
    """
    return " ".join(text.split()).replace(" , ", ", ").replace(" )", ")")


DIFFERENCES = {
    "HasKey": "commas between the properties, where OWLAPI 4 writes nothing (`<Person> HasKey <hasSSN>, <ssn>`)",
    "DatatypeDefinition": "`<Adulthood> EquivalentTo …`, where OWLAPI 4 renders nothing",
    "Rule": "functional syntax (`browl.owl.OtherAxiom`)",
}
"""The kinds of axiom whose text differs from the OWLAPI's by design, besides spacing (`normalized`) and ill-typed
literals (`test_unquoted_literals`)."""


def expected_text(kind: str, axiom: Any, renderer: Any) -> str:
    """Return the text browl should give an axiom: the OWLAPI's, with the difference `DIFFERENCES` lists for its kind.

    `comparable` makes up for `HasKey`'s commas.
    """
    if kind == "DatatypeDefinition":
        return f"{renderer.render(axiom.getDatatype())} EquivalentTo {renderer.render(axiom.getDataRange())}"
    if kind == "Rule":
        return str(axiom)
    return str(renderer.render(axiom))


def unlabelled(text: str) -> str:
    """Return text `normalized`, without anonymous individuals' labels, which `browl.axioms` gives anew."""
    return re.sub(r"_:[\w-]+", "_:", normalized(text))


def comparable(kind: str, text: str) -> str:
    """Return text `unlabelled`, and for a `HasKey` axiom without spaces or commas, which the OWLAPI writes in part."""
    text = unlabelled(text)
    return re.sub(r"[\s,]", "", text) if kind == "HasKey" else text


class Reference(NamedTuple):
    """An axiom compared with the OWLAPI's rendering.

    Attributes:
        kind: The axiom's kind.
        owlapi: The OWLAPI's text.
        expected: The text browl should give it (`expected_text`).
        node: The axiom as a node.
    """

    kind: str
    owlapi: str
    expected: str
    node: owl.AnyAxiom


def synthetic() -> list[Any]:
    """Return axioms the test ontology lacks, built with the OWLAPI's data factory, to compare with its text.

    They have nestings of expressions, inverse properties, anonymous individuals, and literals of many datatypes.
    """
    factory = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").getOWLDataFactory()
    create = jpype.JClass("org.semanticweb.owlapi.model.IRI").create
    datatypes = jpype.JClass("org.semanticweb.owlapi.vocab.OWL2Datatype")
    facets = jpype.JClass("org.semanticweb.owlapi.vocab.OWLFacet")

    def items(*values: Any) -> Any:
        found = jpype.JClass("java.util.HashSet")()
        for value in values:
            found.add(value)
        return found

    a, b, c = (factory.getOWLClass(create(f"{NS}{name}")) for name in "ABC")
    p, q = (factory.getOWLObjectProperty(create(f"{NS}{name}")) for name in "pq")
    inverse = factory.getOWLObjectInverseOf(p)
    d, e = (factory.getOWLDataProperty(create(f"{NS}{name}")) for name in "de")
    i, j = (factory.getOWLNamedIndividual(create(f"{NS}{name}")) for name in "ij")
    anonymous = factory.getOWLAnonymousIndividual("x1")
    integer, string = datatypes.XSD_INTEGER.getDatatype(factory), datatypes.XSD_STRING.getDatatype(factory)
    custom = factory.getOWLDatatype(create(f"{NS}DT"))
    three = factory.getOWLLiteral(jpype.JInt(3))
    union, complement = factory.getOWLObjectUnionOf(items(a, b)), factory.getOWLObjectComplementOf(c)
    data_union = factory.getOWLDataUnionOf(items(integer, string))
    classes = [
        factory.getOWLObjectSomeValuesFrom(p, factory.getOWLObjectOneOf(items(i, j))),
        factory.getOWLObjectSomeValuesFrom(p, complement),
        factory.getOWLObjectSomeValuesFrom(p, factory.getOWLObjectSomeValuesFrom(q, a)),
        factory.getOWLObjectSomeValuesFrom(p, factory.getOWLObjectHasSelf(q)),
        factory.getOWLObjectComplementOf(factory.getOWLObjectIntersectionOf(items(a, b))),
        factory.getOWLObjectComplementOf(complement),
        factory.getOWLObjectIntersectionOf(items(a, union, complement, factory.getOWLObjectAllValuesFrom(q, c))),
        factory.getOWLObjectUnionOf(items(a, factory.getOWLObjectIntersectionOf(items(b, c)))),
        factory.getOWLObjectIntersectionOf(items(a, factory.getOWLObjectHasValue(p, anonymous))),
        factory.getOWLObjectMinCardinality(2, p),
        factory.getOWLObjectMinCardinality(2, inverse, union),
        factory.getOWLObjectMaxCardinality(1, p, factory.getOWLObjectOneOf(items(i, anonymous))),
        factory.getOWLObjectHasSelf(inverse),
        factory.getOWLDataSomeValuesFrom(d, factory.getOWLDataOneOf(items(three, factory.getOWLLiteral("x")))),
        factory.getOWLDataSomeValuesFrom(d, factory.getOWLDataComplementOf(integer)),
        factory.getOWLDataSomeValuesFrom(
            d, factory.getOWLDataIntersectionOf(items(integer, factory.getOWLDataComplementOf(custom)))
        ),
        factory.getOWLDataSomeValuesFrom(d, factory.getOWLDataIntersectionOf(items(custom, data_union))),
        factory.getOWLDataAllValuesFrom(
            d, factory.getOWLDataComplementOf(factory.getOWLDataIntersectionOf(items(integer, custom)))
        ),
        factory.getOWLDataSomeValuesFrom(
            d, factory.getOWLDataComplementOf(factory.getOWLDatatypeRestriction(integer, facets.MIN_INCLUSIVE, three))
        ),
        factory.getOWLDataMinCardinality(2, d),
        factory.getOWLDataSomeValuesFrom(d, factory.getOWLDataComplementOf(factory.getOWLDataOneOf(items(three)))),
        factory.getOWLDataExactCardinality(2, d, data_union),
        factory.getOWLDataMaxCardinality(2, d, factory.getOWLDatatypeRestriction(integer, facets.MAX_EXCLUSIVE, three)),
        *(factory.getOWLDataHasValue(d, literal) for literal in literals(factory, custom)),
    ]
    restrictions = [
        items(
            factory.getOWLFacetRestriction(facets.MIN_INCLUSIVE, three),
            factory.getOWLFacetRestriction(facets.MAX_EXCLUSIVE, factory.getOWLLiteral(jpype.JInt(9))),
        ),
        items(
            factory.getOWLFacetRestriction(facets.PATTERN, factory.getOWLLiteral("a.*")),
            factory.getOWLFacetRestriction(facets.LENGTH, three),
        ),
        items(*(factory.getOWLFacetRestriction(facet, three) for facet in facets.values())),
    ]
    ranges = [
        data_union,
        factory.getOWLDataComplementOf(integer),
        factory.getOWLDataOneOf(items(factory.getOWLLiteral("a"))),
        *(factory.getOWLDatatypeRestriction(integer, each) for each in restrictions),
    ]
    note = factory.getOWLAnnotationProperty(create(f"{NS}note"))
    nested = factory.getOWLAnnotation(note, create(f"{NS}V"), items(factory.getOWLAnnotation(note, anonymous)))
    annotations = items(nested, factory.getOWLAnnotation(note, factory.getOWLLiteral("why")))
    variable = factory.getSWRLVariable(create("urn:swrl#x"))
    rule = factory.getSWRLRule(
        items(factory.getSWRLClassAtom(a, variable)), items(factory.getSWRLClassAtom(b, variable))
    )
    return [
        *(factory.getOWLSubClassOfAxiom(a, each) for each in classes),
        *(factory.getOWLDataPropertyRangeAxiom(d, each) for each in ranges),
        factory.getOWLSubClassOfAxiom(union, complement),
        factory.getOWLEquivalentClassesAxiom(items(union, complement)),
        factory.getOWLEquivalentClassesAxiom(items(a, union, complement)),
        factory.getOWLDisjointClassesAxiom(items(a, b, complement)),
        factory.getOWLDisjointUnionAxiom(a, items(b, complement)),
        factory.getOWLHasKeyAxiom(union, items(p, inverse, d)),
        factory.getOWLSubObjectPropertyOfAxiom(inverse, q),
        factory.getOWLSubPropertyChainOfAxiom(jpype.JClass("java.util.Arrays").asList([inverse, q, p]), q),
        factory.getOWLObjectPropertyDomainAxiom(p, union),
        factory.getOWLObjectPropertyRangeAxiom(inverse, complement),
        factory.getOWLInverseObjectPropertiesAxiom(inverse, q),
        factory.getOWLEquivalentObjectPropertiesAxiom(items(p, q, inverse)),
        factory.getOWLSymmetricObjectPropertyAxiom(inverse),
        factory.getOWLEquivalentDataPropertiesAxiom(items(d, e)),
        factory.getOWLDataPropertyDomainAxiom(d, complement),
        factory.getOWLClassAssertionAxiom(union, anonymous),
        factory.getOWLObjectPropertyAssertionAxiom(inverse, i, j),
        factory.getOWLNegativeObjectPropertyAssertionAxiom(inverse, i, anonymous),
        factory.getOWLDataPropertyAssertionAxiom(d, anonymous, factory.getOWLLiteral("hi", "fr")),
        factory.getOWLSameIndividualAxiom(items(i, j, anonymous)),
        factory.getOWLDifferentIndividualsAxiom(items(i, anonymous)),
        factory.getOWLDatatypeDefinitionAxiom(custom, data_union),
        factory.getOWLSubAnnotationPropertyOfAxiom(factory.getRDFSLabel(), factory.getRDFSComment()),
        factory.getOWLAnnotationAssertionAxiom(note, anonymous, factory.getOWLLiteral("anonymous")),
        factory.getOWLAnnotationAssertionAxiom(note, create(f"{NS}A"), anonymous),
        factory.getOWLSubClassOfAxiom(a, b, annotations),  # annotations nested, an anonymous individual among them
        rule,
    ]


def literals(factory: Any, custom: Any) -> list[Any]:
    """Return literals of every built-in datatype, of a custom one, with language tags and with escapes."""
    datatypes = jpype.JClass("org.semanticweb.owlapi.vocab.OWL2Datatype")
    found = [factory.getOWLLiteral("1", each.getDatatype(factory)) for each in datatypes.values()]
    return [
        *found,
        factory.getOWLLiteral("x", custom),
        factory.getOWLLiteral("x", "en"),
        factory.getOWLLiteral("", ""),
        factory.getOWLLiteral('a "b" \\c\nd\te'),
        factory.getOWLLiteral("1.50", datatypes.XSD_DECIMAL.getDatatype(factory)),
        factory.getOWLLiteral("-1.5E3", datatypes.XSD_FLOAT.getDatatype(factory)),
    ]


def compared() -> list[Reference]:
    """Return every axiom of the test ontology, and the synthetic ones, each with the OWLAPI's text and as a node."""
    manager = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").createOWLOntologyManager()
    ontology = manager.loadOntologyFromOntologyDocument(jpype.JClass("java.io.File")(str(ONTOLOGY)))
    every = [
        axiom.getAxiomWithoutAnnotations()
        for axiom in ontology.getAxioms()
        if str(axiom.getAxiomType().getName()) != "Declaration"
    ]
    renderer = reference_renderer()
    return [
        Reference(kind, str(renderer.render(axiom)), expected_text(kind, axiom, renderer), node)
        for axiom in [*every, *synthetic()]
        for kind in [str(axiom.getAxiomType().getName())]
        for [node] in [axioms.convert_axioms([axiom])]
    ]


@pytest.fixture(scope="module")
def references() -> list[Reference]:
    """Return `compared`, run on the Java thread."""
    return asyncio.run(owlapi.run_java(compared))


def test_matches_owlapi(references: list[Reference]) -> None:
    """Check that each axiom's text is the OWLAPI's, but for the differences `DIFFERENCES` lists, and only those."""
    assert len(references) > 150
    unexpected = [
        (normalized(each.node.manchester), normalized(each.expected))
        for each in references
        if comparable(each.kind, each.node.manchester) != comparable(each.kind, each.expected)
    ]
    assert unexpected == []
    differ = {each.kind for each in references if unlabelled(each.node.manchester) != unlabelled(each.owlapi)}
    assert differ == set(DIFFERENCES)


def walk(node: owl.Node) -> Iterator[owl.Node]:
    """Yield a node and every node in it."""
    yield node
    for name in type(node).model_fields:
        value = getattr(node, name)
        for each in value if isinstance(value, tuple) else (value,):
            if isinstance(each, owl.Node):
                yield from walk(each)


def test_every_node_type(references: list[Reference]) -> None:
    """Check that the axioms compared with the OWLAPI's use every node type, in its union, and the OWLAPI's keywords."""
    nodes = [each for reference in references for each in walk(reference.node)]
    assert {type(each) for each in nodes} == set(owl.NODE_TYPES)
    syntax = jpype.JClass("org.semanticweb.owlapi.manchestersyntax.parser.ManchesterOWLSyntax")
    keywords = {str(keyword.keyword()) for keyword in syntax.values()}
    used = {
        token.text
        for reference in references
        for token in reference.node.manchester_tokens()
        if token.kind == TokenKind.KEYWORD
    }
    assert {word.removesuffix(":") for word in used} <= {word.removesuffix(":") for word in keywords}
    adapter = TypeAdapter(list[owl.AnyAxiom])
    every = [reference.node for reference in references]
    assert adapter.validate_json(adapter.dump_json(every)) == every


OWN_NODE_TYPES = {
    "Entity",
    "IRI",
    "AnonymousIndividual",
    "Literal",
    "ObjectInverseOf",
    "FacetRestriction",
    "Annotation",
    "OtherAxiom",
}
"""Node types with no counterpart among the OWLAPI's kinds of axiom, class expression and data range."""


def test_node_types_match_owlapi() -> None:
    """Check that `NodeType` names the OWLAPI's kinds of axiom, class expression and data range, and browl's own.

    A class or a datatype is an `Entity`.
    """
    model = "org.semanticweb.owlapi.model."
    expressions = {
        str(each.getName())
        for name in ("ClassExpressionType", "DataRangeType")
        for each in jpype.JClass(model + name).values()
    }
    assert set(owl.NodeType) == kinds() | (expressions - {"Class", "Datatype"}) | OWN_NODE_TYPES


def test_facets() -> None:
    """Check `browl.FACETS` against the OWLAPI's facets and their symbols."""
    facets = jpype.JClass("org.semanticweb.owlapi.vocab.OWLFacet")
    assert {str(facet.getIRI()): str(facet.getSymbolicForm()) for facet in facets.values()} == owl.FACETS


def test_every_kind(rendered: tuple[list[owl.Axiom], dict[str, list[owl.Axiom]]]) -> None:
    """Check that the test ontology has every kind of axiom, each on one line."""
    every, _ = rendered
    # A rule's kind, though no `NodeType`'s; `whole` leaves out annotation assertions, as an Overview does.
    assert {axiom.kind for axiom in every} == (kinds() - {"AnnotationAssertion"}) | {"Rule"}
    for axiom in every:
        assert axiom.manchester
        assert "\n" not in axiom.manchester


def test_json_round_trip(rendered: tuple[list[owl.Axiom], dict[str, list[owl.Axiom]]]) -> None:
    """Check that every axiom of the test ontology round-trips through JSON."""
    every, _ = rendered
    adapter = TypeAdapter(list[owl.AnyAxiom])
    assert adapter.validate_json(adapter.dump_json(every)) == every


def test_closure(rendered: tuple[list[owl.Axiom], dict[str, list[owl.Axiom]]]) -> None:
    """Check some entities' axioms: n-ary ones, general class axioms, property chains, keys, annotations stripped."""
    _, closures = rendered

    def texts(name: str) -> list[str]:
        return [axiom.manchester.replace(NS, "") for axiom in closures[f"{NS}{name}"]]

    general = "<partOf> only (<Dog> or (not (<Cat>))) SubClassOf <Pet>"  # through unionOf and complementOf
    assert texts("Cat") == ["<Cat> DisjointWith <Dog>", "<Cat> SubClassOf <Animal>", general]  # annotation stripped
    # Punned, so it has a class's axioms and an individual's.
    assert texts("Dog") == ["<Dog> Type <Animal>", "<Cat> DisjointWith <Dog>", "<Dog> SubClassOf <Animal>", general]
    assert general in texts("Pet")
    assert "<hasPart> min 2 <Liver> SubClassOf <Vertebrate>" in texts("Liver")  # through onClass
    assert "<hasPart> some <Heart> SubClassOf <Vertebrate>" in texts("Heart")  # annotated
    assert "DisjointClasses: <Animal>, <Fungus>, <Plant>" in texts("Plant")
    assert "<Organ> and (<partOf> some <Plant>) SubClassOf <http://www.w3.org/2002/07/owl#Nothing>" in texts("Plant")
    assert "<Person> HasKey <hasSSN>, <ssn>" in texts("Person")  # an object and a data property
    assert texts("hasUncle") == ["<hasParent> o <hasBrother> SubPropertyOf: <hasUncle>"]
    assert "not (<alice> <hates> <rex>)" in texts("alice")
    assert texts("partOf") == ["<hasPart> InverseOf <partOf>", "Transitive: <partOf>"]  # not the GCAs it's in
    assert texts("Adulthood") == ["<Adulthood> EquivalentTo <http://www.w3.org/2001/XMLSchema#integer>[>= 18]"]


def test_roles(rendered: tuple[list[owl.Axiom], dict[str, list[owl.Axiom]]]) -> None:
    """Check axioms' roles: what each field holds, for the kinds whose functional syntax orders them otherwise."""
    _, closures = rendered

    def found[T: owl.Axiom](name: str, kind: type[T]) -> list[T]:
        return [axiom for axiom in closures[f"{NS}{name}"] if isinstance(axiom, kind)]

    def short(node: owl.Node) -> str:
        return owl.manchester_text(node.manchester_tokens()).replace(NS, "")

    [assertion] = found("rex", owl.ClassAssertion)
    assert (short(assertion.individual), short(assertion.class_)) == ("<rex>", "<Dog>")
    [likes] = [axiom for axiom in found("alice", owl.ObjectPropertyAssertion) if "likes" in axiom.manchester]
    assert (short(likes.subject), short(likes.property), short(likes.object)) == ("<alice>", "<likes>", "<rex>")
    [hates] = found("alice", owl.NegativeObjectPropertyAssertion)
    assert (short(hates.subject), short(hates.property), short(hates.object)) == ("<alice>", "<hates>", "<rex>")
    [age] = found("bob", owl.NegativeDataPropertyAssertion)
    assert (short(age.subject), short(age.property), age.object.lexical) == ("<bob>", "<age>", "7")
    [inverse] = found("partOf", owl.InverseObjectProperties)
    assert {short(inverse.first), short(inverse.second)} == {"<partOf>", "<hasPart>"}
    [union] = found("LivingThing", owl.DisjointUnion)
    assert (short(union.class_), [short(each) for each in union.members]) == ("<LivingThing>", ["<Animal>", "<Plant>"])
    [chain] = found("hasAncestor", owl.SubPropertyChainOf)
    assert ([short(each) for each in chain.chain], short(chain.super)) == (
        ["<hasParent>", "<hasParent>"],
        "<hasAncestor>",
    )
    [key] = found("Person", owl.HasKey)
    assert [short(key.class_), *map(short, key.object_properties), *map(short, key.data_properties)] == [
        "<Person>",
        "<hasSSN>",
        "<ssn>",
    ]
    [definition] = found("Adulthood", owl.DatatypeDefinition)
    assert (short(definition.datatype), definition.iris()) == ("<Adulthood>", (f"{NS}Adulthood", f"{XSD}integer"))
    [nickname] = [axiom for axiom in closures[f"{NS}Person"] if "nickname" in axiom.manchester]
    # Not the IRI in the literal; the literal's datatype, though it isn't in the text.
    assert nickname.iris() == (f"{NS}Person", f"{NS}nickname", f"{RDF}PlainLiteral")


HOSTILE = '5 " <http://example.org/B> \u202e and'
"""A literal's text that would forge an IRI, a keyword, a bidi override and a quote if written unquoted."""
DATA_PROPERTY = f"<{NS}p> <{RDF}type> <http://www.w3.org/2002/07/owl#DatatypeProperty> .\n"


def hostile_assertions() -> tuple[set[str], list[tuple[str, owl.Axiom]]]:
    """Return the built-in datatypes whose literals the OWLAPI's renderer writes unquoted, and `HOSTILE` in each.

    The built-in datatypes are OWLAPI's `OWL2Datatype`; `HOSTILE` is in a data property assertion, for each of them and
    one that isn't built in.
    """
    factory = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").getOWLDataFactory()
    create = jpype.JClass("org.semanticweb.owlapi.model.IRI").create
    built_in = [
        each.getDatatype(factory) for each in jpype.JClass("org.semanticweb.owlapi.vocab.OWL2Datatype").values()
    ]
    renderer = reference_renderer()
    unquoted = {
        str(datatype.getIRI())
        for datatype in built_in
        if not str(renderer.render(factory.getOWLLiteral("1", datatype))).startswith('"')
    }
    prop, individual = factory.getOWLDataProperty(create(f"{NS}p")), factory.getOWLNamedIndividual(create(f"{NS}i"))
    converted = []
    for datatype in [*built_in, factory.getOWLDatatype(create(f"{NS}Adulthood"))]:
        literal = factory.getOWLLiteral(HOSTILE, datatype)
        [axiom] = axioms.convert_axioms([factory.getOWLDataPropertyAssertionAxiom(prop, individual, literal)])
        converted.append((str(datatype.getIRI()), axiom))
    return unquoted, converted


def check_quoted(axiom: owl.Axiom) -> None:
    """Check that no text of `HOSTILE` is in an axiom's tokens but its literal's, nor its IRI in the axiom's IRIs."""
    for token in axiom.manchester_tokens():
        if token.kind != TokenKind.LITERAL:
            assert not any(mark in token.text for mark in ('"', "\u202e", "example.org/B")), axiom.manchester
            assert token.kind != TokenKind.KEYWORD or token.text != "and", axiom.manchester
        else:
            assert token.text.startswith('"'), axiom.manchester
    assert "http://example.org/B" not in axiom.iris()


def test_unquoted_literals() -> None:
    """Check that, for every datatype, an ill-typed literal is one token, quoted (DESIGN.md §2.8).

    The OWLAPI writes some unquoted.
    """
    unquoted, converted = asyncio.run(owlapi.run_java(hostile_assertions))
    assert unquoted == set(XSD_LEXICAL_FORMS)
    for datatype, axiom in converted:
        if datatype == f"{XSD}boolean":
            continue  # the data factory reads an ill-typed boolean as false, which is valid
        check_quoted(axiom)
        assert axiom.iris() == (f"{NS}i", f"{NS}p", datatype)


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
        check_quoted(axiom)
    assert found[1].manchester == f'<{NS}A> SubClassOf <{NS}p> value "1 \\" and <http://example.org/B>"^^<{XSD}decimal>'


@pytest.mark.parametrize(
    ("datatype", "lexical", "shown"),
    [("integer", "-5", "-5"), ("decimal", "1.5", "1.5"), ("float", "2.0", "2.0f"), ("boolean", "true", "true")],
)
def test_valid_unquoted(datatype: str, lexical: str, shown: str) -> None:
    """Check that valid literals the OWLAPI writes unquoted are unquoted."""
    ntriples = f'<{NS}i> <{NS}p> "{lexical}"^^<{XSD}{datatype}> .\n{DATA_PROPERTY}'.encode()
    [axiom] = asyncio.run(owlapi.run_java(axioms.entity_axioms, ntriples, f"{NS}i"))
    assert axiom.manchester == f"<{NS}i> <{NS}p> {shown}"
    assert axiom.iris() == (f"{NS}i", f"{NS}p", f"{XSD}{datatype}")


def relative_iri() -> list[owl.AnyAxiom]:
    """Convert an axiom whose class has a relative IRI, which the OWLAPI's data factory allows, and one without."""
    factory = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").getOWLDataFactory()
    create = jpype.JClass("org.semanticweb.owlapi.model.IRI").create
    relative, a = factory.getOWLClass(create("relative")), factory.getOWLClass(create(f"{NS}A"))
    return axioms.convert_axioms(
        [
            factory.getOWLSubClassOfAxiom(relative, factory.getOWLThing()),
            factory.getOWLSubClassOfAxiom(a, factory.getOWLThing()),
        ]
    )


def test_invalid_iri(caplog: pytest.LogCaptureFixture) -> None:
    """Check that an axiom with an IRI its node refuses is logged and shown in functional syntax, not the others."""
    with caplog.at_level(logging.WARNING, logger="browl.axioms"):
        found = asyncio.run(owlapi.run_java(relative_iri))
    assert [(type(node), node.kind, node.manchester) for node in found] == [
        (owl.SubClassOf, "SubClassOf", f"<{NS}A> SubClassOf <http://www.w3.org/2002/07/owl#Thing>"),
        (owl.OtherAxiom, "SubClassOf", "SubClassOf(<relative> owl:Thing)"),
    ]
    assert [record.getMessage() for record in caplog.records] == [
        "Showing an axiom in functional syntax, as browl can't hold it as nodes: not a valid IRI: 'relative'"
    ]


def nested(depth: int, intersections: bool) -> list[owl.AnyAxiom]:
    """Convert `A SubClassOf` an expression whose nodes nest `depth` deep, the axiom counting as 1, ending in `B`.

    With `intersections`, each level is `A and (…)`, whose operands are a list in JSON, so two levels there; else
    `not (…)`.
    """
    factory = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").getOWLDataFactory()
    create = jpype.JClass("org.semanticweb.owlapi.model.IRI").create
    a, expression = factory.getOWLClass(create(f"{NS}A")), factory.getOWLClass(create(f"{NS}B"))
    for _ in range(depth - 2):
        if intersections:
            expression = factory.getOWLObjectIntersectionOf(a, expression)
        else:
            expression = factory.getOWLObjectComplementOf(expression)
    return axioms.convert_axioms([factory.getOWLSubClassOfAxiom(a, expression)])


@pytest.mark.parametrize("intersections", [False, True])
def test_depth(intersections: bool) -> None:
    """Check that an axiom `MAX_DEPTH` deep converts and round-trips through JSON; one deeper is a `FragmentError`."""
    deepest = asyncio.run(owlapi.run_java(nested, nesting.MAX_DEPTH, intersections))
    assert deepest[0].iris() == (f"{NS}A", f"{NS}B")
    adapter = TypeAdapter(list[owl.AnyAxiom])
    assert adapter.validate_json(adapter.dump_json(deepest)) == deepest
    with pytest.raises(owlapi.FragmentError, match=f"^an axiom's nodes nest more than {nesting.MAX_DEPTH} deep$"):
        asyncio.run(owlapi.run_java(nested, nesting.MAX_DEPTH + 1, intersections))


def test_deep_fragment() -> None:
    """Check that a fragment nesting far deeper than `MAX_DEPTH`, as a fetch can return, fails as a `FragmentError`."""
    lines = [f"<{NS}{name}> <{RDF}type> <{OWL}Class> ." for name in "AB"]
    lines.append(f"<{NS}A> <{RDFS}subClassOf> _:b0 .")
    for level in range(1_000):
        lines.append(f"_:b{level} <{RDF}type> <{OWL}Class> .")
        lines.append(f"_:b{level} <{OWL}complementOf> {f'_:b{level + 1}' if level < 999 else f'<{NS}B>'} .")
    with pytest.raises(owlapi.FragmentError, match="nest more than"):
        asyncio.run(owlapi.run_java(axioms.entity_axioms, "\n".join(lines).encode(), f"{NS}A"))


def unconvertible() -> list[str]:
    """Return the errors from converting a class, which isn't an axiom, and a SWRL variable, which no node covers."""
    factory = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").getOWLDataFactory()
    create = jpype.JClass("org.semanticweb.owlapi.model.IRI").create
    errors = []
    for value in (factory.getOWLClass(create(f"{NS}A")), factory.getSWRLVariable(create("urn:swrl#x"))):
        with pytest.raises(owlapi.FragmentError) as error:
            axioms.convert_axioms([value])
        errors.append(str(error.value))
    return errors


def test_unconvertible() -> None:
    """Check that an object that isn't an axiom, or that no node type covers, fails as a `FragmentError`."""
    not_axiom, no_node = asyncio.run(owlapi.run_java(unconvertible))
    assert not_axiom == "not an axiom: Entity"
    assert re.fullmatch(r"browl has no node type for [\w.]+SWRLVariable\w*", no_node)


ANONYMOUS = "".join(
    [
        f"<{NS}i> <{RDF}type> <{OWL}NamedIndividual> .\n",
        *(f"<{NS}{name}> <{RDF}type> <{OWL}ObjectProperty> .\n" for name in "pqr"),
        f"<{NS}i> <{NS}r> _:y .\n<{NS}i> <{NS}q> _:x .\n<{NS}i> <{NS}p> _:x .\n",
    ]
).encode()
"""An individual related to two anonymous individuals, one of them twice."""


def test_anonymous_labels() -> None:
    """Check that anonymous individuals are labelled in order of appearance, the same each time a fragment is parsed."""
    for _ in range(2):
        found = asyncio.run(owlapi.run_java(axioms.entity_axioms, ANONYMOUS, f"{NS}i"))
        assert [axiom.manchester.replace(NS, "") for axiom in found] == ["<i> <p> _:b1", "<i> <q> _:b1", "<i> <r> _:b2"]


def test_too_many(ntriples: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that an entity with more than `MAX_AXIOMS` axioms isn't rendered."""
    monkeypatch.setattr(axioms, "MAX_AXIOMS", 2)
    with pytest.raises(axioms.TooManyAxiomsError, match="the entity has 3 axioms, more than 2"):
        asyncio.run(owlapi.run_java(axioms.entity_axioms, ntriples, f"{NS}Cat"))
    monkeypatch.setattr(axioms, "MAX_AXIOMS", 3)
    assert len(asyncio.run(owlapi.run_java(axioms.entity_axioms, ntriples, f"{NS}Cat"))) == 3


def annotated_twice() -> list[owl.Axiom]:
    """Return the axioms of `:Cat` in an ontology with `SubClassOf(:Cat :Animal)` with an annotation and without."""
    manager = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").createOWLOntologyManager()
    factory = manager.getOWLDataFactory()
    create = jpype.JClass("org.semanticweb.owlapi.model.IRI").create
    cat, animal = factory.getOWLClass(create(f"{NS}Cat")), factory.getOWLClass(create(f"{NS}Animal"))
    note = factory.getOWLAnnotation(factory.getOWLAnnotationProperty(create(f"{NS}note")), factory.getOWLLiteral("n"))
    plain = factory.getOWLSubClassOfAxiom(cat, animal)
    annotated = factory.getOWLSubClassOfAxiom(cat, animal, jpype.JClass("java.util.Collections").singleton(note))
    ontology = manager.createOntology()
    for axiom in (plain, annotated):
        manager.addAxiom(ontology, axiom)
    assert ontology.getAxiomCount() == 2
    return axioms.convert_axioms(axioms.entity_closure(ontology, f"{NS}Cat"))


def test_annotations_collapse() -> None:
    """Check that an axiom stated with an annotation and without is the entity's twice, and one line of its frame."""
    found = asyncio.run(owlapi.run_java(annotated_twice))
    assert [(axiom.manchester, len(axiom.annotations)) for axiom in found] == [
        (f"<{NS}Cat> SubClassOf <{NS}Animal>", 0),
        (f"<{NS}Cat> SubClassOf <{NS}Animal>", 1),
    ]
    [group] = frames.frame_groups(found, f"{NS}Cat", [], str)
    [line] = group.lines
    assert line.axiom == found[0] and line.annotations == found[1].annotations


ANNOTATED = "".join(
    [
        f"<{NS}A> <{RDF}type> <{OWL}Class> .\n<{NS}B> <{RDF}type> <{OWL}Class> .\n",
        f"<{NS}note> <{RDF}type> <{OWL}AnnotationProperty> .\n<{NS}A> <{RDFS}subClassOf> <{NS}B> .\n",
        f"_:ax <{RDF}type> <{OWL}Axiom> .\n_:ax <{OWL}annotatedSource> <{NS}A> .\n",
        f"_:ax <{OWL}annotatedProperty> <{RDFS}subClassOf> .\n_:ax <{OWL}annotatedTarget> <{NS}B> .\n",
        f'_:ax <{NS}note> "why" .\n_:ax <{NS}note> <{NS}V> .\n_:ax <{NS}note> _:x .\n',
        f"_:an <{RDF}type> <{OWL}Annotation> .\n_:an <{OWL}annotatedSource> _:ax .\n",
        f"_:an <{OWL}annotatedProperty> <{NS}note> .\n_:an <{OWL}annotatedTarget> <{NS}V> .\n",
        f'_:an <{NS}note> "nested" .\n',
    ]
).encode()
"""`A SubClassOf B`, annotated with a literal, an IRI (itself annotated) and an anonymous individual."""


def converted_annotated(ntriples: bytes) -> list[owl.AnyAxiom]:
    """Return the axioms but declarations that N-Triples parse as, with their annotations."""
    with owlapi.ntriples_ontology(ntriples) as ontology:
        found = [axiom for axiom in ontology.getAxioms() if str(axiom.getAxiomType().getName()) != "Declaration"]
    return axioms.convert_axioms(found)


def test_axiom_annotations() -> None:
    """Check that an axiom keeps its annotations, sorted, nested ones under theirs, anonymous individuals relabelled."""
    for _ in range(2):
        [axiom] = asyncio.run(owlapi.run_java(converted_annotated, ANNOTATED))
        assert axiom.manchester == f"<{NS}A> SubClassOf <{NS}B>"
        shown = [
            (owl.manchester_text(each.manchester_tokens()).replace(NS, ""), len(each.annotations))
            for each in axiom.annotations
        ]
        assert shown == [("<note> <V>", 1), ("<note> _:b1", 0), ('<note> "why"', 0)]  # in the OWLAPI's order
        [inner] = axiom.annotations[0].annotations
        assert owl.manchester_text(inner.manchester_tokens()).replace(NS, "") == '<note> "nested"'
        assert axiom.without_annotations() == owl.SubClassOf(sub=axiom.sub, super=axiom.super)
        assert f"{NS}V" in axiom.iris()


def relative_annotation() -> list[owl.AnyAxiom]:
    """Convert `A SubClassOf B` annotated with a relative IRI, which the data factory makes but a node refuses."""
    factory = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").getOWLDataFactory()
    create = jpype.JClass("org.semanticweb.owlapi.model.IRI").create
    note = factory.getOWLAnnotation(factory.getOWLAnnotationProperty(create(f"{NS}note")), create("relative"))
    a, b = (factory.getOWLClass(create(f"{NS}{name}")) for name in "AB")
    return axioms.convert_axioms(
        [factory.getOWLSubClassOfAxiom(a, b, jpype.JClass("java.util.Collections").singleton(note))]
    )


def test_unconvertible_annotations(caplog: pytest.LogCaptureFixture) -> None:
    """Check that an axiom whose annotations a node refuses is shown without them, and that it's logged."""
    with caplog.at_level(logging.WARNING, logger="browl.axioms"):
        [axiom] = asyncio.run(owlapi.run_java(relative_annotation))
    assert (axiom.manchester, axiom.annotations) == (f"<{NS}A> SubClassOf <{NS}B>", ())
    assert "Not showing an axiom's annotations, as browl can't hold them as nodes" in caplog.text


@pytest.mark.anyio
async def test_empty_fragment(fuseki_url: str, dataset: str) -> None:
    """Check that an IRI the store doesn't hold has an empty fragment, and no axioms."""
    async with httpx2.AsyncClient(base_url=fuseki_url) as http:
        fragment = await axioms.fetch_fragment(FusekiClient(http), dataset, f"{NS}Nobody")
    assert fragment == axioms.Fragment(b"")
    assert await owlapi.run_java(axioms.entity_axioms, fragment.ntriples, f"{NS}Nobody") == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("fixture", "clause"), [("dataset", ""), ("split_dataset", UNION_FROM)], ids=["default-graph", "union"]
)
async def test_fragment_matches_whole(
    fuseki_url: str,
    rendered: tuple[list[owl.Axiom], dict[str, list[owl.Axiom]]],
    request: pytest.FixtureRequest,
    fixture: str,
    clause: str,
) -> None:
    """Check that, for every entity, the axioms rendered from its fragment are those from the whole ontology.

    With imports, the dataset clause makes the queries see the named graphs too, where some of the triples are.
    """
    dataset = request.getfixturevalue(fixture)
    _, closures = rendered
    assert sum(map(len, closures.values())) > 90
    async with httpx2.AsyncClient(base_url=fuseki_url) as http:
        sparql = FusekiClient(http)
        for iri, expected in closures.items():
            fragment = await axioms.fetch_fragment(sparql, dataset, iri, clause)
            assert fragment.annotations_left_out is None
            assert await owlapi.run_java(axioms.entity_axioms, fragment.ntriples, iri) == expected, iri


ENTITIES = [
    (f"{NS}Heart", EntityType.CLASS),
    (f"{NS}Organ", EntityType.CLASS),
    (f"{NS}Animal", EntityType.CLASS),
    (f"{NS}Plant", EntityType.CLASS),
    (f"{NS}Person", EntityType.CLASS),
    (f"{NS}Dog", EntityType.CLASS),
    (f"{NS}Adult", EntityType.CLASS),
    (f"{NS}Mineral", EntityType.CLASS),
    (f"{NS}partOf", EntityType.OBJECT_PROPERTY),
    (f"{NS}hasPart", EntityType.OBJECT_PROPERTY),
    (f"{NS}hasUncle", EntityType.OBJECT_PROPERTY),
    (f"{NS}nickname", EntityType.DATA_PROPERTY),
    (f"{NS}comment", EntityType.ANNOTATION_PROPERTY),
]
NAMES = {
    f"{NS}Heart": ('<b>Heart</b> & "soul"', None),
    f"{NS}Organ": ("organ", None),
    f"{NS}partOf": ("part of", None),
}


def test_page(data_dir: Path, fuseki_url: str, dataset: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check an entity page's axioms: entity links with escaped names, keywords, other IRIs and literals as text."""
    monkeypatch.setattr(browl.overview, "dataset_name", lambda generation: dataset)
    app = make_app(FUSEKI_URL=fuseki_url)
    with TestClient(app) as client:
        app.state.jobs.register(Stage.PRE_CACHING, derived_stage(ENTITIES, names=NAMES))
        client.portal.call(app.state.jobs.start)
        client.portal.call(app.state.jobs.wait)
        page = client.get("/_browl/hash", params={"path": "/axioms", "fragment": "Heart"}).text
        person = client.get("/_browl/hash", params={"path": "/axioms", "fragment": "Person", "panel": "1"}).text
        comment = client.get("/_browl/hash", params={"path": "/axioms", "fragment": "comment", "panel": "1"}).text
        uncle = client.get("/_browl/hash", params={"path": "/axioms", "fragment": "hasUncle", "panel": "1"}).text
        animal = client.get("/_browl/hash", params={"path": "/axioms", "fragment": "Animal", "panel": "1"}).text
        adult = client.get("/_browl/hash", params={"path": "/axioms", "fragment": "Adult", "panel": "1"}).text
        part_of = client.get("/_browl/hash", params={"path": "/axioms", "fragment": "partOf", "panel": "1"}).text
        mineral = client.get("/_browl/hash", params={"path": "/axioms", "fragment": "Mineral", "panel": "1"}).text
    heart_link = '<a href="/axioms#Heart"><bdi>&lt;b&gt;Heart&lt;/b&gt; &amp; &#34;soul&#34;</bdi></a>'
    keyword = '<span class="keyword">{}</span>'.format
    assert re.search(r'<dt>SubClass Of</dt>\s*<dd dir="ltr"><a href="/axioms#Organ"><bdi>organ</bdi></a></dd>', page)
    assert (
        f'<dd dir="ltr"><a href="/axioms#hasPart"><bdi>http://example.org/axioms#hasPart</bdi></a> {keyword("some")} '
        f'<mark>{heart_link}</mark> {keyword("SubClassOf")} <code dir="ltr">&lt;{NS}Vertebrate&gt;</code>'
        f"{boxed(f'<code dir="ltr">{NS}note</code>: <bdi class="value">general</bdi>')}</dd>"
    ) in page  # a general class axiom, shown whole, the entity highlighted
    assert f"{keyword('value')} <bdi>&#34;see &lt;{NS}Dog&gt; &amp; \\&#34;friends\\&#34;&#34;@en</bdi>" in person
    label = re.escape('<bdi class="value">&lt;b&gt;Heart&lt;/b&gt; &amp; &#34;soul&#34;</bdi>')
    assert re.search(rf'<dt><code dir="ltr">rdfs:label</code></dt>\s*<dd>{label}</dd>', page)  # the annotations panel
    assert f'{keyword("some")} <code dir="ltr">xsd:integer</code>[&gt;= ' in person  # a built-in datatype
    code = '<code dir="ltr">&lt;{}&gt;</code>'.format
    keys = f"{re.escape(code(f'{NS}hasSSN'))}, {re.escape(code(f'{NS}ssn'))}"
    assert re.search(rf'<dt>Keys</dt>\s*<dd dir="ltr">{keys}</dd>', person)
    assert '<p class="note">None.</p>' in comment  # it has no logical axioms
    # A literal of a custom datatype, which has no page.
    assert f'<bdi>&#34;B&#34;</bdi>^^<code dir="ltr">&lt;{NS}Grade&gt;</code>' in person
    chain = f'<code dir="ltr">&lt;{NS}hasParent&gt;</code> {keyword("o")} <code dir="ltr">&lt;{NS}hasBrother&gt;</code>'
    assert re.search(rf'<dt>SuperProperty Of \(Chain\)</dt>\s*<dd dir="ltr">{re.escape(chain)}</dd>', uncle)
    assert "SubClass Of" not in comment and "None." not in page
    # Sections from n-ary and symmetric axioms, as the OWLAPI converts them.
    assert '<dl class="frame">' in animal
    disjoint = f'<a href="/axioms#Plant"><bdi>{NS}Plant</bdi></a>'
    n_ary = boxed(
        f'<code dir="ltr">{NS}note</code>: <bdi class="value">n-ary</bdi>',
        f'<code dir="ltr">{NS}remark</code>: <a href="/axioms#Mineral"><bdi>{NS}Mineral</bdi></a>',
    )
    disjoint_line = f'<dd dir="ltr">{code(f"{NS}Fungus")}, {disjoint}{n_ary}</dd>'
    assert re.search(rf"<dt>Disjoint With</dt>\s*{re.escape(disjoint_line)}", animal)
    # The store holds a three-way equivalence as pairwise triples, so only one is Adult's; the other member comes
    # from the equivalence group, which this test's derived database doesn't have.
    living = f'<dd dir="ltr">{code(f"{NS}LivingThing")}</dd>'
    assert re.search(rf"<dt>Equivalent To</dt>\s*{living}\s*</dl>", adult)
    has_part = f'<a href="/axioms#hasPart"><bdi>{NS}hasPart</bdi></a>'
    assert re.search(rf'<dt>Characteristics</dt>\s*<dd dir="ltr">{keyword("Transitive")}</dd>', part_of)
    assert re.search(rf'<dt>Inverse Of</dt>\s*<dd dir="ltr">{has_part}</dd>', part_of)
    # The entity is highlighted in its axioms, but not in its annotations: Mineral's own `rdfs:seeAlso Mineral`.
    annotations, axioms = mineral.split('<section class="axioms panel"')
    mineral_link = f'<a href="/axioms#Mineral"><bdi>{NS}Mineral</bdi></a>'
    assert mineral_link in annotations and "<mark>" not in annotations
    assert axioms.count(f"<mark>{mineral_link}</mark>") == 1  # in a general class axiom
    assert "<mark>" not in part_of


INDIVIDUALS = [
    *ENTITIES,
    *((f"{NS}{name}", EntityType.NAMED_INDIVIDUAL) for name in ("alice", "bob", "carol", "dave", "rex", "tom", "Dog")),
    *((f"{NS}{name}", EntityType.OBJECT_PROPERTY) for name in ("likes", "hasChild", "hates", "knows")),
    *((f"{NS}{name}", EntityType.DATA_PROPERTY) for name in ("age", "name", "weight")),
]


def test_individual_page(data_dir: Path, fuseki_url: str, dataset: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check an individual's frame: its sections, each property's assertions under its name, and a punned IRI's."""
    monkeypatch.setattr(browl.overview, "dataset_name", lambda generation: dataset)
    app = make_app(FUSEKI_URL=fuseki_url)
    with TestClient(app) as client:
        app.state.jobs.register(Stage.PRE_CACHING, derived_stage(INDIVIDUALS))
        client.portal.call(app.state.jobs.start)
        client.portal.call(app.state.jobs.wait)
        pages = {
            name: client.get("/_browl/hash", params={"path": "/axioms", "fragment": name, "panel": "1"}).text
            for name in ("alice", "bob", "carol", "Dog")
        }
    link = '<a href="/axioms#{0}"><bdi>http://example.org/axioms#{0}</bdi></a>'.format
    keyword = '<span class="keyword">{}</span>'.format

    def frame(name: str) -> str:
        page = pages[name]
        return re.sub(r">\s*\n\s*<", "><", page[page.index('<dl class="frame">') :].split("</dl>")[0].strip())

    code = '<code dir="ltr">&lt;http://example.org/axioms#{}&gt;</code>'.format
    heading, by = '<dt class="frame-heading">{}</dt>'.format, '<dt class="frame-property">{}</dt>'.format
    value = '<dd class="frame-value" dir="ltr">'  # a property's, which a narrow window indents under it
    assert frame("alice") == "".join(
        [
            '<dl class="frame">',
            f'<dt>Types</dt><dd dir="ltr">{link("likes")} {keyword("some")} {code("Pet")}</dd>',
            f'<dt>Different From</dt><dd dir="ltr">{link("bob")}</dd><dd dir="ltr">{link("carol")}, {link("tom")}</dd>',
            heading("Object property assertions"),
            f"{by(link('hasChild'))}{value}{link('bob')}</dd>",
            f"{by(link('likes'))}{value}{link('rex')}</dd>",
            heading("Data property assertions"),
            f"{by(link('age'))}{value}<bdi>42</bdi></dd>",
            f'<dt>Negative property assertions</dt><dd dir="ltr">{link("hates")} {link("rex")}</dd>',
        ]
    )
    bob = frame("bob")
    assert f"{by(f'<code dir="ltr">{NS}loves</code>')}{value}_:b1</dd>" in bob  # an anonymous object
    assert f'<dt>Negative property assertions</dt><dd dir="ltr">{link("age")} <bdi>7</bdi></dd>' in bob
    carol = frame("carol")
    assert f'<dt>Same As</dt><dd dir="ltr">{link("dave")}</dd>' in carol
    assert f'{by(link("knows"))}{value}{link("dave")} <span class="axiom-annotations">' in carol
    # Dog is a class and an individual: its sections are under a heading for each, the class's first, as in the header.
    dog = re.sub(r">\s*\n\s*<", "><", pages["Dog"])
    kind = '<div class="frame-kind" role="group" aria-labelledby="axioms-{0}"><h3 id="axioms-{0}">{1}</h3>'.format
    class_start = dog.index(kind("class", "Class"))
    individual_start = dog.index(kind("namedindividual", "Named individual"))
    assert class_start < individual_start
    as_class, as_individual = dog[class_start:individual_start], dog[individual_start:]
    assert re.findall(r"<dt>([^<]*)</dt>", as_class) == ["SubClass Of", "Disjoint With", "General class axioms"]
    assert re.findall(r"<dt>([^<]*)</dt>", as_individual) == ["Types"]
    assert f'<dt>Types</dt><dd dir="ltr">{link("Animal")}</dd></dl></div>' in as_individual
    # An individual alone has no heading over its sections.
    assert all("frame-kind" not in pages[name] for name in ("alice", "bob", "carol"))
    assert "Other axioms" not in "".join(pages.values())
