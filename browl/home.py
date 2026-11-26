"""The ontology's home page (U-1, DESIGN.md §2.8, The home page).

Its metadata from the derived database's `meta` row, its entity and axiom counts from `type_count` and `axiom_count`,
and its prefixes from `prefix`; its triple counts and imports, the dropped ones with why, from the job state (P-8); and
its annotations from Fuseki, as an entity page's annotations panel reads them (`browl.overview.read_annotations`).
"""

import sqlite3

from fastapi import Request
from pydantic import BaseModel, ConfigDict

from browl.derived import AxiomKind, EntityType, Meta, read_axiom_counts, read_type_counts
from browl.jobs import JobState
from browl.jobtypes import ImportResult
from browl.links import EntityLink, read_entity_links
from browl.loading import dataset_name
from browl.overview import AnnotationsPanel, annotation_iris, annotations_panel, read_annotations
from browl.pages import link_root, read_derived
from browl.settings import Settings
from browl.usages import ready_store

MAX_PREFIXES = 1_000
"""The most prefixes the page lists; an ontology file may declare many more, and the page says how many."""


class Home(BaseModel):
    """The home page's data.

    Attributes:
        meta: The ontology's IRIs, title, version info, reasoner and load time.
        entity_counts: How many entities have each type, the ontology's own left out; a type with none has no key.
        axiom_counts: How many of the ontology's own axioms, without its imports, there are of each kind.
        prefixes: The prefixes the site's CURIEs use, name → namespace, by name: the first `MAX_PREFIXES` of them.
        prefix_count: How many prefixes there are in all.
        triples: How many triples the ontology holds, without its imports; None if unknown.
        imports: The ontology's imports, direct and indirect, each loaded, with its triple count, or dropped, with why;
            None if unknown.
        annotations: The ontology's annotations panel.
        links: Each IRI the annotations mention, their properties included (`browl.links.read_entity_links`).
    """

    model_config = ConfigDict(frozen=True)

    meta: Meta
    entity_counts: dict[EntityType, int]
    axiom_counts: dict[AxiomKind, int]
    prefixes: dict[str, str]
    prefix_count: int
    triples: int | None
    imports: tuple[ImportResult, ...] | None
    annotations: AnnotationsPanel
    links: dict[str, EntityLink]

    @property
    def dropped_imports(self) -> list[ImportResult]:
        """Return the imports that couldn't be loaded, so the site leaves out what they declare."""
        return [result for result in self.imports or () if result.dropped]

    @property
    def import_triples(self) -> int | None:
        """Return how many triples the loaded imports hold in all; None if none were loaded, or a count is unknown."""
        loaded = [result.triples for result in self.imports or () if not result.dropped]
        if not loaded or None in loaded:
            return None
        return sum(count for count in loaded if count is not None)


async def read_home(request: Request, state: JobState, meta: Meta) -> Home:
    """Return the home page's data, from the store and derived database of the job state's ready generation.

    A failed annotations query fails only the annotations panel, as on an entity page.

    Args:
        request: The request.
        state: The job state the page read its entities with, so all its data is of one generation.
        meta: The ready generation's `meta` row, which has an ontology IRI, as the home page is the ontology's.

    Raises:
        NotCurrentError: No generation is ready, or its derived database isn't current (`browl.pages.read_derived`).
    """
    assert meta.ontology_iri is not None  # the home page is the ontology's
    generation, clause = ready_store(state)
    annotations = await read_annotations(request, dataset_name(generation), meta.ontology_iri, clause)
    mentioned = annotation_iris(annotations.pairs)
    settings: Settings = request.app.state.settings
    prefix, root = settings.reserved_prefix, link_root(request.scope)

    def read(
        connection: sqlite3.Connection,
    ) -> tuple[dict[EntityType, int], dict[AxiomKind, int], tuple[dict[str, str], int], dict[str, EntityLink]]:
        return (
            read_type_counts(connection),
            read_axiom_counts(connection),
            _read_prefixes(connection),
            read_entity_links(connection, mentioned, prefix, root),
        )

    counts, axioms, (prefixes, prefix_count), links = await read_derived(request, read, state)
    counts.pop(EntityType.ONTOLOGY, None)
    # The job state's counts and imports are the latest job's: the ready generation's unless another job has started
    # since the page read the state (`browl.usages.ready_store`), when they're left out rather than shown wrongly.
    current = state.generation == generation
    return Home(
        meta=meta,
        entity_counts=counts,
        axiom_counts=axioms,
        prefixes=prefixes,
        prefix_count=prefix_count,
        triples=state.triples if current else None,
        imports=state.imports if current else None,
        annotations=annotations_panel(annotations, links, settings),
        links=links,
    )


def _read_prefixes(connection: sqlite3.Connection) -> tuple[dict[str, str], int]:
    """Return the first `MAX_PREFIXES` prefixes of the `prefix` table, by name, and how many it holds in all."""
    rows = connection.execute(
        "SELECT prefix, namespace FROM prefix ORDER BY prefix COLLATE NOCASE, prefix LIMIT ?", (MAX_PREFIXES,)
    )
    prefixes = dict(rows.fetchall())
    count = len(prefixes)
    if count == MAX_PREFIXES:
        count = connection.execute("SELECT count(*) FROM prefix").fetchone()[0]
    return prefixes, count
