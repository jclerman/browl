"""The entities pre-caching hook (P-10): the `entity` and `hash_prefix` tables."""

import functools
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field

from browl.derived import EntityType
from browl.precache.core import PreCacheContext, batched, hook, sparql_file
from browl.rdf import Term
from browl.urls import URLMapper

logger = logging.getLogger(__name__)

_LOGGED_IRIS = 10  # the most reserved IRIs a warning lists
_LOGGED_IRI_CHARS = 300  # the most characters of each it shows, so a huge IRI can't give a huge log line

OWL = "http://www.w3.org/2002/07/owl#"

ENTITY_TYPES = {
    f"{OWL}Class": EntityType.CLASS,
    f"{OWL}ObjectProperty": EntityType.OBJECT_PROPERTY,
    f"{OWL}DatatypeProperty": EntityType.DATA_PROPERTY,
    f"{OWL}AnnotationProperty": EntityType.ANNOTATION_PROPERTY,
}
"""The entity types the entity list (`entity-list.rq`) has, by their OWL classes; the ontology itself comes from the
header (`entities`). Named individuals come later (U-18)."""


@hook("base_url", "reserved_prefix")
async def entities(context: PreCacheContext) -> None:
    """Fill `entity`, with each entity's URL path and fragment (`browl.urls.URLMapper`), and `hash_prefix`.

    The entities are the ontology itself, whose IRI the `meta` row holds, and every declared class and property, from
    one bulk query (`entity-list.rq`). The URLs are computed on the database's thread, off the event loop. IRIs whose
    native or proxied URL would be under the reserved prefix get the fallback URL, and a warning lists them (O-3).
    `display_name` and `curie` are left NULL, for the names hook (`browl.precache.names`).
    """
    settings = context.job.settings
    mapper = URLMapper(settings.base_url, settings.reserved_prefix)
    reserved = Reserved()
    ontology = await context.db.run(
        lambda connection: connection.execute("SELECT ontology_iri FROM meta").fetchone()[0]
    )
    if ontology is not None:
        await context.db.run(functools.partial(insert_entities, [(ontology, EntityType.ONTOLOGY)], mapper, reserved))
    async with context.bulk_select(sparql_file("entity-list.rq")) as stream:
        columns = tuple(stream.variables.index(name) for name in ("entity", "type"))
        async for batch in batched(stream):
            await context.db.run(functools.partial(_insert_listed, batch, columns, mapper, reserved))
    await context.db.create_indexes("entity")
    await context.db.run(fill_hash_prefix)
    await context.db.create_indexes("hash_prefix")
    if reserved.count:
        logger.warning(
            "%d entity IRIs would have URLs under the reserved prefix /%s, so they get fallback URLs: %r%s",
            reserved.count,
            settings.reserved_prefix,
            reserved.iris,
            " …" if reserved.count > len(reserved.iris) else "",
        )


@dataclass
class Reserved:
    """The entity IRIs whose URLs would be under the reserved prefix: how many, and the first few, cut short."""

    count: int = 0
    iris: list[str] = field(default_factory=list)


def _insert_listed(
    batch: list[tuple[Term | None, ...]],
    columns: tuple[int, ...],
    mapper: URLMapper,
    reserved: Reserved,
    connection: sqlite3.Connection,
) -> None:
    """Insert a batch of the entity list's rows (`insert_entities`), given its entity and type columns.

    Their terms are read as each row is inserted, on the database's thread rather than the event loop.
    """
    entity, kind = columns
    insert_entities(((str(row[entity]), ENTITY_TYPES[str(row[kind])]) for row in batch), mapper, reserved, connection)


def insert_entities(
    rows: Iterable[tuple[str, EntityType]], mapper: URLMapper, reserved: Reserved, connection: sqlite3.Connection
) -> None:
    """Insert entities with their URLs, counting in `reserved` those whose URLs would be under the reserved prefix."""
    values = []
    for iri, kind in rows:
        url = mapper(iri)
        if url.reserved:
            reserved.count += 1
            if len(reserved.iris) < _LOGGED_IRIS:
                reserved.iris.append(iri if len(iri) <= _LOGGED_IRI_CHARS else f"{iri[:_LOGGED_IRI_CHARS]}…")
        values.append((iri, kind, url.path, url.fragment))  # an EntityType is stored as TEXT, as a str is
    connection.executemany("INSERT INTO entity (iri, type, path, fragment) VALUES (?, ?, ?, ?)", values)


def fill_hash_prefix(connection: sqlite3.Connection) -> None:
    """Fill `hash_prefix` from `entity`: each path that hash entities' URLs have."""
    # In key order, from the entity_path index.
    connection.execute(
        "INSERT INTO hash_prefix SELECT DISTINCT path FROM entity WHERE fragment IS NOT NULL ORDER BY path"
    )
