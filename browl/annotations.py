"""An entity's annotations, fetched from Fuseki and grouped by property, as typed data (DESIGN.md §2.8).

`fetch_annotations` reads the entity's annotation assertions (`sparql/entity-annotations.rq`) and `group_annotations`
groups their values by property, in the order the page shows them. Templates make the HTML
(`templates/annotations.html`).
"""

from collections.abc import Callable, Iterable, Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from browl import rdf
from browl.axioms import filled_query
from browl.fuseki import FusekiClient, SparqlError
from browl.names import language_rank
from browl.precache.core import PREFIXES, sparql_file
from browl.rdf import IRI

_ANNOTATIONS = PREFIXES + sparql_file("entity-annotations.rq")

MAX_ANNOTATIONS = 1_000
"""The most annotation values an entity may have for its page to show them: more, and the page says so, as for
`browl.axioms.MAX_AXIOMS`."""


class TooManyAnnotationsError(Exception):
    """An entity has more than `MAX_ANNOTATIONS` annotation values."""


class IRIValue(BaseModel):
    """An annotation's value that is an IRI.

    Attributes:
        iri: The IRI.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["iri"] = "iri"
    iri: IRI


class LiteralValue(BaseModel):
    """An annotation's value that is a literal.

    Attributes:
        lexical: Its lexical form.
        datatype: Its datatype's IRI.
        language: Its language tag, lowercased, if it has one.
        direction: Its base direction, "ltr" or "rtl", if it has one (RDF 1.2).
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["literal"] = "literal"
    lexical: str
    datatype: IRI
    language: str | None = None
    direction: str | None = None


type AnnotationValue = Annotated[IRIValue | LiteralValue, Field(discriminator="type")]


class Annotations(BaseModel):
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
        TooManyAnnotationsError: The entity has more than `MAX_ANNOTATIONS` annotation values.
    """
    query = filled_query(_ANNOTATIONS, dataset_clause, {"$entity": f"<{IRI(iri)}>"})
    result = await sparql.select(dataset, f"{query}LIMIT {MAX_ANNOTATIONS + 1}\n")
    if len(result.rows) > MAX_ANNOTATIONS:
        raise TooManyAnnotationsError(f"the entity has more than {MAX_ANNOTATIONS} annotation values")
    pairs: list[tuple[IRI, AnnotationValue]] = []
    for property_, value in result.rows:
        if not isinstance(property_, IRI):
            raise SparqlError(f"an annotation property that isn't an IRI: {property_!r:.200}")
        if isinstance(value, IRI):
            pairs.append((property_, IRIValue(iri=value)))
        elif isinstance(value, rdf.Literal):
            literal = LiteralValue(
                lexical=value.value, datatype=IRI(value.datatype), language=value.language, direction=value.direction
            )
            pairs.append((property_, literal))
        else:
            raise SparqlError(f"an annotation value that isn't an IRI or a literal: {value!r:.200}")
    return pairs


def group_annotations(
    pairs: Iterable[tuple[IRI, AnnotationValue]],
    first: Sequence[str],
    shown_name: Callable[[str], str],
    ranking: Sequence[str],
) -> list[Annotations]:
    """Return annotations grouped by property, each value once, in the order the page shows them (DESIGN.md §2.8).

    The properties in `first` (the name properties) come first, in its order, then the others by the name the page
    shows for them, case-insensitively. A property's literals come before its IRIs; literals by their language's place
    in the ranking, then by text; IRIs by text.

    Args:
        pairs: The (property, value) pairs (`fetch_annotations`).
        first: The IRIs of the properties that come first, best first.
        shown_name: The name the page shows for a property, by its IRI.
        ranking: The language ranking (`Settings.language_ranking`).
    """
    groups: dict[IRI, dict[AnnotationValue, None]] = {}
    for property_, value in pairs:
        groups.setdefault(property_, {})[value] = None
    position = {iri: index for index, iri in enumerate(first)}

    def property_key(iri: str) -> tuple[int, str, str]:
        name = shown_name(iri)
        return position.get(iri, len(position)), name.casefold(), iri

    def value_key(value: AnnotationValue) -> tuple[int, float, str]:
        if isinstance(value, IRIValue):
            return 1, 0, value.iri
        return 0, language_rank(value.language, ranking), value.lexical

    return [
        Annotations(property=iri, values=tuple(sorted(groups[iri], key=value_key)))
        for iri in sorted(groups, key=property_key)
    ]
