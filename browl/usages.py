"""An entity's usages, and their count: the axioms in which other entities refer to it (U-26, DESIGN.md §2.8).

Usages come from three sources (`UsageSource`): triples with the entity as object, triples with it as predicate, and
the blank-node owner map's roots that are its usages (`browl.precache.owner_map`). Each has at most one subject: the
triple's subject, or the root's owner; a root with no owner, such as a general class axiom that mentions a property,
has none. Axioms with no single subject aren't a class's usages, as its Overview lists them.

The count is of those usages, without fetching them: Fuseki's counts of the first two sources, not filtered on their
subjects (`usage-count.rq`), less the entity's triples about itself and the triples with a blank subject (the owner
map's `usage_blank`), plus the roots. Counts that take longer than `CACHED_COUNT_S` are kept per generation
(`UsageCounts`).

Blank nodes the owner map doesn't hold, such as anonymous individuals (U-18), are in the count but not the usages: an
axiom with one as subject, or within one, is counted and not listed.
"""

import logging
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from browl.derived import DerivedReader, NotCurrentError, UsageRootKind
from browl.fuseki import UNION_FROM, FusekiClient, SparqlError, dataset_clause, filled_query
from browl.jobs import JobState
from browl.loading import dataset_name
from browl.precache.core import PREFIXES, has_loaded_imports, logged_iri, sparql_file
from browl.precache.usage_table import Source
from browl.rdf import IRI, BlankNode, Literal, Term, ntriples_term
from browl.tasks import task_group
from browl.usage_tables import TableNotReadyError, UsageTables

logger = logging.getLogger(__name__)

MAX_STREAMED_USAGES = 20_000
"""The most usages `read_usages` reads by default: T, above which the Usage tab waits for the usage table (S-7)."""

CACHED_COUNT_S = 0.1
"""How long a count must take to be kept in memory (`UsageCounts`): 13 of NCBITaxon's entities, none of Uberon's or
HPO's, take longer (S-7)."""

MAX_KEPT_COUNTS = 1000
"""The most counts `UsageCounts` keeps; the oldest kept goes first. A slow Fuseki makes every count slow."""

_COUNT = PREFIXES + sparql_file("usage-count.rq")
_OBJECT = PREFIXES + sparql_file("usage-object.rq")
_PREDICATE = PREFIXES + sparql_file("usage-predicate.rq")
_VALUES = PREFIXES + sparql_file("usage-values.rq")
_ENTITY = "$entity"
_NO_SUBJECTS = "VALUES ?subject { }"


class TooManyUsagesError(Exception):
    """An entity has more usages than `read_usages` was asked to read."""


class UsageSource(StrEnum):
    """Where a usage was found.

    Attributes:
        OBJECT: A triple with the entity as object (`?s ?p <entity>`).
        PREDICATE: A triple with the entity as predicate (`?s <entity> ?o`).
        ROOT: A root of the blank-node owner map that names the entity (`usage_root`).
    """

    OBJECT = "object"
    PREDICATE = "predicate"
    ROOT = "root"


@dataclass(frozen=True, slots=True)
class UsageRoot:
    """A root of the blank-node owner map (`usage_root`).

    Attributes:
        id: Its number in `usage_root`.
        kind: What it is.
        label: Its blank node's label in the store, which a query can name as `<_:label>`.
    """

    id: int
    kind: UsageRootKind
    label: BlankNode


@dataclass(frozen=True, slots=True)
class Usage:
    """An axiom in which another entity refers to this one.

    A `PREDICATE` usage is known by its place among its subject's values of the property (`index`), not by its
    object, so reading a property's usages never holds their values, which may be long literals; a page reads only
    its own (`read_values`).

    Attributes:
        source: Where it was found.
        subject: The IRI whose axiom it is: the triple's subject or the root's owner; None for a root with no owner.
        predicate: An `OBJECT` triple's predicate; a `ROOT` triple's predicate, or a reified axiom's
            `owl:annotatedProperty`; else None.
        index: A `PREDICATE` triple's place, from 0, among its subject's values of the property, in `read_values`'s
            order; else 0.
        root: A `ROOT` usage's root; else None.
    """

    source: UsageSource
    subject: IRI | None
    predicate: IRI | None = None
    index: int = 0
    root: UsageRoot | None = None


@dataclass(frozen=True)
class UsageStore:
    """Where a generation's usages are read: its Fuseki dataset and its derived database.

    Attributes:
        sparql: The Fuseki client.
        derived: The derived databases' reader.
        generation: The generation.
        dataset_clause: The dataset clause that makes queries see the imports (`browl.fuseki.dataset_clause`), as the
            owner map does.
        tables: The usage tables, which serve counts and pages once the generation's is ready; None if never.
    """

    sparql: FusekiClient
    derived: DerivedReader
    generation: int
    dataset_clause: str
    tables: UsageTables | None = None

    @property
    def dataset(self) -> str:
        """Return the generation's dataset."""
        return dataset_name(self.generation)

    def query(self, query: str, iri: str, limit: int | None = None) -> str:
        """Return a query for an entity, with the dataset clause, and a limit if one's given.

        Raises:
            RDFSyntaxError: The IRI isn't valid.
        """
        filled = filled_query(query, self.dataset_clause, {_ENTITY: f"<{IRI(iri)}>"})
        return filled if limit is None else f"{filled}LIMIT {limit}\n"


def ready_store(state: JobState) -> tuple[int, str]:
    """Return a job state's ready generation, and the dataset clause that makes its queries see its imports.

    Raises:
        NotCurrentError: No generation is ready.
    """
    generation = state.ready_generation
    if generation is None:
        raise NotCurrentError("no generation is ready")
    # Pages are served only once the latest job is ready, so its imports are the ready generation's; if a new job has
    # started meanwhile, seeing the imports' graphs too is always right, if slower.
    clause = dataset_clause(has_loaded_imports(state.imports)) if state.generation == generation else UNION_FROM
    return generation, clause


class UsageCounts:
    """Usage counts kept in memory, for the newest generation asked for, at most `MAX_KEPT_COUNTS` of them."""

    def __init__(self) -> None:
        """Hold none yet."""
        self._generation: int | None = None
        self._counts: dict[str, int] = {}

    def get(self, generation: int, iri: str) -> int | None:
        """Return an entity's count, if it's kept for the generation."""
        return self._counts.get(iri) if generation == self._generation else None

    def keep(self, generation: int, iri: str, count: int) -> None:
        """Keep an entity's count, unless the generation is older than the newest asked for; a newer one's replace all.

        The oldest kept is dropped to make room.
        """
        if self._generation is None or generation > self._generation:
            self._generation = generation
            self._counts = {}
        elif generation < self._generation:
            return
        if iri not in self._counts and len(self._counts) >= MAX_KEPT_COUNTS:
            del self._counts[next(iter(self._counts))]
        self._counts[iri] = count


async def read_usages(store: UsageStore, iri: str, limit: int = MAX_STREAMED_USAGES) -> list[Usage]:
    """Return an entity's usages, from the three sources, in no particular order.

    Args:
        store: Where to read them.
        iri: The entity's IRI. One that isn't in `entity` has only the triples' usages.
        limit: The most to read.

    Raises:
        TooManyUsagesError: The entity has more than `limit` usages.
        SparqlError: A query failed.
        RDFSyntaxError: The IRI isn't valid.
        NotCurrentError: The generation's derived database isn't current.
    """
    async with task_group() as group:
        objects = group.create_task(_object_usages(store, iri, limit + 1))
        predicates = group.create_task(_predicate_usages(store, iri, limit + 1))
        roots = group.create_task(store.derived.read(store.generation, lambda c: _root_usages(c, iri, limit + 1)))
    usages = [*objects.result(), *predicates.result(), *roots.result()]
    if len(usages) > limit:
        raise TooManyUsagesError(f"the entity has more than {limit} usages")
    return usages


async def _object_usages(store: UsageStore, iri: str, limit: int) -> list[Usage]:
    async with store.sparql.select_stream(store.dataset, store.query(_OBJECT, iri, limit)) as stream:
        subject, predicate = (stream.variables.index(name) for name in ("subject", "predicate"))
        return [Usage(UsageSource.OBJECT, _iri(row[subject]), predicate=_iri(row[predicate])) async for row in stream]


async def _predicate_usages(store: UsageStore, iri: str, limit: int) -> list[Usage]:
    """Return the usages of an entity as a triple's predicate, from how many values of it each subject has."""
    async with store.sparql.select_stream(store.dataset, store.query(_PREDICATE, iri, limit)) as stream:
        values = Counter([_iri(subject) async for (subject,) in stream])
    return [Usage(UsageSource.PREDICATE, subject, index=k) for subject, count in values.items() for k in range(count)]


Value = IRI | Literal | BlankNode
"""A value of a property, a blank one by its store label, which a query can name as `<_:label>`."""


async def read_values(
    store: UsageStore, iri: str, subjects: Iterable[str], max_chars: int | None = None
) -> dict[str, list[Value]]:
    """Return the values of an entity as a property that some subjects have, each subject's in one fixed order.

    `Usage.index` counts in this order: by the value's N-Triples form, a blank one's by its store label, which are
    fixed for a generation's store.

    Args:
        store: Where to read them.
        iri: The property's IRI.
        subjects: The subjects.
        max_chars: The most characters the query's result may hold; no limit if None.

    Raises:
        SparqlError: The query failed.
        ResultTooLargeError: The result is longer than `max_chars`.
        RDFSyntaxError: An IRI isn't valid.
    """
    rows = " ".join(f"<{IRI(subject)}>" for subject in sorted(set(subjects)))
    if not rows:
        return {}
    query = store.query(_VALUES, iri).replace(_NO_SUBJECTS, _NO_SUBJECTS.replace("{ }", f"{{ {rows} }}"))
    result = await store.sparql.select(store.dataset, query, max_chars=max_chars)
    subject, value, blank = (result.variables.index(name) for name in ("subject", "object", "blank"))
    found: dict[str, list[Value]] = {}
    for row in result.rows:
        label, term = row[blank], row[value]
        if isinstance(label, Literal):
            term = BlankNode(label.value)
        if not isinstance(term, IRI | Literal | BlankNode):
            raise SparqlError(f"a property's values query gave no value: {row!r}")
        found.setdefault(_iri(row[subject]), []).append(term)
    for values in found.values():
        values.sort(key=_value_key)
    return found


def _value_key(value: Value) -> str:
    return f"_:{value}" if isinstance(value, BlankNode) else ntriples_term(value)


def _iri(term: Term | None) -> IRI:
    if not isinstance(term, IRI):
        raise SparqlError(f"a usage query gave a subject or predicate that isn't an IRI: {term!r}")
    return term


def _entity_id(connection: sqlite3.Connection, iri: str) -> int | None:
    """Return the first `entity.id` of an IRI, by which the owner map refers to it; None if it's no entity."""
    return connection.execute("SELECT min(id) FROM entity WHERE iri = ?", (iri,)).fetchone()[0]


def _root_usages(connection: sqlite3.Connection, iri: str, limit: int) -> list[Usage]:
    """Return the owner map's roots that are an entity's usages (`usage_ref`), with their owners, at most `limit`."""
    entity = _entity_id(connection, iri)
    if entity is None:
        return []
    rows = connection.execute(
        "SELECT r.id, r.kind, r.label, coalesce(o.iri, r.owner_iri), r.predicate FROM usage_ref AS f"
        " JOIN usage_root AS r ON r.id = f.root LEFT JOIN entity AS o ON o.id = r.owner WHERE f.entity = ? LIMIT ?",
        (entity, limit),
    )
    return [
        Usage(
            UsageSource.ROOT,
            None if owner is None else IRI(owner),
            predicate=None if predicate is None else IRI(predicate),
            root=UsageRoot(number, UsageRootKind(kind), BlankNode(label)),
        )
        for number, kind, label, owner, predicate in rows
    ]


async def count_usages(store: UsageStore, iri: str, cache: UsageCounts | None = None) -> int:
    """Return how many usages an entity has (the module's docstring says how they're counted), as `read_usages` lists.

    Args:
        store: Where to count them.
        iri: The entity's IRI. One that isn't in `entity` gets its triples' count, blank subjects included.
        cache: Where an entity's counts are kept that took longer than `CACHED_COUNT_S`, and looked up first; none if
            None. Once the generation's usage table is ready, an entity's count comes from it (`read_table_page`).

    Raises:
        SparqlError: The query failed.
        RDFSyntaxError: The IRI isn't valid.
        NotCurrentError: The generation's derived database isn't current.
    """
    if cache is not None and (known := cache.get(store.generation, iri)) is not None:
        return known
    if store.tables is not None:
        try:
            page = await read_table_page(store, iri, 0, 0)
        except TableNotReadyError:
            page = None
        except sqlite3.Error:
            logger.exception("Couldn't read the count of %s from the usage table; counting it", logged_iri(iri))
            page = None
        if page is not None:
            return page.count
    start = time.perf_counter()
    async with task_group() as group:
        triples = group.create_task(store.sparql.select(store.dataset, store.query(_COUNT, iri)))
        corrections = group.create_task(store.derived.read(store.generation, lambda c: _corrections(c, iri)))
    result = triples.result()
    [row] = result.rows
    found = {name: _integer(row[index]) for index, name in enumerate(result.variables)}
    correction = corrections.result()
    count = found["object"] + found["predicate"] - found["self"] + (correction or 0)
    elapsed = time.perf_counter() - start
    if cache is not None and correction is not None and elapsed > CACHED_COUNT_S:
        logger.info("Keeping the usage count of %s, %d, which took %.0f ms", logged_iri(iri), count, elapsed * 1000)
        cache.keep(store.generation, iri, count)
    return count


def _integer(term: Term | None) -> int:
    if not isinstance(term, Literal):
        raise SparqlError(f"the usage count query gave a count that isn't a literal: {term!r}")
    return int(term.value)


def _corrections(connection: sqlite3.Connection, iri: str) -> int | None:
    """Return what the owner map adds to Fuseki's counts of an entity's triples; None if it's no entity.

    That's its roots that are the entity's usages, less the triples with a blank subject that have it as object or
    predicate.
    """
    entity = _entity_id(connection, iri)
    if entity is None:
        return None
    return connection.execute(
        "SELECT (SELECT count(*) FROM usage_ref WHERE entity = :entity)"
        " - coalesce((SELECT object + predicate FROM usage_blank WHERE entity = :entity), 0)",
        {"entity": entity},
    ).fetchone()[0]


@dataclass(frozen=True)
class TablePage:
    """An entity's usages at some positions in the tab's order, from the usage table (`read_table_page`).

    Attributes:
        count: How many usages the entity has, as `count_usages` counts them.
        listed: How many of them can be listed.
        usages: The usages at the positions asked for.
        previous: The usage just before them; None if they start at the first.
    """

    count: int
    listed: int
    usages: list[Usage]
    previous: Usage | None


async def read_table_page(store: UsageStore, iri: str, start: int, stop: int) -> TablePage | None:
    """Return an entity's count, and its usages at positions `start` to `stop` (not included), from the usage table.

    Args:
        store: Where to read them; its `tables` must be given.
        iri: The entity's IRI.
        start: The first position, from 0.
        stop: The position after the last; `start` for none.

    Returns:
        The page; None if the IRI isn't an entity's.

    Raises:
        TableNotReadyError: The generation's usage table isn't ready.
        NotCurrentError: The generation's derived database isn't current.
    """
    assert store.tables is not None
    entity = await store.derived.read(store.generation, lambda connection: _entity_id(connection, iri))
    if entity is None:
        return None
    first = max(start - 1, 0)
    listed, count, rows = await store.tables.read(
        store.generation, lambda connection: _table_rows(connection, entity, first, stop)
    )
    usages = await store.derived.read(store.generation, lambda connection: _table_usages(connection, rows))
    previous = usages.pop(0) if start > 0 and usages and rows[0][0] == first else None
    return TablePage(count, listed, usages, previous)


type _TableRow = tuple[int, int | None, str | None, int, str | None, int]
"""A `usage` row: its position, subject's `entity.id` or else IRI, source, predicate's IRI and detail."""


def _table_rows(connection: sqlite3.Connection, entity: int, start: int, stop: int) -> tuple[int, int, list[_TableRow]]:
    """Return an entity's listed usages and count, and its `usage` rows from `start` to `stop`, with their terms."""
    row = connection.execute("SELECT listed, count FROM usage_count WHERE entity = ?", (entity,)).fetchone()
    listed, count = row if row is not None else (0, 0)
    rows = connection.execute(
        "SELECT u.position, u.subject, s.iri, u.source, p.iri, u.detail FROM usage AS u"
        " LEFT JOIN term AS s ON s.id = u.subject_term LEFT JOIN term AS p ON p.id = u.predicate"
        " WHERE u.entity = ? AND u.position >= ? AND u.position < ? ORDER BY u.position",
        (entity, start, stop),
    ).fetchall()
    return listed, count, rows


def _table_usages(connection: sqlite3.Connection, rows: list[_TableRow]) -> list[Usage]:
    """Return `usage` rows as usages, with their subjects' IRIs and roots from the derived database."""
    subjects = _by_id(connection, "SELECT id, iri FROM entity", {row[1] for row in rows if row[1] is not None})
    roots = _by_id(
        connection,
        "SELECT id, kind, label FROM usage_root",
        {row[5] for row in rows if row[3] == Source.ROOT},
    )
    usages = []
    for _, subject_id, subject_iri, source, predicate, detail in rows:
        subject = IRI(subjects[subject_id][0]) if subject_id is not None else None
        if subject_iri is not None:
            subject = IRI(subject_iri)
        iri = None if predicate is None else IRI(predicate)
        if source == Source.OBJECT:
            usages.append(Usage(UsageSource.OBJECT, subject, predicate=iri))
        elif source == Source.PREDICATE:
            usages.append(Usage(UsageSource.PREDICATE, subject, index=detail))
        else:
            kind, label = roots[detail]
            root = UsageRoot(detail, UsageRootKind(kind), BlankNode(label))
            usages.append(Usage(UsageSource.ROOT, subject, predicate=iri, root=root))
    return usages


def _by_id(connection: sqlite3.Connection, query: str, ids: set[int]) -> dict[int, tuple]:
    """Return rows by their id, read by a query (`SELECT id, …`) of some ids."""
    found: dict[int, tuple] = {}
    ordered = sorted(ids)
    for batch in (ordered[at : at + 500] for at in range(0, len(ordered), 500)):
        rows = connection.execute(f"{query} WHERE id IN ({', '.join('?' * len(batch))})", batch)
        found |= {number: rest for number, *rest in rows}
    return found
