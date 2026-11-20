"""An entity's axioms, fetched from Fuseki as a fragment, parsed by the OWLAPI, as a typed tree (DESIGN.md §2.8).

A page fetches the triples its entity's axioms need (`fetch_fragment`), the OWLAPI parses them as a small ontology, and
`entity_axioms` converts the entity's axioms into nodes (`browl.owl`), which render themselves in Manchester syntax.
Templates make the HTML (`templates/axioms.html`).

An entity's axioms are those the OWLAPI lists for it (`getAxioms(entity)`), and for a class its `HasKey` axioms and
the general class axioms that mention it, for an object property the property chains that imply it, without their
annotations. That's every logical axiom about the entity, plus an annotation property's domain, range and
super-properties; not its annotation assertions, nor its usage in other entities' axioms (P-27).
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, cast

import jpype

from browl import owl
from browl.fuseki import FusekiClient
from browl.owl import EntityKind
from browl.owlapi import FragmentError, check_java_thread, ntriples_ontology
from browl.precache.core import PREFIXES, sparql_file
from browl.rdf import IRI, ntriples_iris

_AXIOMS = PREFIXES + sparql_file("entity-axioms.rq")
_SYMMETRIC = PREFIXES + sparql_file("entity-symmetric.rq")
_TYPES = PREFIXES + sparql_file("entity-types.rq")
_ENTITY = "$entity"
_WHERE = "\nWHERE {"
_NO_IRIS = "VALUES ?iri { }"

MAX_FRAGMENT_IRIS = 5_000
"""The most IRIs an entity's fragment may mention before its types are fetched (`fetch_fragment`): more, and the page
says the entity has too many axioms to show, rather than fetch, parse, convert and link them all (DESIGN.md §2.8)."""

MAX_AXIOMS = 1_000
"""The most axioms an entity may have for its page to show them (`entity_axioms`), as for `MAX_FRAGMENT_IRIS`."""


class TooManyAxiomsError(Exception):
    """An entity's axioms are too many, or mention too many IRIs, to show on its page (DESIGN.md §2.8)."""


def _filled(query: str, dataset_clause: str, parts: dict[str, str]) -> str:
    """Return a query with its dataset clause before `WHERE`, and each of its placeholders replaced.

    The query files are valid SPARQL as they are, as `tests/test_sparql.py` checks: a placeholder is a variable, such
    as `$entity`, or an empty `VALUES` block.
    """
    if _WHERE not in query:
        raise ValueError(f"no {_WHERE.strip()!r} at the start of a line in the query")
    filled = query.replace(_WHERE, f"\n{dataset_clause}{_WHERE}", 1)
    for placeholder, value in parts.items():
        if placeholder not in filled:
            raise ValueError(f"no {placeholder} in the query")
        filled = filled.replace(placeholder, value)
    return filled


async def fetch_fragment(sparql: FusekiClient, dataset: str, iri: str, dataset_clause: str = "") -> bytes:
    """Return, as N-Triples, the triples from which the OWLAPI can parse an entity's axioms (DESIGN.md §2.8).

    They are the entity's triples with their blank-node closure, the axioms rooted at blank nodes that list or mention
    it (`sparql/entity-axioms.rq`), the symmetric axioms stated on other entities (`sparql/entity-symmetric.rq`), and
    the types of every IRI those mention (`sparql/entity-types.rq`).

    Args:
        sparql: The Fuseki client.
        dataset: The dataset that serves the ontology.
        iri: The entity's IRI.
        dataset_clause: The dataset clause that makes the queries see the imports (`browl.fuseki.dataset_clause`).

    Raises:
        SparqlError: A query failed.
        RDFSyntaxError: The IRI isn't valid.
        TooManyAxiomsError: The triples mention more than `MAX_FRAGMENT_IRIS` IRIs.
    """
    entity = {_ENTITY: f"<{IRI(iri)}>"}
    try:
        # A task group, unlike `gather`, cancels the other query when one fails.
        async with asyncio.TaskGroup() as group:
            own = group.create_task(sparql.construct(dataset, _filled(_AXIOMS, dataset_clause, entity)))
            symmetric = group.create_task(sparql.construct(dataset, _filled(_SYMMETRIC, dataset_clause, entity)))
    except ExceptionGroup as errors:
        raise errors.exceptions[0]  # noqa: B904 -- the query's own error, with the group as its context
    triples = own.result() + symmetric.result()
    mentioned = ntriples_iris(triples)
    if len(mentioned) > MAX_FRAGMENT_IRIS:
        raise TooManyAxiomsError(f"the fragment mentions {len(mentioned)} IRIs, more than {MAX_FRAGMENT_IRIS}")
    if not mentioned:
        return triples
    values = f"VALUES ?iri {{ {' '.join(f'<{each}>' for each in sorted(mentioned))} }}"
    return triples + await sparql.construct(dataset, _filled(_TYPES, dataset_clause, {_NO_IRIS: values}))


type _Build = Callable[[_Converter, Any], owl.Node]
"""Makes a node from an OWLAPI object, converting its parts with the converter."""


def _fields(node_type: Callable[..., owl.Node], **getters: str) -> _Build:
    """Return a build that makes a node from the values of an OWLAPI object's getters, each converted (`_value`)."""

    def build(converter: _Converter, owl_object: Any) -> owl.Node:
        return node_type(**{field: converter.value(getattr(owl_object, getter)()) for field, getter in getters.items()})

    return build


def _entity(kind: EntityKind) -> _Build:
    return lambda converter, entity: owl.Entity(kind=kind, iri=str(entity.getIRI()))


def _literal(converter: _Converter, literal: Any) -> owl.Node:
    language = str(literal.getLang()) if literal.hasLang() else None
    return owl.Literal(
        lexical=str(literal.getLiteral()), datatype=str(literal.getDatatype().getIRI()), language=language
    )


def _facet(converter: _Converter, restriction: Any) -> owl.Node:
    return owl.FacetRestriction(
        facet=str(restriction.getFacet().getIRI()), value=cast(owl.Literal, converter(restriction.getFacetValue()))
    )


def _annotation_domain(converter: _Converter, axiom: Any) -> owl.Node:
    return owl.AnnotationPropertyDomain(
        property=cast(owl.Entity, converter(axiom.getProperty())), domain=owl.IRIReference(iri=str(axiom.getDomain()))
    )


def _annotation_range(converter: _Converter, axiom: Any) -> owl.Node:
    return owl.AnnotationPropertyRange(
        property=cast(owl.Entity, converter(axiom.getProperty())), range=owl.IRIReference(iri=str(axiom.getRange()))
    )


def _other(converter: _Converter, axiom: Any) -> owl.Node:
    return owl.OtherAxiom(axiom_type=str(axiom.getAxiomType().getName()), functional=str(axiom))


_PROPERTY_FILLER = {"property": "getProperty", "filler": "getFiller"}
_CARDINALITY = {**_PROPERTY_FILLER, "cardinality": "getCardinality"}
_SUB_SUPER = {"sub": "getSubProperty", "super": "getSuperProperty"}
_PROPERTY = {"property": "getProperty"}
_ASSERTION = {"subject": "getSubject", "property": "getProperty", "object": "getObject"}

_BUILDS: tuple[tuple[str, _Build], ...] = (
    # Each OWLAPI interface (in `org.semanticweb.owlapi.model`), and how to make its node. Sets are sorted, as the
    # OWLAPI's renderer sorts them; lists, such as a property chain, keep their order.
    ("OWLClass", _entity(EntityKind.CLASS)),
    ("OWLDatatype", _entity(EntityKind.DATATYPE)),
    ("OWLObjectProperty", _entity(EntityKind.OBJECT_PROPERTY)),
    ("OWLDataProperty", _entity(EntityKind.DATA_PROPERTY)),
    ("OWLAnnotationProperty", _entity(EntityKind.ANNOTATION_PROPERTY)),
    ("OWLNamedIndividual", _entity(EntityKind.NAMED_INDIVIDUAL)),
    ("OWLAnonymousIndividual", _fields(owl.AnonymousIndividual, id="toStringID")),
    ("OWLLiteral", _literal),
    ("OWLFacetRestriction", _facet),
    ("OWLObjectInverseOf", _fields(owl.ObjectInverseOf, property="getInverse")),
    # Class expressions.
    ("OWLObjectIntersectionOf", _fields(owl.ObjectIntersectionOf, operands="getOperands")),
    ("OWLObjectUnionOf", _fields(owl.ObjectUnionOf, operands="getOperands")),
    ("OWLObjectComplementOf", _fields(owl.ObjectComplementOf, operand="getOperand")),
    ("OWLObjectOneOf", _fields(owl.ObjectOneOf, items="getIndividuals")),
    ("OWLObjectSomeValuesFrom", _fields(owl.ObjectSomeValuesFrom, **_PROPERTY_FILLER)),
    ("OWLObjectAllValuesFrom", _fields(owl.ObjectAllValuesFrom, **_PROPERTY_FILLER)),
    ("OWLObjectHasValue", _fields(owl.ObjectHasValue, **_PROPERTY_FILLER)),
    ("OWLObjectHasSelf", _fields(owl.ObjectHasSelf, **_PROPERTY)),
    ("OWLObjectMinCardinality", _fields(owl.ObjectMinCardinality, **_CARDINALITY)),
    ("OWLObjectMaxCardinality", _fields(owl.ObjectMaxCardinality, **_CARDINALITY)),
    ("OWLObjectExactCardinality", _fields(owl.ObjectExactCardinality, **_CARDINALITY)),
    ("OWLDataSomeValuesFrom", _fields(owl.DataSomeValuesFrom, **_PROPERTY_FILLER)),
    ("OWLDataAllValuesFrom", _fields(owl.DataAllValuesFrom, **_PROPERTY_FILLER)),
    ("OWLDataHasValue", _fields(owl.DataHasValue, **_PROPERTY_FILLER)),
    ("OWLDataMinCardinality", _fields(owl.DataMinCardinality, **_CARDINALITY)),
    ("OWLDataMaxCardinality", _fields(owl.DataMaxCardinality, **_CARDINALITY)),
    ("OWLDataExactCardinality", _fields(owl.DataExactCardinality, **_CARDINALITY)),
    # Data ranges.
    ("OWLDataIntersectionOf", _fields(owl.DataIntersectionOf, operands="getOperands")),
    ("OWLDataUnionOf", _fields(owl.DataUnionOf, operands="getOperands")),
    ("OWLDataComplementOf", _fields(owl.DataComplementOf, operand="getDataRange")),
    ("OWLDataOneOf", _fields(owl.DataOneOf, items="getValues")),
    (
        "OWLDatatypeRestriction",
        _fields(owl.DatatypeRestriction, datatype="getDatatype", restrictions="getFacetRestrictions"),
    ),
    # Axioms.
    ("OWLSubClassOfAxiom", _fields(owl.SubClassOf, sub="getSubClass", super="getSuperClass")),
    ("OWLEquivalentClassesAxiom", _fields(owl.EquivalentClasses, members="getClassExpressions")),
    ("OWLDisjointClassesAxiom", _fields(owl.DisjointClasses, members="getClassExpressions")),
    ("OWLDisjointUnionAxiom", _fields(owl.DisjointUnion, class_="getOWLClass", members="getClassExpressions")),
    (
        "OWLHasKeyAxiom",
        _fields(
            owl.HasKey,
            class_="getClassExpression",
            object_properties="getObjectPropertyExpressions",
            data_properties="getDataPropertyExpressions",
        ),
    ),
    ("OWLSubPropertyChainOfAxiom", _fields(owl.SubPropertyChainOf, chain="getPropertyChain", super="getSuperProperty")),
    ("OWLSubObjectPropertyOfAxiom", _fields(owl.SubObjectPropertyOf, **_SUB_SUPER)),
    ("OWLEquivalentObjectPropertiesAxiom", _fields(owl.EquivalentObjectProperties, members="getProperties")),
    ("OWLDisjointObjectPropertiesAxiom", _fields(owl.DisjointObjectProperties, members="getProperties")),
    (
        "OWLInverseObjectPropertiesAxiom",
        _fields(owl.InverseObjectProperties, first="getFirstProperty", second="getSecondProperty"),
    ),
    ("OWLObjectPropertyDomainAxiom", _fields(owl.ObjectPropertyDomain, property="getProperty", domain="getDomain")),
    ("OWLObjectPropertyRangeAxiom", _fields(owl.ObjectPropertyRange, property="getProperty", range="getRange")),
    ("OWLFunctionalObjectPropertyAxiom", _fields(owl.FunctionalObjectProperty, **_PROPERTY)),
    ("OWLInverseFunctionalObjectPropertyAxiom", _fields(owl.InverseFunctionalObjectProperty, **_PROPERTY)),
    ("OWLReflexiveObjectPropertyAxiom", _fields(owl.ReflexiveObjectProperty, **_PROPERTY)),
    ("OWLIrreflexiveObjectPropertyAxiom", _fields(owl.IrreflexiveObjectProperty, **_PROPERTY)),
    ("OWLSymmetricObjectPropertyAxiom", _fields(owl.SymmetricObjectProperty, **_PROPERTY)),
    ("OWLAsymmetricObjectPropertyAxiom", _fields(owl.AsymmetricObjectProperty, **_PROPERTY)),
    ("OWLTransitiveObjectPropertyAxiom", _fields(owl.TransitiveObjectProperty, **_PROPERTY)),
    ("OWLSubDataPropertyOfAxiom", _fields(owl.SubDataPropertyOf, **_SUB_SUPER)),
    ("OWLEquivalentDataPropertiesAxiom", _fields(owl.EquivalentDataProperties, members="getProperties")),
    ("OWLDisjointDataPropertiesAxiom", _fields(owl.DisjointDataProperties, members="getProperties")),
    ("OWLDataPropertyDomainAxiom", _fields(owl.DataPropertyDomain, property="getProperty", domain="getDomain")),
    ("OWLDataPropertyRangeAxiom", _fields(owl.DataPropertyRange, property="getProperty", range="getRange")),
    ("OWLFunctionalDataPropertyAxiom", _fields(owl.FunctionalDataProperty, **_PROPERTY)),
    ("OWLDatatypeDefinitionAxiom", _fields(owl.DatatypeDefinition, datatype="getDatatype", range="getDataRange")),
    ("OWLClassAssertionAxiom", _fields(owl.ClassAssertion, individual="getIndividual", class_="getClassExpression")),
    ("OWLObjectPropertyAssertionAxiom", _fields(owl.ObjectPropertyAssertion, **_ASSERTION)),
    ("OWLNegativeObjectPropertyAssertionAxiom", _fields(owl.NegativeObjectPropertyAssertion, **_ASSERTION)),
    ("OWLDataPropertyAssertionAxiom", _fields(owl.DataPropertyAssertion, **_ASSERTION)),
    ("OWLNegativeDataPropertyAssertionAxiom", _fields(owl.NegativeDataPropertyAssertion, **_ASSERTION)),
    ("OWLSameIndividualAxiom", _fields(owl.SameIndividual, members="getIndividuals")),
    ("OWLDifferentIndividualsAxiom", _fields(owl.DifferentIndividuals, members="getIndividuals")),
    ("OWLSubAnnotationPropertyOfAxiom", _fields(owl.SubAnnotationPropertyOf, **_SUB_SUPER)),
    ("OWLAnnotationPropertyDomainAxiom", _annotation_domain),
    ("OWLAnnotationPropertyRangeAxiom", _annotation_range),
    ("OWLAxiom", _other),  # any other, such as a SWRL rule
)


@dataclass(frozen=True, slots=True)
class _Java:
    """The Java classes and constants this module uses."""

    ArrayList: Any
    AxiomType: Any
    Collection: Any
    Collections: Any
    IRI: Any
    List: Any
    excluded: Any  # Imports.EXCLUDED: a fragment's imports are never loaded
    builds: tuple[tuple[Any, _Build], ...]  # `_BUILDS`, with the interfaces


_LEFT_OUT = frozenset({"Declaration", "AnnotationAssertion"})
"""The kinds of axiom about an entity that aren't its axioms: its declarations and annotations."""


@functools.cache
def _java() -> _Java:
    """Look the classes up once, on first use; the JVM must be running."""
    model = "org.semanticweb.owlapi.model"
    return _Java(
        ArrayList=jpype.JClass("java.util.ArrayList"),
        AxiomType=jpype.JClass(f"{model}.AxiomType"),
        Collection=jpype.JClass("java.util.Collection"),
        Collections=jpype.JClass("java.util.Collections"),
        IRI=jpype.JClass(f"{model}.IRI"),
        List=jpype.JClass("java.util.List"),
        excluded=jpype.JClass(f"{model}.parameters.Imports").EXCLUDED,
        builds=tuple((jpype.JClass(f"{model}.{name}"), build) for name, build in _BUILDS),
    )


def entity_closure(ontology: Any, iri: str) -> list[Any]:
    """Return an entity's axioms in an ontology (the module's docstring says which), without their annotations.

    The same for a fragment fetched for the entity as for the whole ontology: a test checks that. An IRI with several
    types (punning) has the axioms of each.

    Args:
        ontology: The `OWLOntology`.
        iri: The entity's IRI.

    Returns:
        The `OWLAxiom`s, each once.
    """
    check_java_thread()
    java = _java()
    found: dict[Any, None] = {}
    for entity in ontology.getEntitiesInSignature(java.IRI.create(iri)):
        own = list(ontology.getAxioms(entity, java.excluded))
        if entity.isOWLClass():
            own += ontology.getHasKeyAxioms(entity)
            general = ontology.getGeneralClassAxioms()
            own += (axiom for axiom in general if axiom.getClassesInSignature().contains(entity))
        elif entity.isOWLObjectProperty():
            chains = ontology.getAxioms(java.AxiomType.SUB_PROPERTY_CHAIN_OF, java.excluded)
            own += (axiom for axiom in chains if axiom.getSuperProperty().equals(entity))
        for axiom in own:
            if str(axiom.getAxiomType().getName()) not in _LEFT_OUT:
                found.setdefault(axiom.getAxiomWithoutAnnotations(), None)
    return list(found)


class _Converter:
    """Converts OWLAPI objects into nodes (`browl.owl`), choosing each build (`_BUILDS`) by the object's Java class.

    One Python function per kind of node, rather than a Java visitor (`OWLObjectVisitorEx` through `jpype.JProxy`):
    a proxy's overloads of `visit` all reach one Python method, which would have to dispatch on the class anyway, and
    each call from Java back into Python costs more than a lookup by class here.
    """

    def __init__(self) -> None:
        self._java = _java()
        self._builds: dict[type, _Build] = {}

    def __call__(self, owl_object: Any) -> owl.Node:
        """Return an OWLAPI object as a node.

        Raises:
            FragmentError: It's of a kind no node type covers (only axioms have a fallback, `browl.owl.OtherAxiom`).
        """
        build = self._builds.get(type(owl_object))
        if build is None:
            build = self._builds[type(owl_object)] = self._build(owl_object)
        return build(self, owl_object)

    def _build(self, owl_object: Any) -> _Build:
        for interface, build in self._java.builds:
            if isinstance(owl_object, interface):
                return build
        raise FragmentError(f"browl has no node type for {owl_object.getClass().getName()}")

    def value(self, value: Any) -> Any:
        """Return the value of an OWLAPI getter as a node's field: a node, a number, text, or a tuple of nodes."""
        if isinstance(value, int):
            return int(value)
        if isinstance(value, jpype.JString):
            return str(value)
        if isinstance(value, self._java.Collection):
            if not isinstance(value, self._java.List):
                value = self._java.ArrayList(value)
                self._java.Collections.sort(value)
            return tuple(map(self, value))
        return self(value)


@functools.cache
def _converter() -> _Converter:
    """Return the converter, which keeps no state but the builds it has chosen for each Java class."""
    return _Converter()


def convert_axioms(axioms: Iterable[Any]) -> list[owl.Axiom]:
    """Return OWLAPI axioms as nodes (`browl.owl`), sorted by kind and text.

    Call this on the Java thread (`browl.owlapi.run_java`).

    Raises:
        FragmentError: An axiom holds an object of a kind no node type covers.
    """
    check_java_thread()
    converter = _converter()
    nodes: list[owl.Axiom] = []
    for axiom in axioms:
        node = converter(axiom)
        if not isinstance(node, owl.Axiom):
            raise FragmentError(f"not an axiom: {node.type}")
        nodes.append(node)
    return sorted(nodes, key=lambda node: (node.kind, node.text))


def entity_axioms(ntriples: bytes, iri: str) -> list[owl.Axiom]:
    """Parse a fragment fetched for an entity (`fetch_fragment`) and return the entity's axioms, as nodes.

    Call this on the Java thread (`browl.owlapi.run_java`).

    Raises:
        FragmentError: The OWLAPI couldn't parse the fragment, or failed on it with any other Java exception, or an
            axiom holds an object of a kind no node type covers.
        TooManyAxiomsError: The entity has more than `MAX_AXIOMS` axioms.
    """
    try:
        with ntriples_ontology(ntriples) as ontology:
            closure = entity_closure(ontology, iri)
            if len(closure) > MAX_AXIOMS:
                raise TooManyAxiomsError(f"the entity has {len(closure)} axioms, more than {MAX_AXIOMS}")
            return convert_axioms(closure)
    except jpype.JException as error:
        raise FragmentError(f"the OWLAPI failed on the fragment: {error}") from error
