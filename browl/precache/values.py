"""The annotation values pre-caching hook (P-42): the `annotation_value`, `search_property` and `sub_property` tables.

Every literal value of each annotation property, for search (DESIGN.md §2.6, Every annotation property), and each
property's kind: name-like, searched by the lane index, or prose, for a full-text index (P-46).
"""

import bisect
import functools
import itertools
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from browl.names import name_property_iris
from browl.precache.core import PreCacheContext, batched, hook
from browl.rdf import OWL, RDFS, XSD_STRING, Literal, Term

IAO = "http://purl.obolibrary.org/obo/IAO_"
SKOS = "http://www.w3.org/2004/02/skos/core#"
OBO_IN_OWL = "http://www.geneontology.org/formats/oboInOwl#"
DC = "http://purl.org/dc/elements/1.1/"
DCTERMS = "http://purl.org/dc/terms/"

BUILT_IN = (
    f"{RDFS}label",
    f"{RDFS}comment",
    f"{RDFS}seeAlso",
    f"{RDFS}isDefinedBy",
    f"{OWL}deprecated",
    f"{OWL}versionInfo",
    f"{OWL}priorVersion",
    f"{OWL}backwardCompatibleWith",
    f"{OWL}incompatibleWith",
)
"""OWL 2's built-in annotation properties, which needn't be declared: NCBITaxon doesn't declare `rdfs:label`."""

LABEL_PROPERTIES = (
    f"{RDFS}label",
    f"{SKOS}prefLabel",
    f"{SKOS}altLabel",
    f"{SKOS}hiddenLabel",
    f"{OBO_IN_OWL}hasExactSynonym",
    f"{OBO_IN_OWL}hasRelatedSynonym",
    f"{OBO_IN_OWL}hasBroadSynonym",
    f"{OBO_IN_OWL}hasNarrowSynonym",
    f"{IAO}0000111",  # editor preferred term
    f"{IAO}0000118",  # alternative label
)
"""The built-in label and synonym properties: name-like, and searched by default, with the name properties."""

PROSE_PROPERTIES = (
    f"{IAO}0000115",  # definition
    f"{IAO}0000112",  # example of usage
    f"{IAO}0000116",  # editor note
    f"{IAO}0000232",  # curator note
    f"{IAO}0000600",  # elucidation
    f"{RDFS}comment",
    f"{SKOS}definition",
    f"{SKOS}note",
    f"{SKOS}scopeNote",
    f"{SKOS}editorialNote",
    f"{SKOS}historyNote",
    f"{SKOS}changeNote",
    f"{SKOS}example",
    f"{DC}description",
    f"{DCTERMS}description",
    f"{DCTERMS}abstract",
)
"""The built-in prose properties: definitions, comments and notes."""

PROSE_WORDS = 8
"""A property not otherwise classified is prose if its median value has at least this many words. On HPO, Uberon and
NCBITaxon, name-like properties have medians of at most 5 and prose ones 8 to 46 (research/P-42)."""

NAME_REUSE = 2
"""A name-like property is searched by default if its values are on fewer than this many entities each, on average:
names, IDs and synonyms tell entities apart, while categories, sources, dates of change and creators repeat. An entity
is an `entity` row, so a punned IRI counts once per type, and a value is its lexical form, whatever its language."""

ANNOTATION_VALUES = """SELECT ?entity ?property ?value
WHERE {{
  {{ VALUES ?property {{ {properties} }} }}
  UNION {{ ?property rdf:type owl:AnnotationProperty }}
  ?entity ?property ?value .
  FILTER (isIRI(?entity) && isLiteral(?value) && (lang(?value) != "" || datatype(?value) = <{string}>))
}}"""
"""Every string or language-tagged value, on an IRI, of each declared annotation property and of the built-in and name
properties (`{properties}`: their IRIs, in `<>`). A property both listed and declared comes twice; `fill_staged`
keeps each value once."""

SUB_PROPERTIES = """SELECT ?sub ?property
WHERE {
  ?sub rdfs:subPropertyOf+ ?property .
  FILTER (isIRI(?sub) && isIRI(?property) && ?sub != ?property)
}"""
"""Every property's sub-properties, transitively, through a property path, which reaches each once however they
cycle."""


@hook("name_properties", "extra_prefixes")
async def annotation_values(context: PreCacheContext) -> None:
    """Fill `annotation_value`, `search_property` and `sub_property` (DESIGN.md §2.6, Every annotation property).

    The values come from one bulk query (`ANNOTATION_VALUES`), over the imports too; a value on an IRI that isn't a
    declared entity's is left out, and one asserted with several properties is stored once for each, under the property
    it's asserted with: entailment is search's, at query time. Each property is classified (`classify`).
    """
    names = name_property_iris(context.job.settings)
    words: defaultdict[str, Counter[int]] = defaultdict(Counter)
    await context.db.run(create_staging)
    listed = " ".join(f"<{iri}>" for iri in dict.fromkeys([*BUILT_IN, *names]))
    async with context.bulk_select(ANNOTATION_VALUES.format(properties=listed, string=XSD_STRING)) as stream:
        columns = tuple(stream.variables.index(name) for name in ("entity", "property", "value"))
        async for batch in batched(stream):
            await context.db.run(functools.partial(_stage, batch, columns, words))
    async with context.bulk_select(SUB_PROPERTIES) as stream:
        columns = tuple(stream.variables.index(name) for name in ("sub", "property"))
        async for batch in batched(stream):
            await context.db.run(functools.partial(_stage_sub_properties, batch, columns))
    await context.db.run(functools.partial(fill_staged, names, words))
    await context.db.create_indexes("search_property")
    await context.db.create_indexes("annotation_value")


@dataclass(frozen=True)
class Stats:
    """A property's values, as `classify` weighs them.

    Attributes:
        words: How many values have each word count.
        values: How many values it has, on declared entities.
        unique: How many distinct values (lexical forms) those are.
        uses: How many (entity, lexical form) pairs those are: a value in two languages on one entity is one use.
    """

    words: Counter[int]
    values: int
    unique: int
    uses: int


def classify(
    names: Sequence[str], stats: Mapping[str, Stats], supers: Mapping[str, Collection[str]]
) -> dict[str, tuple[bool, bool]]:
    """Return each property's kind, as (prose, searched by default), by DESIGN.md §2.6, Which is which.

    A property with values: a name property is name-like and searched by default; else a built-in prose property is
    prose; else a built-in label or synonym property (`LABEL_PROPERTIES`) is name-like and searched by default; else a
    sub-property of a name or label property is name-like; else a property is prose if its median value has at least
    `PROSE_WORDS` words, and name-like if not. A name-like property is searched by default if its values are on fewer
    than `NAME_REUSE` entities each, on average: so names, IDs and synonyms are, whatever the ontology calls them.

    A property without values is classified too if it's a name, label or synonym property, so a default search reaches
    its sub-properties' values, or a super-property of one with values, so it can be chosen for them: name-like unless
    each of those is prose, and searched by default if each of those is, so choosing it adds none that isn't.

    Args:
        names: The name properties' IRIs.
        stats: Each property with values' statistics, by IRI.
        supers: Each property's super-properties, transitively, by IRI.
    """
    labels = {*names, *LABEL_PROPERTIES}
    kinds: dict[str, tuple[bool, bool]] = {}
    for iri, found in stats.items():
        if iri in labels:
            kinds[iri] = (False, True)
        elif iri in PROSE_PROPERTIES or (
            not labels.intersection(supers.get(iri, ())) and median(found.words) >= PROSE_WORDS
        ):
            kinds[iri] = (True, False)
        else:
            kinds[iri] = (False, found.uses < NAME_REUSE * found.unique)
    ancestors: defaultdict[str, list[tuple[bool, bool]]] = defaultdict(list)
    for iri, kind in list(kinds.items()):
        for ancestor in supers.get(iri, ()):
            ancestors[ancestor].append(kind)
    for iri in labels:
        kinds.setdefault(iri, (False, True))
    for iri, below in ancestors.items():
        prose = iri in PROSE_PROPERTIES or all(sub_prose for sub_prose, _ in below)
        kinds.setdefault(iri, (prose, not prose and all(default for _, default in below)))
    return kinds


def median(counts: Counter[int]) -> float:
    """Return the median of the numbers a histogram counts (how many times each occurs), without listing them.

    Examples:
        >>> median(Counter({1: 3, 10: 1}))
        1.0
        >>> median(Counter({2: 1, 5: 1}))
        3.5
    """
    total = counts.total()
    if not total:
        raise ValueError("no numbers")
    ordered = sorted(counts)
    cumulative = list(itertools.accumulate(counts[number] for number in ordered))

    def at(index: int) -> int:
        return ordered[bisect.bisect_right(cumulative, index)]

    return (at((total - 1) // 2) + at(total // 2)) / 2


def create_staging(connection: sqlite3.Connection) -> None:
    """Create the temporary tables the hook's queries' rows are staged in: `value_staging` and `sub_staging`."""
    connection.execute(
        "CREATE TEMP TABLE value_staging (iri TEXT NOT NULL, property TEXT NOT NULL, value TEXT NOT NULL,"
        " language TEXT)"
    )
    connection.execute("CREATE TEMP TABLE sub_staging (sub TEXT NOT NULL, property TEXT NOT NULL)")


def _stage(
    batch: list[tuple[Term | None, ...]],
    columns: tuple[int, ...],
    words: defaultdict[str, Counter[int]],
    connection: sqlite3.Connection,
) -> None:
    """Stage a batch of the values query's rows, counting each property's values' words."""
    entity, prop, value = columns
    rows = []
    for row in batch:
        literal = row[value]
        assert isinstance(literal, Literal)  # the query's filter
        iri = str(row[prop])
        words[iri][len(literal.value.split())] += 1
        rows.append((str(row[entity]), iri, literal.value, literal.language))
    connection.executemany("INSERT INTO value_staging VALUES (?, ?, ?, ?)", rows)


def _stage_sub_properties(
    batch: list[tuple[Term | None, ...]], columns: tuple[int, ...], connection: sqlite3.Connection
) -> None:
    sub, prop = columns
    connection.executemany("INSERT INTO sub_staging VALUES (?, ?)", [(str(row[sub]), str(row[prop])) for row in batch])


def fill_staged(names: Sequence[str], words: Mapping[str, Counter[int]], connection: sqlite3.Connection) -> None:
    """Fill the tables from the staged rows: values by entity id, once for each type an IRI is declared with (punning).

    A property is kept if it has values on declared entities, is searched by default (a default search reaches its
    sub-properties' values even if it has none), or is a super-property of one with values (`classify`).

    Args:
        names: The name properties' IRIs.
        words: Each property's values' word counts, by IRI, as staged.
        connection: The derived database, with the staging tables filled.
    """
    supers: defaultdict[str, set[str]] = defaultdict(set)
    for sub, prop in connection.execute("SELECT sub, property FROM sub_staging"):
        supers[sub].add(prop)
    every = dict.fromkeys([*words, *names, *LABEL_PROPERTIES, *(a for sub in words for a in supers.get(sub, ()))])
    connection.executemany(
        "INSERT INTO search_property (iri, prose, by_default, value_count) VALUES (?, 0, 0, 0)",
        [(iri,) for iri in sorted(every)],
    )
    connection.execute(
        "INSERT INTO annotation_value (entity, property, value, language)"
        " SELECT entity.id, p.id, s.value, s.language"
        " FROM value_staging AS s JOIN entity ON entity.iri = s.iri JOIN search_property AS p ON p.iri = s.property"
        " GROUP BY entity.id, p.id, s.value, s.language ORDER BY entity.id"
    )
    stats = {
        iri: Stats(words[iri], values, unique, uses)
        for iri, values, unique, uses in connection.execute(
            "SELECT p.iri, sum(v.languages), count(DISTINCT v.value), count(*) FROM (SELECT property, value,"
            "  count(*) AS languages FROM annotation_value GROUP BY property, entity, value) AS v"
            " JOIN search_property AS p ON p.id = v.property GROUP BY v.property"
        )
    }
    kinds = classify(names, stats, supers)
    connection.executemany(
        "UPDATE search_property SET prose = ?, by_default = ?, value_count = ? WHERE iri = ?",
        [(prose, default, stats[iri].values if iri in stats else 0, iri) for iri, (prose, default) in kinds.items()],
    )
    connection.executemany("DELETE FROM search_property WHERE iri = ?", [(iri,) for iri in every if iri not in kinds])
    connection.execute(
        "INSERT OR IGNORE INTO sub_property (property, sub)"
        " SELECT p.id, s.id FROM sub_staging JOIN search_property AS p ON p.iri = sub_staging.property"
        " JOIN search_property AS s ON s.iri = sub_staging.sub"
    )
    connection.execute("DROP TABLE value_staging")
    connection.execute("DROP TABLE sub_staging")
