"""An entity's axioms, fetched from Fuseki as a fragment, rendered in Manchester syntax by the OWLAPI (DESIGN.md §2.8).

A page fetches the triples its entity's axioms need (`fetch_fragment`), the OWLAPI parses them as a small ontology, and
`entity_axioms` renders the axioms as data (`Axiom`): Manchester text with full IRIs, with no HTML. Templates make the
HTML (`browl.manchester`, `templates/axioms.html`).

An entity's axioms are those the OWLAPI lists for it (`getAxioms(entity)`), and for a class its `HasKey` axioms and
the general class axioms that mention it, for an object property the property chains that imply it, without their
annotations. That's every logical axiom about the entity, plus an annotation property's domain, range and
super-properties; not its annotation assertions, nor its usage in other entities' axioms (P-27).
"""

import asyncio
import functools
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import jpype
from pydantic import BaseModel

from browl import manchester
from browl.fuseki import FusekiClient
from browl.owlapi import FragmentError, check_java_thread, ntriples_ontology
from browl.precache.core import PREFIXES, sparql_file
from browl.rdf import IRI, XSD_LEXICAL_FORMS, ntriples_iris

_AXIOMS = PREFIXES + sparql_file("entity-axioms.rq")
_SYMMETRIC = PREFIXES + sparql_file("entity-symmetric.rq")
_TYPES = PREFIXES + sparql_file("entity-types.rq")
_ENTITY = "$entity"
_WHERE = "\nWHERE {"
_NO_IRIS = "VALUES ?iri { }"


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


class Syntax(StrEnum):
    """The syntax an axiom's text is in."""

    MANCHESTER = "manchester"
    FUNCTIONAL = "functional"


class Axiom(BaseModel, frozen=True):
    """An axiom, as data: what a page shows of it, without HTML (DESIGN.md §2.8, Panel data apart from its HTML).

    Attributes:
        kind: Its kind, by its OWL 2 functional-syntax name (`SubClassOf`, `ObjectPropertyDomain`, …), but for a
            property chain, `SubPropertyChainOf`.
        syntax: The syntax of `text`: Manchester, but functional syntax when the OWLAPI renders the axiom as nothing
            (it does for `DatatypeDefinition`), or when the axiom has a literal that the OWLAPI writes unquoted and
            whose lexical form isn't valid for its datatype (`_unquoted_ill_typed`).
        text: The axiom, every entity in it written as its full IRI, `<…>`; a `HasKey` axiom's properties separated by
            commas, which OWLAPI 4's renderer leaves out.
        operands: What the axiom relates, each in Manchester syntax like `text`, in functional-syntax order: e.g. a
            `SubClassOf` axiom's subclass and superclass, a property domain's property and class, an n-ary axiom's
            members. A property chain is one operand, its properties joined by `o`. In functional syntax, as `text`
            is, if an ill-typed literal puts `text` in it.
        iris: The IRIs of the entities and datatypes in the axiom (its signature): those in `text` and `operands`
            in order of first appearance, then the rest sorted. Only these become links (DESIGN.md §2.8).
    """

    kind: str
    syntax: Syntax
    text: str
    operands: tuple[str, ...]
    iris: tuple[str, ...]


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
    if not mentioned:
        return triples
    values = f"VALUES ?iri {{ {' '.join(f'<{each}>' for each in sorted(mentioned))} }}"
    return triples + await sparql.construct(dataset, _filled(_TYPES, dataset_clause, {_NO_IRIS: values}))


@dataclass(frozen=True, slots=True)
class _Java:
    """The Java classes and constants this module uses."""

    AxiomType: Any
    Collection: Any
    Collections: Any
    DefaultPrefixManager: Any
    IRI: Any
    ManchesterOWLSyntaxOWLObjectRendererImpl: Any
    OWLHasKeyAxiom: Any
    OWLLiteral: Any
    OWLObjectVisitorEx: Any
    OWLObjectWalker: Any
    excluded: Any  # Imports.EXCLUDED: a fragment's imports are never loaded
    operands: tuple[tuple[Any, tuple[str, ...]], ...]  # `_OPERANDS`, with the interfaces


_OPERANDS = (
    # Each kind of axiom (OWLAPI interface), and the methods that return its operands, in functional-syntax order.
    ("OWLSubClassOfAxiom", ("getSubClass", "getSuperClass")),
    ("OWLNaryClassAxiom", ("getClassExpressionsAsList",)),
    ("OWLDisjointUnionAxiom", ("getOWLClass", "getClassExpressions")),
    ("OWLHasKeyAxiom", ("getClassExpression", "getPropertyExpressions")),
    ("OWLSubPropertyChainOfAxiom", ("getPropertyChain", "getSuperProperty")),
    ("OWLSubPropertyAxiom", ("getSubProperty", "getSuperProperty")),
    ("OWLSubAnnotationPropertyOfAxiom", ("getSubProperty", "getSuperProperty")),
    ("OWLInverseObjectPropertiesAxiom", ("getFirstProperty", "getSecondProperty")),
    ("OWLNaryPropertyAxiom", ("getProperties",)),
    ("OWLPropertyDomainAxiom", ("getProperty", "getDomain")),
    ("OWLPropertyRangeAxiom", ("getProperty", "getRange")),
    ("OWLAnnotationPropertyDomainAxiom", ("getProperty", "getDomain")),
    ("OWLAnnotationPropertyRangeAxiom", ("getProperty", "getRange")),
    ("OWLUnaryPropertyAxiom", ("getProperty",)),
    ("OWLClassAssertionAxiom", ("getIndividual", "getClassExpression")),
    ("OWLPropertyAssertionAxiom", ("getSubject", "getProperty", "getObject")),
    ("OWLNaryIndividualAxiom", ("getIndividualsAsList",)),
    ("OWLDatatypeDefinitionAxiom", ("getDatatype", "getDataRange")),
)

_KIND_NAMES = {
    "IrrefexiveObjectProperty": "IrreflexiveObjectProperty",
    "AnnotationPropertyRangeOf": "AnnotationPropertyRange",
}
"""OWLAPI axiom type names that differ from OWL 2's (`AxiomType.getName`)."""

_LEFT_OUT = frozenset({"Declaration", "AnnotationAssertion"})
"""The kinds of axiom about an entity that aren't its axioms: its declarations and annotations."""


@functools.cache
def _java() -> _Java:
    """Look the classes up once, on first use; the JVM must be running."""
    model = "org.semanticweb.owlapi.model"
    return _Java(
        AxiomType=jpype.JClass(f"{model}.AxiomType"),
        Collection=jpype.JClass("java.util.Collection"),
        Collections=jpype.JClass("java.util.Collections"),
        DefaultPrefixManager=jpype.JClass("org.semanticweb.owlapi.util.DefaultPrefixManager"),
        IRI=jpype.JClass(f"{model}.IRI"),
        ManchesterOWLSyntaxOWLObjectRendererImpl=jpype.JClass(
            "org.semanticweb.owlapi.manchestersyntax.renderer.ManchesterOWLSyntaxOWLObjectRendererImpl"
        ),
        OWLHasKeyAxiom=jpype.JClass(f"{model}.OWLHasKeyAxiom"),
        OWLLiteral=jpype.JClass(f"{model}.OWLLiteral"),
        OWLObjectVisitorEx=jpype.JClass(f"{model}.OWLObjectVisitorEx"),
        OWLObjectWalker=jpype.JClass("org.semanticweb.owlapi.util.OWLObjectWalker"),
        excluded=jpype.JClass(f"{model}.parameters.Imports").EXCLUDED,
        operands=tuple((jpype.JClass(f"{model}.{name}"), methods) for name, methods in _OPERANDS),
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


class _Literals:
    """Collects the literals an `OWLObjectWalker` visits: an `OWLObjectVisitorEx`, through `jpype.JProxy`."""

    def __init__(self) -> None:
        self.found: list[Any] = []

    def visit(self, owl_object: Any) -> None:
        """Visit an OWL object, any kind: Java's overloads of `visit` all come here."""
        if isinstance(owl_object, _java().OWLLiteral):
            self.found.append(owl_object)


def _unquoted_ill_typed(axiom: Any) -> bool:
    """Return whether an axiom has an ill-typed literal that OWLAPI 4's Manchester renderer writes unquoted.

    The renderer writes literals of `XSD_LEXICAL_FORMS`' datatypes unquoted without checking their lexical form, so an
    ill-typed one's text could pass for IRIs and keywords.
    """
    java = _java()
    if not any(str(datatype.getIRI()) in XSD_LEXICAL_FORMS for datatype in axiom.getDatatypesInSignature()):
        return False
    literals = _Literals()
    walker = java.OWLObjectWalker(java.Collections.singleton(axiom))
    walker.walkStructure(jpype.JProxy(java.OWLObjectVisitorEx, inst=literals))
    for literal in literals.found:
        pattern = XSD_LEXICAL_FORMS.get(str(literal.getDatatype().getIRI()))
        if pattern is not None and not pattern.fullmatch(str(literal.getLiteral())):
            return True
    return False


class _Renderer:
    """Renders OWL objects in Manchester syntax with full IRIs.

    The OWLAPI's renderer, given a prefix manager with no prefixes, writes each entity as `<IRI>` (S-1). It keeps
    state between calls, so each page gets its own.
    """

    def __init__(self) -> None:
        java = _java()
        prefixes = java.DefaultPrefixManager()
        prefixes.clear()
        self._renderer = java.ManchesterOWLSyntaxOWLObjectRendererImpl()
        self._renderer.setShortFormProvider(prefixes)

    def manchester(self, owl_object: Any) -> str:
        """Return an OWL object in Manchester syntax, on one line; empty if the OWLAPI renders it as nothing."""
        return manchester.one_line(str(self._renderer.render(owl_object)))

    def operand(self, value: Any) -> str:
        """Return an OWL object in Manchester syntax, else functional syntax."""
        return self.manchester(value) or str(value)

    def axiom(self, axiom: Any) -> Axiom:
        """Return an axiom as data."""
        if _unquoted_ill_typed(axiom):
            operands = self.operands(axiom, str)
            text, syntax = str(axiom), Syntax.FUNCTIONAL
        else:
            operands = self.operands(axiom, self.operand)
            text, syntax = self.manchester_text(axiom, operands)
        own = {str(entity.getIRI()) for entity in axiom.getSignature()}
        found = dict.fromkeys(iri for each in (text, *operands) for iri in manchester.iris(each) if iri in own)
        iris = (*found, *sorted(own.difference(found)))
        name = str(axiom.getAxiomType().getName())
        return Axiom(kind=_KIND_NAMES.get(name, name), syntax=syntax, text=text, operands=operands, iris=iris)

    def manchester_text(self, axiom: Any, operands: tuple[str, ...]) -> tuple[str, Syntax]:
        """Return an axiom's text, in Manchester syntax unless the OWLAPI renders it as nothing, and its syntax."""
        if isinstance(axiom, _java().OWLHasKeyAxiom):
            # OWLAPI 4's renderer writes a key's properties with nothing between them (`<A> HasKey <p><q>`).
            return f"{operands[0]} HasKey {', '.join(operands[1:])}", Syntax.MANCHESTER
        text = self.manchester(axiom)
        return (text, Syntax.MANCHESTER) if text else (str(axiom), Syntax.FUNCTIONAL)

    def operands(self, axiom: Any, render: Callable[[Any], str]) -> tuple[str, ...]:
        """Return an axiom's operands, each rendered by `render`.

        None for an axiom of a kind `_OPERANDS` lacks, such as a SWRL rule.
        """
        java = _java()
        for interface, methods in java.operands:
            if isinstance(axiom, interface):
                operands: list[str] = []
                for method in methods:
                    value = getattr(axiom, method)()
                    if method == "getPropertyChain":
                        operands.append(" o ".join(map(render, value)))
                    elif isinstance(value, java.Collection):
                        operands += map(render, value)
                    else:
                        operands.append(render(value))
                return tuple(operands)
        return ()


def render_axioms(axioms: Iterable[Any]) -> list[Axiom]:
    """Return OWLAPI axioms as data, rendered with full IRIs (`Axiom`), sorted by kind and text.

    Call this on the Java thread (`browl.owlapi.run_java`).
    """
    check_java_thread()
    renderer = _Renderer()
    return sorted(map(renderer.axiom, axioms), key=lambda axiom: (axiom.kind, axiom.text))


def entity_axioms(ntriples: bytes, iri: str) -> list[Axiom]:
    """Parse a fragment fetched for an entity (`fetch_fragment`) and return the entity's axioms, rendered.

    Call this on the Java thread (`browl.owlapi.run_java`).

    Raises:
        FragmentError: The OWLAPI couldn't parse the fragment, or failed on it with any other Java exception.
    """
    try:
        with ntriples_ontology(ntriples) as ontology:
            return render_axioms(entity_closure(ontology, iri))
    except jpype.JException as error:
        raise FragmentError(f"the OWLAPI failed on the fragment: {error}") from error
