"""An entity's axioms, fetched from Fuseki as a fragment, parsed by the OWLAPI, as a typed tree (DESIGN.md §2.8).

A page fetches the triples its entity's axioms need (`fetch_fragment`), the OWLAPI parses them as a small ontology, and
`entity_axioms` converts the entity's axioms into nodes (`browl.owl`), which render themselves in Manchester syntax.
Templates make the HTML (`templates/axioms.html`).

An entity's axioms are those the OWLAPI lists for it (`getAxioms(entity)`), and for a class its `HasKey` axioms and
the general class axioms that mention it, for an object property the property chains that imply it, with their
annotations (U-35). That's every logical axiom about the entity, plus an annotation property's domain, range and
super-properties; not its annotation assertions, nor its usage in other entities' axioms, which the Usage tab shows
(`browl.usage_tab`, which also converts annotation assertions).
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

import jpype
from pydantic import ValidationError

from browl import owl
from browl.fuseki import CONSTRUCT_MAX_BYTES, FusekiClient, ResultTooLargeError, SparqlError, filled_query
from browl.nesting import MAX_ANNOTATION_DEPTH, MAX_DEPTH, annotation_sources
from browl.owl import EntityKind
from browl.owlapi import FragmentError, check_java_thread, ntriples_ontology
from browl.precache.core import PREFIXES, logged_iri, sparql_file
from browl.rdf import IRI, OWL, RDF, RDFSyntaxError, ntriples_iris
from browl.tasks import caught, task_group

logger = logging.getLogger(__name__)

MAX_FRAGMENT_IRIS = 5_000
"""The most IRIs an entity's fragment may mention before its types are fetched (`fetch_fragment`): more, and the page
says the entity has too many axioms to show, rather than fetch, parse, convert and link them all (DESIGN.md §2.8)."""

MAX_AXIOMS = 1_000
"""The most axioms an entity may have for its page to show them (`entity_axioms`), as for `MAX_FRAGMENT_IRIS`."""

MAX_FRAGMENT_ANNOTATIONS = 2_000
"""The most axiom annotations, nested ones included, a fragment may hold (`pruned_annotations` counts them), as the
OWLAPI's parse time grows with the square of one node's annotations: past it, they're left out, with a note
(DESIGN.md §2.8, Axiom annotations)."""

_AXIOMS = PREFIXES + sparql_file("entity-axioms.rq")
_AXIOM_ANNOTATIONS = PREFIXES + sparql_file("entity-axiom-annotations.rq")
_SYMMETRIC = PREFIXES + sparql_file("entity-symmetric.rq")
_TYPES = PREFIXES + sparql_file("entity-types.rq")
_ENTITY = "$entity"
_NO_IRIS = "VALUES ?iri { }"
_ANNOTATED_SOURCE = f"<{OWL}annotatedSource>".encode()
_NOT_ANNOTATIONS = frozenset(
    f"<{iri}>".encode()
    for iri in (f"{RDF}type", f"{OWL}annotatedSource", f"{OWL}annotatedProperty", f"{OWL}annotatedTarget")
)
"""The predicates of an annotation node's triples that aren't its annotations."""


class TooManyAxiomsError(Exception):
    """An entity's axioms are too many, or mention too many IRIs, to show on its page (DESIGN.md §2.8)."""


class LeftOut(StrEnum):
    """Why a panel's axiom annotations are left out, which the page says: too many, or they couldn't be read."""

    TOO_MANY = "too_many"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Fragment:
    """The triples from which the OWLAPI can parse an entity's axioms (`fetch_fragment`), as N-Triples.

    Attributes:
        axioms: The triples but the axioms' annotations.
        annotations: The axioms' annotations, pruned (`pruned_annotations`); empty if they have none, or they're left
            out.
        annotations_left_out: Why the annotations are left out, if they are; else None.
    """

    axioms: bytes
    annotations: bytes = b""
    annotations_left_out: LeftOut | None = None

    @property
    def ntriples(self) -> bytes:
        """Return all the triples."""
        return self.axioms + self.annotations


async def fetch_fragment(sparql: FusekiClient, dataset: str, iri: str, dataset_clause: str = "") -> Fragment:
    """Return the triples from which the OWLAPI can parse an entity's axioms, with their annotations (DESIGN.md §2.8).

    They are the entity's triples with their blank-node closure, the axioms rooted at blank nodes that list or mention
    it (`sparql/entity-axioms.rq`), the symmetric axioms stated on other entities (`sparql/entity-symmetric.rq`), the
    reified axioms and nested annotations that annotate them (`sparql/entity-axiom-annotations.rq`, pruned by
    `pruned_annotations`), and the types of every IRI those mention (`sparql/entity-types.rq`). If the annotations
    would take the triples past `browl.fuseki.CONSTRUCT_MAX_BYTES`, `MAX_FRAGMENT_IRIS` or
    `MAX_FRAGMENT_ANNOTATIONS`, or their query fails, they're left out, which is logged, so the axioms can still be
    shown. The scans run in a thread, off the event loop.

    Args:
        sparql: The Fuseki client.
        dataset: The dataset that serves the ontology.
        iri: The entity's IRI.
        dataset_clause: The dataset clause that makes the queries see the imports (`browl.fuseki.dataset_clause`).

    Raises:
        SparqlError: A query failed.
        RDFSyntaxError: The IRI isn't valid.
        TooManyAxiomsError: The triples, without the annotations, mention more than `MAX_FRAGMENT_IRIS` IRIs.
    """
    entity = {_ENTITY: f"<{IRI(iri)}>"}
    async with task_group() as group:
        own = group.create_task(sparql.construct(dataset, filled_query(_AXIOMS, dataset_clause, entity)))
        symmetric = group.create_task(sparql.construct(dataset, filled_query(_SYMMETRIC, dataset_clause, entity)))
        annotations = group.create_task(
            caught(sparql.construct(dataset, filled_query(_AXIOM_ANNOTATIONS, dataset_clause, entity)), SparqlError)
        )
    triples = own.result() + symmetric.result()
    mentioned = await asyncio.to_thread(fragment_iris, triples)
    annotation_triples, left_out = b"", None
    if isinstance(result := annotations.result(), ResultTooLargeError):
        left_out = LeftOut.TOO_MANY
    elif isinstance(result, SparqlError):
        logger.error(
            "Showing the axioms of %s without their annotations, which couldn't be read",
            logged_iri(iri),
            exc_info=result,
        )
        left_out = LeftOut.FAILED
    elif result:
        checked = await asyncio.to_thread(_checked_annotations, result, len(triples), mentioned)
        if checked is None:
            left_out = LeftOut.TOO_MANY
        else:
            annotation_triples, mentioned = checked
    if left_out == LeftOut.TOO_MANY:
        logger.warning("Showing the axioms of %s without their annotations, which are too many", logged_iri(iri))
    if mentioned:
        values = f"VALUES ?iri {{ {' '.join(f'<{each}>' for each in sorted(mentioned))} }}"
        triples += await sparql.construct(dataset, filled_query(_TYPES, dataset_clause, {_NO_IRIS: values}))
    return Fragment(triples, annotation_triples, left_out)


def _checked_annotations(ntriples: bytes, size: int, mentioned: set[str]) -> tuple[bytes, set[str]] | None:
    """Return axiom annotations' triples, pruned, and the IRIs a fragment mentions with them; None if they're too many.

    They're too many if they'd take a fragment of `size` bytes that mentions `mentioned` past its caps
    (`fetch_fragment`).
    """
    if size + len(ntriples) > CONSTRUCT_MAX_BYTES:
        return None
    pruned, count = pruned_annotations(ntriples)
    if count > MAX_FRAGMENT_ANNOTATIONS:
        return None
    with_annotations = mentioned | ntriples_iris(pruned)
    if len(with_annotations) > MAX_FRAGMENT_IRIS:
        return None
    return pruned, with_annotations


def pruned_annotations(ntriples: bytes) -> tuple[bytes, int]:
    """Return N-Triples without the annotation nodes `annotation_sources` leaves out, nor the sources they don't keep.

    The OWLAPI's parser overflows its stack on a cycle of `owl:Annotation`s, and nests as deep as they go, so a
    fragment is pruned before it's parsed. The N-Triples are Fuseki's: one triple a line, its terms separated by one
    space.

    Returns:
        The N-Triples, and how many annotations the nodes kept hold: their triples but their type and
        `owl:annotated*` links (`MAX_FRAGMENT_ANNOTATIONS`).
    """
    if _ANNOTATED_SOURCE not in ntriples:
        return ntriples, 0
    lines = ntriples.split(b"\n")
    sources: dict[bytes, list[bytes]] = {}
    counts: Counter[bytes] = Counter()
    for line in lines:
        parts = line.split(b" ", 3)
        if len(parts) == 4:
            if parts[1] == _ANNOTATED_SOURCE:
                sources.setdefault(parts[0], []).append(parts[2])
            elif parts[1] not in _NOT_ANNOTATIONS:
                counts[parts[0]] += 1
    kept = annotation_sources(sources, key=bytes.decode)
    count = sum(counts[node] for node in kept)
    if len(kept) == len(sources) and all(len(each) == 1 for each in sources.values()):
        return ntriples, count
    pruned = []
    for line in lines:
        parts = line.split(b" ", 3)
        if parts[0] in sources and (
            parts[0] not in kept or (len(parts) == 4 and parts[1] == _ANNOTATED_SOURCE and parts[2] != kept[parts[0]])
        ):
            continue
        pruned.append(line)
    return b"\n".join(pruned), count


def fragment_iris(*fragments: bytes, what: str = "the fragment") -> set[str]:
    """Return the IRIs that N-Triples fragments mention, before their types are fetched.

    Raises:
        TooManyAxiomsError: They mention more than `MAX_FRAGMENT_IRIS`; `what` names them in its message.
    """
    mentioned = set[str]().union(*map(ntriples_iris, fragments))
    if len(mentioned) > MAX_FRAGMENT_IRIS:
        raise TooManyAxiomsError(f"{what} mentions {len(mentioned)} IRIs, more than {MAX_FRAGMENT_IRIS}")
    return mentioned


type _Build = Callable[[_Converter, Any], owl.Node]
"""Makes a node from an OWLAPI object, converting its parts with the converter."""


def _fields(node_type: Callable[..., owl.Node], **getters: str) -> _Build:
    """Return a build that makes a node from the values of an OWLAPI object's getters.

    Each value is converted by `_Converter.value`.
    """

    def build(converter: _Converter, owl_object: Any) -> owl.Node:
        return node_type(**{field: converter.value(getattr(owl_object, getter)()) for field, getter in getters.items()})

    return build


def _entity(kind: EntityKind) -> _Build:
    return lambda converter, entity: owl.Entity(kind=kind, iri=IRI(str(entity.getIRI())))


def _literal(converter: _Converter, literal: Any) -> owl.Node:
    language = str(literal.getLang()) if literal.hasLang() else None
    return owl.Literal(
        lexical=str(literal.getLiteral()), datatype=IRI(str(literal.getDatatype().getIRI())), language=language
    )


def _facet(converter: _Converter, restriction: Any) -> owl.Node:
    return owl.FacetRestriction(
        facet=IRI(str(restriction.getFacet().getIRI())), value=cast(owl.Literal, converter(restriction.getFacetValue()))
    )


def _annotation(converter: _Converter, annotation: Any) -> owl.Node:
    """Make an annotation, with its own annotations unless it's `MAX_ANNOTATION_DEPTH` deep, when they're left out."""
    nested = converter.value(annotation.getAnnotations()) if converter.depth < MAX_ANNOTATION_DEPTH else ()
    return owl.Annotation(
        property=cast(owl.Entity, converter(annotation.getProperty())),
        value=cast(owl.IRIReference | owl.AnonymousIndividual | owl.Literal, converter(annotation.getValue())),
        annotations=nested,
    )


def _iri(converter: _Converter, iri: Any) -> owl.Node:
    return owl.IRIReference(iri=IRI(str(iri)))


def _annotation_domain(converter: _Converter, axiom: Any) -> owl.Node:
    return owl.AnnotationPropertyDomain(
        property=cast(owl.Entity, converter(axiom.getProperty())),
        domain=owl.IRIReference(iri=IRI(str(axiom.getDomain()))),
    )


def _annotation_range(converter: _Converter, axiom: Any) -> owl.Node:
    return owl.AnnotationPropertyRange(
        property=cast(owl.Entity, converter(axiom.getProperty())),
        range=owl.IRIReference(iri=IRI(str(axiom.getRange()))),
    )


def _anonymous(converter: _Converter, individual: Any) -> owl.Node:
    """Make an anonymous individual, labelled anew while `convert_axioms` relabels them (`_Converter.labels`)."""
    node_id = str(individual.toStringID())
    if converter.labels is not None:
        node_id = converter.labels.setdefault(node_id, f"_:b{len(converter.labels) + 1}")
    return owl.AnonymousIndividual(id=node_id)


def _other(converter: _Converter, axiom: Any) -> owl.Node:
    """Make an `OtherAxiom`, without its annotations, which `_axiom_node` adds as any axiom's."""
    return owl.OtherAxiom(
        axiom_type=str(axiom.getAxiomType().getName()), functional=str(axiom.getAxiomWithoutAnnotations())
    )


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
    ("OWLAnonymousIndividual", _anonymous),
    ("OWLLiteral", _literal),
    ("OWLFacetRestriction", _facet),
    ("IRI", _iri),  # an annotation's subject or value
    ("OWLAnnotation", _annotation),
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
    (
        "OWLAnnotationAssertionAxiom",
        _fields(owl.AnnotationAssertion, subject="getSubject", property="getProperty", value="getValue"),
    ),
    ("OWLAxiom", _other),  # any other, such as a SWRL rule
)


@dataclass(frozen=True, slots=True)
class _Java:
    """The Java classes and constants this module uses."""

    ArrayList: Any
    AxiomType: Any
    Collection: Any
    Collections: Any
    HashSet: Any
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
        HashSet=jpype.JClass("java.util.HashSet"),
        IRI=jpype.JClass(f"{model}.IRI"),
        List=jpype.JClass("java.util.List"),
        excluded=jpype.JClass(f"{model}.parameters.Imports").EXCLUDED,
        builds=tuple((jpype.JClass(f"{model}.{name}"), build) for name, build in _BUILDS),
    )


def entity_closure(ontology: Any, iri: str) -> list[Any]:
    """Return an entity's axioms in an ontology (the module's docstring says which), with their annotations.

    The same for a fragment fetched for the entity as for the whole ontology: a test checks that. An IRI with several
    types (punning) has the axioms of each. An axiom stated with different annotations is listed once for each: the
    OWLAPI's axioms differ by their annotations, and the frame merges them (`browl.frames`).

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
                found.setdefault(axiom, None)
    return list(found)


def is_declaration(axiom: Any) -> bool:
    """Return whether an OWLAPI axiom is a declaration."""
    return str(axiom.getAxiomType().getName()) == "Declaration"


def axioms_about(ontology: Any, iri: str) -> list[Any]:
    """Return the axioms in an ontology whose subject is an IRI, as OWL's RDF mapping writes them, with annotations.

    That's what the OWLAPI lists for each entity with the IRI (`getAxioms(entity)`), with a class's `HasKey` axioms, the
    property chains that imply an object property, the annotation assertions about the IRI, and its annotated
    declarations, whose annotations can name another entity, whose usage they are. The two-member axioms, such as
    `DisjointClasses(A, B)`, are listed for both members.

    Args:
        ontology: The `OWLOntology`.
        iri: The IRI.

    Returns:
        The `OWLAxiom`s, each once.
    """
    check_java_thread()
    java = _java()
    subject = java.IRI.create(iri)
    found: dict[Any, None] = dict.fromkeys(ontology.getAnnotationAssertionAxioms(subject))
    for entity in ontology.getEntitiesInSignature(subject):
        own = list(ontology.getAxioms(entity, java.excluded))
        own += (axiom for axiom in ontology.getDeclarationAxioms(entity) if axiom.isAnnotated())
        if entity.isOWLClass():
            own += ontology.getHasKeyAxioms(entity)
        elif entity.isOWLObjectProperty():
            chains = ontology.getAxioms(java.AxiomType.SUB_PROPERTY_CHAIN_OF, java.excluded)
            own += (axiom for axiom in chains if axiom.getSuperProperty().equals(entity))
        found.update(dict.fromkeys(own))
    return list(found)


def merged_annotations(axioms: Iterable[Any], extra: Mapping[Any, Iterable[Any]]) -> list[Any]:
    """Return OWLAPI axioms, each with the annotations `extra` holds for it without its annotations, added."""
    java = _java()
    merged = []
    for axiom in axioms:
        bare = axiom.getAxiomWithoutAnnotations()
        more = extra.get(bare)
        if more:
            annotations = java.HashSet(axiom.getAnnotations())
            annotations.addAll(more)
            axiom = bare.getAnnotatedAxiom(annotations)
        merged.append(axiom)
    return merged


class _Converter:
    """Converts OWLAPI objects into nodes (`browl.owl`), choosing each build (`_BUILDS`) by the object's Java class.

    One Python function per kind of node, not a Java visitor; DESIGN.md §2.8 says why.

    Attributes:
        labels: Anonymous individuals' new labels, by the OWLAPI's node IDs, while `convert_axioms` relabels them;
            else None.
    """

    def __init__(self) -> None:
        self._java = _java()
        self._builds: dict[type, _Build] = {}
        self._depth = 0
        self.labels: dict[str, str] | None = None

    @property
    def depth(self) -> int:
        """Return the depth of the node being made, the outermost being 1; 0 between calls."""
        return self._depth

    def __call__(self, owl_object: Any) -> owl.Node:
        """Return an OWLAPI object as a node.

        Raises:
            FragmentError: It's of a kind no node type covers (only axioms have a fallback, `browl.owl.OtherAxiom`),
                or its nodes nest more than `MAX_DEPTH` deep.
        """
        build = self._builds.get(type(owl_object))
        if build is None:
            build = self._builds[type(owl_object)] = self._build(owl_object)
        if self._depth == MAX_DEPTH:
            raise FragmentError(f"an axiom's nodes nest more than {MAX_DEPTH} deep")
        self._depth += 1
        try:
            return build(self, owl_object)
        finally:
            self._depth -= 1

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
    """Return the converter, which keeps no state between calls but the builds it has chosen for each Java class."""
    return _Converter()


def convert_axioms(axioms: Iterable[Any]) -> list[owl.AnyAxiom]:
    """Return OWLAPI axioms as nodes (`browl.owl`), sorted by kind and text, with their annotations if they have any.

    The OWLAPI labels anonymous individuals afresh whenever it parses (`_:genid…`), so if there are any, they're
    labelled again, `_:b1`, `_:b2`, …, in order of appearance in the axioms sorted without labels: a page's text and
    order are then the same each time. An axiom with a value its node refuses, such as an IRI that isn't absolute, is
    logged and kept as an `OtherAxiom`, in functional syntax, so the rest still show.

    Call this on the Java thread (`browl.owlapi.run_java`).

    Raises:
        FragmentError: An axiom holds an object of a kind no node type covers, or its nodes nest more than
            `MAX_DEPTH` deep.
    """
    check_java_thread()
    converter = _converter()
    axioms = list(axioms)
    nodes = [_axiom_node(converter, axiom) for axiom in axioms]
    if any(token.kind == owl.TokenKind.BLANK_NODE for node in nodes for token in _tokens(node)):
        order = sorted(range(len(nodes)), key=lambda index: (nodes[index].kind, _unlabelled(nodes[index])))
        converter.labels = {}
        try:
            nodes = [_axiom_node(converter, axioms[index]) for index in order]
        finally:
            converter.labels = None
    return sorted(nodes, key=lambda node: (node.kind, node.manchester))


def _axiom_node(converter: _Converter, axiom: Any) -> owl.AnyAxiom:
    try:
        node = converter(axiom)
    except (ValidationError, RDFSyntaxError) as error:
        logger.warning("Showing an axiom in functional syntax, as browl can't hold it as nodes: %s", error)
        node = _other(converter, axiom)
    except RecursionError as error:
        raise FragmentError(f"an axiom nests too deep for browl: {error}") from error
    if not isinstance(node, owl.Axiom):
        raise FragmentError(f"not an axiom: {node.type}")
    if axiom.isAnnotated():
        try:
            annotations = converter.value(axiom.getAnnotations())
        except (ValidationError, RDFSyntaxError) as error:
            logger.warning("Not showing an axiom's annotations, as browl can't hold them as nodes: %s", error)
        else:
            node = node.model_copy(update={"annotations": annotations})
    return cast(owl.AnyAxiom, node)


def _tokens(node: owl.AnyAxiom) -> Iterator[owl.Token]:
    """Yield an axiom's Manchester tokens, then its annotations', each before its own annotations'."""
    yield from node.manchester_tokens()
    stack = list(reversed(node.annotations))
    while stack:
        annotation = stack.pop()
        yield from annotation.manchester_tokens()
        stack.extend(reversed(annotation.annotations))


def _unlabelled(node: owl.AnyAxiom) -> str:
    """Return an axiom's text, and its annotations', without its anonymous individuals' labels."""
    return owl.manchester_text(
        dataclasses.replace(token, text="_:") if token.kind == owl.TokenKind.BLANK_NODE else token
        for token in _tokens(node)
    )


def entity_axioms(ntriples: bytes, iri: str) -> list[owl.AnyAxiom]:
    """Parse a fragment fetched for an entity (`fetch_fragment`) and return the entity's axioms, as nodes.

    Call this on the Java thread (`browl.owlapi.run_java`).

    Raises:
        FragmentError: The OWLAPI couldn't parse the fragment, or failed on it with any other Java exception, or an
            axiom holds an object of a kind no node type covers, or nests too deep.
        TooManyAxiomsError: The entity has more than `MAX_AXIOMS` axioms, not counting their annotations.
    """
    try:
        with ntriples_ontology(ntriples) as ontology:
            closure = entity_closure(ontology, iri)
            if (count := len({axiom.getAxiomWithoutAnnotations() for axiom in closure})) > MAX_AXIOMS:
                raise TooManyAxiomsError(f"the entity has {count} axioms, more than {MAX_AXIOMS}")
            return convert_axioms(closure)
    except jpype.JException as error:
        raise FragmentError(f"the OWLAPI failed on the fragment: {error}") from error
