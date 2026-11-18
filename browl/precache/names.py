"""The names pre-caching hook (P-11): the `prefix` and `name_value` tables, and each entity's CURIE and display name."""

import functools
import sqlite3
from collections.abc import Mapping, Sequence

from browl.names import BUILTIN_PREFIXES, language_rank, name_property_iris, normalize
from browl.precache.core import PreCacheContext, batched, hook
from browl.rdf import Literal, Term

NAME_VALUES = """SELECT ?entity ?name ?property ?value
WHERE {{
  VALUES ?name {{ {properties} }}
  ?property rdfs:subPropertyOf* ?name .
  ?entity ?property ?value .
  FILTER (isIRI(?entity) && isLiteral(?value))
}}"""
"""Every value of each name property (`{properties}`: their IRIs, in `<>`) and of its sub-properties, transitively
(rdfs5, rdfs7), through a property path rather than an RDFS-inferring dataset (DESIGN.md §2.5). The path's zero-length
step gives the name property itself, and it reaches each property once, however the sub-properties cycle."""

_LAST_CHAR = "\U0010ffff"  # sorts after any other character, as UTF-8 bytes too


@hook("name_properties", "language_ranking", "extra_prefixes")
async def names(context: PreCacheContext) -> None:
    """Fill `prefix` and `name_value`, then set each entity's CURIE and display name (DESIGN.md §2.7).

    The prefixes are the built-in ones, overridden by those the ontology file declares, overridden by `EXTRA_PREFIXES`
    (O-11). The name values come from one bulk query (`NAME_VALUES`), over the imports too; an IRI that isn't a
    declared entity's is left out. A value under a name property both directly and through a sub-property is stored
    once, as direct.
    """
    settings = context.job.settings
    prefixes = BUILTIN_PREFIXES | context.job.prefixes | settings.extra_prefixes
    await context.db.run(functools.partial(_store_prefixes, prefixes))
    await context.db.run(functools.partial(_set_curies, prefixes, settings.extra_prefixes, context.job.prefixes))

    properties = name_property_iris(settings)
    await context.db.run(_create_staging)
    if properties:
        query = NAME_VALUES.format(properties=" ".join(f"<{iri}>" for iri in properties))
        async with context.bulk_select(query) as stream:
            columns = tuple(stream.variables.index(name) for name in ("entity", "name", "property", "value"))
            async for batch in batched(stream):
                await context.db.run(functools.partial(_stage, batch, columns))
    await context.db.run(_store_name_values)
    await context.db.create_indexes("name_value")
    await context.db.run(functools.partial(_set_display_names, properties, settings.language_ranking))


def _store_prefixes(prefixes: Mapping[str, str], connection: sqlite3.Connection) -> None:
    connection.executemany("INSERT INTO prefix (prefix, namespace) VALUES (?, ?)", sorted(prefixes.items()))


def _set_curies(
    prefixes: Mapping[str, str],
    configured: Mapping[str, str],
    declared: Mapping[str, str],
    connection: sqlite3.Connection,
) -> None:
    """Set each entity's CURIE, from the longest namespace that's a proper prefix of its IRI.

    Of several prefixes for that namespace, a configured one is taken first, then one the ontology declares, then a
    named one rather than the default (empty) prefix, then the first in order. Each prefix updates the entities in its
    namespace's range of the IRI index, so the IRIs aren't scanned once per prefix.
    """

    def preference(item: tuple[str, str]) -> tuple[int, bool, bool, bool, str]:
        name, namespace = item
        return (-len(namespace), name not in configured, name not in declared, name == "", name)

    for name, namespace in sorted(prefixes.items(), key=preference):
        connection.execute(
            "UPDATE entity SET curie = ? || substr(iri, ?) WHERE iri > ? AND iri < ? AND curie IS NULL",
            (f"{name}:", len(namespace) + 1, namespace, namespace + _LAST_CHAR),
        )


def _create_staging(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TEMP TABLE name_staging (iri TEXT NOT NULL, property TEXT NOT NULL, value TEXT NOT NULL,"
        " normalized TEXT NOT NULL, language TEXT, direct INTEGER NOT NULL)"
    )


def _stage(batch: list[tuple[Term | None, ...]], columns: tuple[int, ...], connection: sqlite3.Connection) -> None:
    """Stage a batch of the name values query's rows, keyed by IRI, normalizing each value on the database's thread."""
    entity, name, prop, value = columns
    rows = []
    for row in batch:
        literal = row[value]
        assert isinstance(literal, Literal)  # the query's filter
        direct = row[prop] == row[name]
        rows.append(
            (str(row[entity]), str(row[name]), literal.value, normalize(literal.value), literal.language, direct)
        )
    connection.executemany("INSERT INTO name_staging VALUES (?, ?, ?, ?, ?, ?)", rows)


def _store_name_values(connection: sqlite3.Connection) -> None:
    """Fill `name_value` from the staged rows, by entity id: once for each type an IRI is declared with (punning)."""
    connection.execute(
        "INSERT INTO name_value (entity, property, value, normalized, language, direct)"
        " SELECT entity.id, s.property, s.value, s.normalized, s.language, max(s.direct)"
        " FROM name_staging AS s JOIN entity ON entity.iri = s.iri"
        " GROUP BY entity.id, s.property, s.value, s.language ORDER BY entity.id"
    )
    connection.execute("DROP TABLE name_staging")


def _set_display_names(properties: Sequence[str], ranking: Sequence[str], connection: sqlite3.Connection) -> None:
    """Set each entity's display name (§2.7): its best name value, else its CURIE, else its IRI.

    Values rank by property position, then language (`browl.names.language_rank`), then direct before entailed, then
    shorter, then lexical order (by code point). The ontology's display name is its title (`meta.title`) if it has one.
    """
    languages = [row[0] for row in connection.execute("SELECT DISTINCT language FROM name_value")]
    connection.execute("CREATE TEMP TABLE property_rank (property TEXT PRIMARY KEY, rank INTEGER NOT NULL)")
    connection.executemany("INSERT INTO property_rank VALUES (?, ?)", [(iri, n) for n, iri in enumerate(properties)])
    connection.execute("CREATE TEMP TABLE language_rank (language TEXT, rank REAL NOT NULL)")
    connection.executemany(
        "INSERT INTO language_rank VALUES (?, ?)", [(tag, language_rank(tag, ranking)) for tag in languages]
    )
    connection.execute(
        "UPDATE entity SET display_name = best.value FROM ("
        " SELECT entity, value, row_number() OVER (PARTITION BY entity ORDER BY p.rank, l.rank, direct DESC,"
        "  length(value), value) AS place"
        " FROM name_value JOIN property_rank AS p USING (property)"
        " JOIN language_rank AS l ON l.language IS name_value.language"
        ") AS best WHERE best.place = 1 AND entity.id = best.entity"
    )
    connection.execute(
        "UPDATE entity SET display_name = (SELECT title FROM meta)"
        " WHERE type = 'ontology' AND (SELECT title FROM meta) IS NOT NULL"
    )
    connection.execute("UPDATE entity SET display_name = coalesce(curie, iri) WHERE display_name IS NULL")
    connection.execute("DROP TABLE property_rank")
    connection.execute("DROP TABLE language_rank")
