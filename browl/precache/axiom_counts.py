"""The axiom-counts pre-caching hook (P-17): the `axiom_count` table, for the home page (DESIGN.md §2.5).

The ontology's own axioms, without its imports, counted from its triples by OWL 2's mapping to RDF rather than by the
OWLAPI, so no step loads the whole ontology into the OWLAPI for them: each triple is one axiom, or none, by its
predicate, and its class if it's an `rdf:type` (`axiom-triples.rq`), less those of nodes whose triples aren't axioms,
such as the ontology's header and reified axioms (`axiom-not-triples.rq`); then corrected (`axiom-adjustments.rq`) for
annotation properties' own axioms, datatype definitions, symmetric axioms stated both ways, which count once, and the
axioms that are a node of their own (n-ary disjointness, negative assertions, SWRL rules).
Annotation assertions are told from property assertions as the annotations panel tells them (`entity-annotations.rq`).
"""

import logging
from collections import Counter, defaultdict
from collections.abc import Mapping
from collections.abc import Set as AbstractSet

from browl.derived import AxiomKind
from browl.fuseki import SparqlError
from browl.precache.core import PreCacheContext, hook, sparql_file
from browl.rdf import OWL, RDF, RDFS, XSD, Literal, Term

logger = logging.getLogger(__name__)

SWRL = "http://www.w3.org/2003/11/swrl#"
"""The SWRL vocabulary, which OWL's RDF mapping uses for rules."""

DECLARATION_TYPES = frozenset(
    {f"{OWL}Class", f"{OWL}ObjectProperty", f"{OWL}DatatypeProperty", f"{OWL}AnnotationProperty"}
    | {f"{OWL}NamedIndividual", f"{RDFS}Datatype"}
)
"""The classes an `rdf:type` triple declares an entity with."""

CHARACTERISTICS = frozenset(
    f"{OWL}{name}Property"
    for name in ("Functional", "InverseFunctional", "Transitive", "Symmetric", "Asymmetric", "Reflexive", "Irreflexive")
)
"""The classes an `rdf:type` triple gives a property a characteristic with, a logical axiom."""

LOGICAL_PREDICATES = frozenset(
    {f"{RDFS}{name}" for name in ("subClassOf", "subPropertyOf", "domain", "range")}
    | {
        f"{OWL}{name}"
        for name in (
            "equivalentClass",
            "disjointWith",
            "disjointUnionOf",
            "hasKey",
            "equivalentProperty",
            "propertyDisjointWith",
            "inverseOf",
            "propertyChainAxiom",
            "sameAs",
            "differentFrom",
        )
    }
)
"""The vocabulary's predicates whose triple with an IRI subject is a logical axiom (or, for some subjects, another kind,
which `axiom-adjustments.rq` counts)."""

BUILTIN_ANNOTATIONS = frozenset(
    {f"{RDFS}{name}" for name in ("label", "comment", "seeAlso", "isDefinedBy")}
    | {
        f"{OWL}{name}"
        for name in ("deprecated", "versionInfo", "priorVersion", "backwardCompatibleWith", "incompatibleWith")
    }
)
"""OWL 2's built-in annotation properties, as `entity-annotations.rq` lists them."""

VOCABULARIES = (RDF, RDFS, OWL, XSD, SWRL)
"""The vocabularies whose terms, as a predicate or a class, make no axiom unless listed above."""

_LOGICAL_PROPERTY_TYPES = frozenset({f"{OWL}ObjectProperty", f"{OWL}DatatypeProperty"} | CHARACTERISTICS)
_TYPE = f"{RDF}type"

type Counts = Mapping[tuple[str, str | None], int]
"""Triple counts by predicate and, for `rdf:type`, IRI class (None for a class expression)."""


@hook()
async def axiom_counts(context: PreCacheContext) -> None:
    """Fill `axiom_count`: how many of the ontology's own axioms there are of each kind (`browl.derived.AxiomKind`)."""
    triples = _typed_apart(await _grouped(context, sparql_file("axiom-triples.rq")))
    not_axioms = await _grouped(context, sparql_file("axiom-not-triples.rq"))
    adjustments: Counter[str] = Counter()
    async with context.bulk_select(sparql_file("axiom-adjustments.rq"), imports=False) as stream:
        kind, count = (stream.variables.index(name) for name in ("kind", "count"))
        async for row in stream:
            match row[kind]:
                case Literal(value=value):
                    adjustments[value] += _number(row[count])
                case other:
                    raise SparqlError(f"an axiom count query gave a kind of {other!r:.200}")
    types: defaultdict[str, set[str]] = defaultdict(set)
    async with context.bulk_select(sparql_file("axiom-property-types.rq")) as stream:
        property_, type_ = (stream.variables.index(name) for name in ("property", "type"))
        async for row in stream:
            types[str(row[property_])].add(str(row[type_]))
    counts = count_axioms(triples, not_axioms, adjustments, types)
    rows = [(kind.value, counts[kind]) for kind in AxiomKind]
    await context.db.insert("INSERT INTO axiom_count (kind, count) VALUES (?, ?)", rows)
    logger.info("The ontology has %d axioms: %r", sum(counts.values()), {kind.value: counts[kind] for kind in counts})


def count_axioms(
    triples: Counts, not_axioms: Counts, adjustments: Mapping[str, int], types: Mapping[str, AbstractSet[str]]
) -> Counter[AxiomKind]:
    """Return the axioms of each kind, from the counts of the hook's queries.

    Args:
        triples: The ontology's triples (`axiom-triples.rq`).
        not_axioms: Those of them that aren't axioms whatever their predicate (`axiom-not-triples.rq`).
        adjustments: The corrections by kind (`axiom-adjustments.rq`).
        types: Each property's declared types and characteristics (`axiom-property-types.rq`).
    """
    counts: Counter[AxiomKind] = Counter()
    for key, count in triples.items():
        kind = _kind(*key, types)
        if kind is not None:
            counts[kind] += count - not_axioms.get(key, 0)
    moved = adjustments.get("annotation_property", 0)
    defined = adjustments.get("datatype_definition", 0)
    counts[AxiomKind.LOGICAL] += adjustments.get("node", 0) - adjustments.get("symmetric", 0) - moved - defined
    counts[AxiomKind.ANNOTATION] += moved
    counts[AxiomKind.OTHER] += defined
    return counts


def _kind(predicate: str, class_: str | None, types: Mapping[str, AbstractSet[str]]) -> AxiomKind | None:
    """Return what kind of axiom a triple is, by its predicate and class; None if it isn't one."""
    if predicate == _TYPE:
        if class_ in DECLARATION_TYPES:
            return AxiomKind.DECLARATION
        if class_ is None or class_ in CHARACTERISTICS:
            return AxiomKind.LOGICAL
        return None if class_.startswith(VOCABULARIES) else AxiomKind.LOGICAL  # a class assertion
    if predicate in LOGICAL_PREDICATES:
        return AxiomKind.LOGICAL
    if predicate in BUILTIN_ANNOTATIONS:
        return AxiomKind.ANNOTATION
    if predicate.startswith(VOCABULARIES):
        return None
    declared = types.get(predicate, set())
    if f"{OWL}AnnotationProperty" in declared or not declared & _LOGICAL_PROPERTY_TYPES:
        return AxiomKind.ANNOTATION
    return AxiomKind.LOGICAL  # a property assertion


def _typed_apart(counts: dict[tuple[str, str | None], int]) -> dict[tuple[str, str | None], int]:
    """Return `axiom-triples.rq`'s counts with its `rdf:type` total made the count of those whose class isn't an IRI.

    The query counts every triple by predicate, and the `rdf:type` triples again by IRI class, as that's faster than
    counting them apart (see the query).
    """
    typed = sum(count for (predicate, class_), count in counts.items() if predicate == _TYPE and class_ is not None)
    total = counts.get((_TYPE, None), 0)
    if total < typed:
        raise SparqlError(f"the axiom triples query counted {total} rdf:type triples, but {typed} by class")
    return counts | {(_TYPE, None): total - typed}


async def _grouped(context: PreCacheContext, query: str) -> dict[tuple[str, str | None], int]:
    """Return a query's counts by predicate and class, on the ontology alone."""
    counts: dict[tuple[str, str | None], int] = {}
    async with context.bulk_select(query, imports=False) as stream:
        predicate, class_, count = (stream.variables.index(name) for name in ("predicate", "class", "count"))
        async for row in stream:
            counts[str(row[predicate]), None if row[class_] is None else str(row[class_])] = _number(row[count])
    return counts


def _number(term: Term | None) -> int:
    """Return a count's value.

    Raises:
        SparqlError: It isn't a literal non-negative integer.
    """
    match term:
        case Literal(value=value) if value.isdigit():
            return int(value)
    raise SparqlError(f"an axiom count query gave a count of {term!r:.200}")
