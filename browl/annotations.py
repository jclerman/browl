"""An entity's annotations, fetched from Fuseki and grouped by property, as typed data (DESIGN.md §2.8).

Each value holds its axiom annotations (U-35), such as a definition's references, which a second query reads
(`fetch_value_annotations`).
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from functools import cache
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from browl import owl, rdf
from browl.axioms import annotation_sources
from browl.fuseki import FusekiClient, ResultTooLargeError, SparqlError, filled_query
from browl.names import language_rank
from browl.precache.core import PREFIXES, sparql_file
from browl.rdf import IRI, BlankNode, Term

_ANNOTATIONS = PREFIXES + sparql_file("entity-annotations.rq")
_VALUE_ANNOTATIONS = PREFIXES + sparql_file("entity-value-annotations.rq")

MAX_ANNOTATIONS = 1_000
"""The most annotation values an entity may have for its page to show them: more, and the page says so, as for
`browl.axioms.MAX_AXIOMS`."""

MAX_ANNOTATIONS_CHARS = 4 * 2**20
"""The most characters an entity's annotations may hold in all, as Fuseki sends them, for its page to show them: room
for `MAX_ANNOTATIONS` values well past what the page shows of each, so huge values can't exhaust the app's memory.
The axiom annotations' query has this cap too, of its own."""

MAX_AXIOM_ANNOTATIONS = 2_000
"""The most axiom annotations an entity's annotation values may carry in all, nested ones included, for its page to
show them: more, and the values are shown without them, with a note, so the panel still renders."""


class TooManyAnnotationsError(Exception):
    """An entity has more than `MAX_ANNOTATIONS` annotation values, or they're longer than `MAX_ANNOTATIONS_CHARS`."""


class TooManyAxiomAnnotationsError(Exception):
    """An entity's values carry too many axiom annotations: `MAX_AXIOM_ANNOTATIONS`, or `MAX_ANNOTATIONS_CHARS`."""


class IRIValue(BaseModel):
    """An annotation's value that is an IRI.

    Attributes:
        iri: The IRI.
        annotations: Its axiom annotations (DESIGN.md §2.8, Axiom annotations).
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["iri"] = "iri"
    iri: IRI
    annotations: tuple[owl.Annotation, ...] = ()


class LiteralValue(BaseModel):
    """An annotation's value that is a literal.

    It parallels `rdf.Literal` on purpose, as typed data for the JSON API (U-42), which is why it keeps `datatype`,
    though pages don't show it.

    Attributes:
        lexical: Its lexical form.
        datatype: Its datatype's IRI.
        language: Its language tag, lowercased, if it has one.
        direction: Its base direction, "ltr" or "rtl", if it has one (RDF 1.2).
        annotations: Its axiom annotations (DESIGN.md §2.8, Axiom annotations).
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["literal"] = "literal"
    lexical: str
    datatype: IRI
    language: Annotated[str, StringConstraints(pattern=r"^[a-z]+(-[a-z0-9]+)*$")] | None = None
    direction: Literal["ltr", "rtl"] | None = None
    annotations: tuple[owl.Annotation, ...] = ()


type AnnotationValue = Annotated[IRIValue | LiteralValue, Field(discriminator="type")]

type ValueAnnotations = Mapping[tuple[IRI, AnnotationValue], tuple[owl.Annotation, ...]]
"""Axiom annotations, by the (property, value) pair they annotate, the value without annotations of its own."""


class AnnotationGroup(BaseModel):
    """An annotation property's values on an entity.

    Attributes:
        property: The annotation property's IRI.
        values: Its values, in the order the page shows them (`group_annotations`).
    """

    model_config = ConfigDict(frozen=True)

    property: IRI
    values: tuple[AnnotationValue, ...]


async def fetch_annotations(
    sparql: FusekiClient, dataset: str, iri: str, dataset_clause: str = ""
) -> list[tuple[IRI, AnnotationValue]]:
    """Return an entity's annotations, as (property, value) pairs, in no particular order.

    Args:
        sparql: The Fuseki client.
        dataset: The dataset that serves the ontology.
        iri: The entity's IRI.
        dataset_clause: The dataset clause that makes the query see the imports (`browl.fuseki.dataset_clause`).

    Raises:
        SparqlError: The query failed, or returned something other than an IRI property and an IRI or literal value.
        RDFSyntaxError: The IRI isn't valid.
        TooManyAnnotationsError: The entity has more than `MAX_ANNOTATIONS` annotation values, or they're longer than
            `MAX_ANNOTATIONS_CHARS`.
    """
    query = filled_query(_ANNOTATIONS, dataset_clause, {"$entity": f"<{IRI(iri)}>"})
    try:
        result = await sparql.select(dataset, f"{query}LIMIT {MAX_ANNOTATIONS + 1}\n", max_chars=MAX_ANNOTATIONS_CHARS)
    except ResultTooLargeError as error:
        raise TooManyAnnotationsError(
            f"the entity's annotations are longer than {MAX_ANNOTATIONS_CHARS} characters"
        ) from error
    if len(result.rows) > MAX_ANNOTATIONS:
        raise TooManyAnnotationsError(f"the entity has more than {MAX_ANNOTATIONS} annotation values")
    pairs: list[tuple[IRI, AnnotationValue]] = []
    for property_, value in result.rows:
        if not isinstance(property_, IRI):
            raise SparqlError(f"an annotation property that isn't an IRI: {property_!r:.200}")
        if not isinstance(value, IRI | rdf.Literal):
            raise SparqlError(f"an annotation value that isn't an IRI or a literal: {value!r:.200}")
        pairs.append((property_, _value(value)))
    return pairs


def _value(term: IRI | rdf.Literal) -> AnnotationValue:
    """Return an RDF term as an annotation's value."""
    if isinstance(term, IRI):
        return IRIValue(iri=term)
    # Validated, as `rdf.Literal` types its (checked) direction as any `str`.
    fields = {"lexical": term.value, "datatype": term.datatype, "language": term.language}
    return LiteralValue.model_validate(fields | {"direction": term.direction})


async def fetch_value_annotations(
    sparql: FusekiClient, dataset: str, iri: str, dataset_clause: str = ""
) -> ValueAnnotations:
    """Return the axiom annotations on an entity's annotations (`value_annotations`), in no particular order.

    Args:
        sparql: The Fuseki client.
        dataset: The dataset that serves the ontology.
        iri: The entity's IRI.
        dataset_clause: The dataset clause that makes the query see the imports (`browl.fuseki.dataset_clause`).

    Raises:
        SparqlError: The query failed, or returned a row with a term missing or of the wrong kind.
        RDFSyntaxError: The IRI isn't valid.
        TooManyAxiomAnnotationsError: The values carry more than `MAX_AXIOM_ANNOTATIONS` annotations, or they're longer
            than `MAX_ANNOTATIONS_CHARS`.
    """
    query = filled_query(_VALUE_ANNOTATIONS, dataset_clause, {"$entity": f"<{IRI(iri)}>"})
    try:
        result = await sparql.select(
            dataset, f"{query}LIMIT {MAX_AXIOM_ANNOTATIONS + 1}\n", max_chars=MAX_ANNOTATIONS_CHARS
        )
    except ResultTooLargeError as error:
        raise TooManyAxiomAnnotationsError(
            f"the axiom annotations are longer than {MAX_ANNOTATIONS_CHARS} characters"
        ) from error
    if len(result.rows) > MAX_AXIOM_ANNOTATIONS:
        raise TooManyAxiomAnnotationsError(f"the values carry more than {MAX_AXIOM_ANNOTATIONS} axiom annotations")
    return value_annotations(IRI(iri), result.rows)


def _term_key(term: Term) -> str:
    return term if isinstance(term, str) else repr(term)


def value_annotations(entity: IRI, rows: Iterable[Sequence[Term | None]]) -> ValueAnnotations:
    """Return the axiom annotations on an entity's annotations, from `sparql/entity-value-annotations.rq`'s rows.

    The reified axioms whose source is the entity give its values' annotations; the `owl:Annotation`s on a node, by
    their property and target, those of its annotations. Each node is placed once, on the source
    `browl.axioms.annotation_sources` keeps and its first property and target, so a cycle ends and nesting stops at
    `browl.axioms.MAX_ANNOTATION_DEPTH`; repeats are dropped, and so are blank values (anonymous individuals), as on
    the panel. The annotations of the entity's logical axioms come too, and are never looked up.

    Raises:
        SparqlError: A row has a term missing, or a property that isn't an IRI.
    """
    sources: dict[Term, list[Term]] = {}
    annotates: dict[Term, set[tuple[IRI, Term]]] = {}
    values: dict[Term, dict[tuple[IRI, IRI | rdf.Literal], None]] = {}
    for row in rows:
        node, source, about, target, property_, value = row
        if node is None or source is None or target is None or value is None:
            raise SparqlError(f"an axiom annotation's row with a term missing: {row!r:.200}")
        if not isinstance(about, IRI) or not isinstance(property_, IRI):
            raise SparqlError(f"an axiom annotation's row with a property that isn't an IRI: {row!r:.200}")
        sources.setdefault(node, []).append(source)
        annotates.setdefault(node, set()).add((about, target))
        if not isinstance(value, BlankNode):
            values.setdefault(node, {})[property_, value] = None
    children: dict[tuple[Term, IRI, Term], list[Term]] = {}
    for node, source in annotation_sources(sources, key=_term_key).items():
        about, target = min(annotates[node], key=lambda pair: (pair[0], _term_key(pair[1])))
        children.setdefault((source, about, target), []).append(node)

    def annotations_on(nodes: Iterable[Term]) -> tuple[owl.Annotation, ...]:
        found: dict[tuple[IRI, IRI | rdf.Literal], dict[owl.Annotation, None]] = {}
        for node in nodes:
            for property_, value in values.get(node, {}):
                nested = found.setdefault((property_, value), {})
                nested.update(dict.fromkeys(annotations_on(children.get((node, property_, value), ()))))
        return tuple(
            owl.Annotation(
                property=owl.Entity(kind=owl.EntityKind.ANNOTATION_PROPERTY, iri=property_),
                value=_node_value(value),
                annotations=tuple(nested),
            )
            for (property_, value), nested in sorted(
                found.items(), key=lambda item: (item[0][0], _term_key(item[0][1]))
            )
        )

    return {
        (about, _value(target)): annotations_on(nodes)
        for (source, about, target), nodes in children.items()
        if source == entity and isinstance(target, IRI | rdf.Literal)
    }


def _node_value(term: IRI | rdf.Literal) -> owl.IRIReference | owl.Literal:
    """Return an RDF term as an annotation node's value; a literal's base direction, which nodes don't hold, is lost."""
    if isinstance(term, IRI):
        return owl.IRIReference(iri=term)
    return owl.Literal(lexical=term.value, datatype=IRI(term.datatype), language=term.language)


def annotated(
    pairs: Iterable[tuple[IRI, AnnotationValue]], annotations: ValueAnnotations
) -> list[tuple[IRI, AnnotationValue]]:
    """Return (property, value) pairs, each value with the axiom annotations on it (`fetch_value_annotations`)."""
    return [
        (property_, value.model_copy(update={"annotations": found}))
        if (found := annotations.get((property_, value)))
        else (property_, value)
        for property_, value in pairs
    ]


def shown_annotations(annotations: Iterable[owl.Annotation], shown_name: Callable[[str], str]) -> list[AnnotationGroup]:
    """Return axiom annotations as a page shows them, after what they annotate (DESIGN.md §2.8, Axiom annotations).

    They're grouped by property, each value once with the union of its own annotations, sorted as the annotations
    panel sorts its values (`group_annotations`), but with no language ranking; blank values are left out.

    Args:
        annotations: The annotations (`browl.owl.Annotation`), from the OWLAPI or `value_annotations`.
        shown_name: The name the page shows for an IRI.
    """
    found: dict[tuple[IRI, AnnotationValue], dict[owl.Annotation, None]] = {}
    for annotation in annotations:
        value = _shown_value(annotation.value)
        if value is not None:
            found.setdefault((annotation.property.iri, value), {}).update(dict.fromkeys(annotation.annotations))
    return group_annotations(
        (
            (property_, value.model_copy(update={"annotations": tuple(nested)}))
            for (property_, value), nested in found.items()
        ),
        (),
        shown_name,
        (),
    )


def _shown_value(value: owl.Node) -> AnnotationValue | None:
    """Return an annotation node's value as an annotation's value, without annotations; None for a blank one."""
    if isinstance(value, owl.IRIReference):
        return IRIValue(iri=value.iri)
    if not isinstance(value, owl.Literal):
        return None
    language = value.language.lower() if value.language else None
    try:
        return LiteralValue(lexical=value.lexical, datatype=value.datatype, language=language)
    except ValidationError:  # a tag the OWLAPI took but isn't one, shown without it
        return LiteralValue(lexical=value.lexical, datatype=value.datatype)


def group_annotations(
    pairs: Iterable[tuple[IRI, AnnotationValue]],
    first: Sequence[str],
    shown_name: Callable[[str], str],
    ranking: Sequence[str],
) -> list[AnnotationGroup]:
    """Return annotations grouped by property, each value once, in the order the page shows them (DESIGN.md §2.8).

    The properties in `first` (the name properties) come first, in its order, then the others by the name the page
    shows for them, case-insensitively, then by IRI. A property's literals come before its IRIs; literals by their
    language's place in the ranking, then by text, then by language tag, datatype and direction, so the order doesn't
    depend on Fuseki's; IRIs as properties are.

    Args:
        pairs: The (property, value) pairs (`fetch_annotations`); repeats are dropped here, as the query has no
            `DISTINCT`.
        first: The IRIs of the properties that come first, best first.
        shown_name: The name the page shows for an IRI.
        ranking: The language ranking (`Settings.language_ranking`).
    """
    groups: dict[IRI, dict[AnnotationValue, None]] = {}
    for property_, value in pairs:
        groups.setdefault(property_, {})[value] = None
    position = {iri: index for index, iri in enumerate(first)}

    def property_key(iri: str) -> tuple[int, str, str]:
        return position.get(iri, len(position)), shown_name(iri).casefold(), iri

    @cache
    def rank(language: str | None) -> float:
        return language_rank(language, ranking)

    def value_key(value: AnnotationValue) -> tuple[int, float, str, str, str, str]:
        if isinstance(value, IRIValue):
            return 1, 0, shown_name(value.iri).casefold(), value.iri, "", ""
        language, direction = value.language or "", value.direction or ""
        return 0, rank(value.language), value.lexical, language, value.datatype, direction

    return [
        AnnotationGroup(property=iri, values=tuple(sorted(groups[iri], key=value_key)))
        for iri in sorted(groups, key=property_key)
    ]
