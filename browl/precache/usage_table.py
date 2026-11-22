"""The usage table (P-27c, DESIGN.md §2.5): every usage of every entity, in the Usage tab's order, in a file of its own.

It's built in the background once a generation is live (`browl.usage_tables`), from one bulk query over every triple
(`usage-scan.rq`) and the derived database's owner map, which the build's connection attaches as `derived`. Each usage
is a row of `usage`, keyed by the entity it refers to and its position in the tab's order (`browl.usage_tab`), so any
page is one range read. This module is the build's code and schema, which its key covers (`browl.precache.keys`).

A usage is identified as `browl.usages.Usage` identifies it: a triple with the entity as object by its subject and
predicate; a triple with the entity as predicate by its subject and its place among that subject's values of the
predicate (`Usage.index`), so no object is stored; a root by its number.
"""

import functools
import logging
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import IntEnum

from browl.derived import UsageRootKind
from browl.fuseki import FusekiClient, SparqlError, filled_query
from browl.precache.core import PREFIXES, DerivedWriter, batched, sparql_file
from browl.rdf import IRI, OWL, RDF, RDFS, Literal, Term
from browl.tasks import task_group

logger = logging.getLogger(__name__)

DERIVED = "derived"
"""The schema name under which the build's connection attaches the generation's derived database."""

SCHEMA = """
CREATE TABLE meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    generation INTEGER NOT NULL,
    key TEXT NOT NULL,
    derived TEXT NOT NULL
);
CREATE TABLE term (
    id INTEGER PRIMARY KEY,
    iri TEXT NOT NULL
);
CREATE TABLE usage (
    entity INTEGER NOT NULL,
    position INTEGER NOT NULL,
    subject INTEGER,
    subject_term INTEGER,
    source INTEGER NOT NULL,
    kind INTEGER NOT NULL,
    predicate INTEGER,
    detail INTEGER NOT NULL,
    PRIMARY KEY (entity, position)
) WITHOUT ROWID;
CREATE TABLE usage_count (
    entity INTEGER PRIMARY KEY,
    listed INTEGER NOT NULL,
    count INTEGER NOT NULL
);
"""
"""The usage table's file. `meta` and its columns must keep their names, as `browl.usage_tables` reads them from a file
built by any browl."""


class Source(IntEnum):
    """Where a usage was found, as `usage.source` stores it; in the tab's order (`browl.usages.UsageSource`)."""

    OBJECT = 0
    PREDICATE = 1
    ROOT = 2


class SubjectGroup(IntEnum):
    """A usage's subject's group, in the tab's order (`browl.usage_tab.sorted_usages`)."""

    NAMED = 0
    """An entity's IRI."""
    NO_SUBJECT = 1
    """None: a root with no owner."""
    NOT_ENTITY = 2
    """An IRI that's no entity's."""


class UsageKind(IntEnum):
    """What a usage's axiom is, as `usage.kind` stores it, for U-34's filters.

    Most come from the axiom's main predicate, which `usage.predicate` holds; any predicate not named here makes an
    annotation or property assertion, by that property.
    """

    ASSERTION = 0
    TYPE = 1
    SUB_CLASS_OF = 2
    EQUIVALENT_CLASS = 3
    DISJOINT_CLASS = 4
    DISJOINT_UNION = 5
    SUB_PROPERTY_OF = 6
    EQUIVALENT_PROPERTY = 7
    DISJOINT_PROPERTY = 8
    INVERSE_OF = 9
    PROPERTY_CHAIN = 10
    DOMAIN = 11
    RANGE = 12
    HAS_KEY = 13
    SAME_AS = 14
    DIFFERENT_FROM = 15
    AXIOM_ANNOTATION = 16
    """Found in a reified axiom's annotations; the predicate is its `owl:annotatedProperty`."""
    NO_SUBJECT = 17
    """A root with no owner, such as a general class axiom; it has no predicate."""


KINDS = {
    f"{RDF}type": UsageKind.TYPE,
    f"{RDFS}subClassOf": UsageKind.SUB_CLASS_OF,
    f"{OWL}equivalentClass": UsageKind.EQUIVALENT_CLASS,
    f"{OWL}disjointWith": UsageKind.DISJOINT_CLASS,
    f"{OWL}disjointUnionOf": UsageKind.DISJOINT_UNION,
    f"{RDFS}subPropertyOf": UsageKind.SUB_PROPERTY_OF,
    f"{OWL}equivalentProperty": UsageKind.EQUIVALENT_PROPERTY,
    f"{OWL}propertyDisjointWith": UsageKind.DISJOINT_PROPERTY,
    f"{OWL}inverseOf": UsageKind.INVERSE_OF,
    f"{OWL}propertyChainAxiom": UsageKind.PROPERTY_CHAIN,
    f"{RDFS}domain": UsageKind.DOMAIN,
    f"{RDFS}range": UsageKind.RANGE,
    f"{OWL}hasKey": UsageKind.HAS_KEY,
    f"{OWL}sameAs": UsageKind.SAME_AS,
    f"{OWL}differentFrom": UsageKind.DIFFERENT_FROM,
}
"""The axiom kind of each main predicate that isn't an assertion's."""

VOCABULARY_TYPES = (
    *(
        f"{OWL}{name}"
        for name in (
            "Class",
            "ObjectProperty",
            "DatatypeProperty",
            "AnnotationProperty",
            "NamedIndividual",
            "Axiom",
            "Annotation",
            "Restriction",
            "Ontology",
            "AllDisjointClasses",
            "AllDisjointProperties",
            "AllDifferent",
            "NegativePropertyAssertion",
            "FunctionalProperty",
            "InverseFunctionalProperty",
            "TransitiveProperty",
            "SymmetricProperty",
            "AsymmetricProperty",
            "ReflexiveProperty",
            "IrreflexiveProperty",
        )
    ),
    f"{RDFS}Datatype",
)
"""The `rdf:type` objects whose triples the scan leaves out, unless they're entities: declarations, characteristics
and the blank nodes' types, none of them usages of an entity."""

# Plain ints, for the scan's speed (`_Scan.add`).
_NAMED, _NO_SUBJECT, _NOT_ENTITY = (int(group) for group in SubjectGroup)
_OBJECT, _PREDICATE = int(Source.OBJECT), int(Source.PREDICATE)
_NO_TYPES = "NOT IN ()"
_NO_ENTITY = "<urn:x-browl:none>"  # stands for none in a `NOT IN` list, which can't be empty
_SCAN = sparql_file("usage-scan.rq")
_COUNT = sparql_file("usage-scan-count.rq")
_STAGE = """
CREATE TABLE scratch.stage (
    entity INTEGER NOT NULL, grp INTEGER NOT NULL, rank INTEGER, subject INTEGER, subject_term INTEGER,
    source INTEGER NOT NULL, kind INTEGER NOT NULL, predicate INTEGER, detail INTEGER NOT NULL
);
CREATE TEMP TABLE term_rank (id INTEGER PRIMARY KEY, rank INTEGER NOT NULL);
"""
_STAGE_ROW = "INSERT INTO scratch.stage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
# The tab's order (`browl.usage_tab.sorted_usages`): subjects by name, then IRI; then by source, predicate, and root.
# A subject's usages of a property as predicate are alike, so they're numbered among themselves: their `detail`.
_POSITIONS = f"""
INSERT INTO usage (entity, position, subject, subject_term, source, kind, predicate, detail)
SELECT s.entity, row_number() OVER w - 1, s.subject, s.subject_term, s.source, s.kind, s.predicate,
    CASE s.source WHEN {Source.PREDICATE} THEN row_number() OVER w - rank() OVER w ELSE s.detail END
FROM scratch.stage AS s
LEFT JOIN term_rank AS ts ON ts.id = s.subject_term
LEFT JOIN term_rank AS tp ON tp.id = s.predicate
WINDOW w AS (
    PARTITION BY s.entity
    ORDER BY s.grp, coalesce(s.rank, ts.rank), s.source, tp.rank,
        CASE s.source WHEN {Source.PREDICATE} THEN 0 ELSE s.detail END
)
"""


@dataclass
class Progress:
    """How far a build has got, for its estimate.

    Attributes:
        started: When it started (`time.monotonic`).
        triples: How many triples the scan will read; None until they're counted.
        scanned: How many it has read.
        scanned_at: When it had read them all, so only the writing was left; None until then.
    """

    started: float = field(default_factory=time.monotonic)
    triples: int | None = None
    scanned: int = 0
    scanned_at: float | None = None


@dataclass
class _Scan:
    """What the scan has found so far, on the build's thread.

    Attributes:
        ranks: Each entity IRI's place in name order, by which its first `entity.id` is found in `ids`.
        ids: Each entity IRI's first `entity.id`, by its place in name order.
        terms: The number of each predicate, and each subject that isn't an entity, in `term`.
        blank: Per entity, how many triples with a blank subject have it as object or predicate.
    """

    ranks: dict[str, int]
    ids: list[int]
    terms: dict[str, int] = field(default_factory=dict)
    blank: Counter[int] = field(default_factory=Counter)

    def term(self, iri: str) -> int:
        """Return an IRI's number in `term`, giving it the next if it has none."""
        number = self.terms.get(iri)
        if number is None:
            number = self.terms[iri] = len(self.terms) + 1
        return number

    def subject(self, iri: str) -> tuple[int, int | None, int | None, int | None]:
        """Return a usage's subject's group, its place in name order, its `entity.id` and its term."""
        rank = self.ranks.get(iri)
        if rank is None:
            return _NOT_ENTITY, None, None, self.term(iri)
        return _NAMED, rank, self.ids[rank], None

    def add(self, rows: Iterable[Sequence[Term | None]], connection: sqlite3.Connection) -> None:
        """Stage the usages among a batch of the scan's rows (subject, predicate, IRI object or None).

        It runs for every triple, so it's written for speed: local names, and plain ints for the enums.

        Raises:
            SparqlError: A row's predicate isn't an IRI, or its object is neither an IRI nor None.
        """
        rank_of, ids, blank, term, kind_of = self.ranks.get, self.ids, self.blank, self.term, KINDS.get
        staged: list[tuple[int | None, ...]] = []
        for subject, predicate, object_ in rows:
            if type(predicate) is not IRI or (object_ is not None and type(object_) is not IRI):
                raise SparqlError(f"the usage table's scan gave a predicate or object it can't: {predicate!r}")
            as_predicate = rank_of(predicate)
            as_object = None if object_ is None else rank_of(object_)
            if type(subject) is not IRI:  # a blank subject's triple: counted, not listed
                for rank in (as_predicate, as_object):
                    if rank is not None:
                        blank[ids[rank]] += 1
                continue
            if as_object is not None and object_ == subject:
                as_object = None
            if as_predicate is not None and predicate == subject:
                as_predicate = None
            if as_object is None and as_predicate is None:
                continue
            group = self.subject(subject)
            kind, number = int(kind_of(predicate, UsageKind.ASSERTION)), term(predicate)
            if as_object is not None:
                staged.append((ids[as_object], *group, _OBJECT, kind, number, 0))
            if as_predicate is not None:
                staged.append((ids[as_predicate], *group, _PREDICATE, kind, number, 0))
        connection.executemany(_STAGE_ROW, staged)


async def fill_usage_table(
    sparql: FusekiClient,
    dataset: str,
    dataset_clause: str,
    db: DerivedWriter,
    progress: Progress,
) -> None:
    """Fill the usage table, from a scan of every triple and the attached derived database's owner map.

    The connection has the schema created, the derived database attached as `DERIVED`, and a scratch database for
    the staged rows attached as `scratch`.

    Args:
        sparql: The Fuseki client.
        dataset: The generation's dataset.
        dataset_clause: The dataset clause that makes queries see the imports, as the owner map's does.
        db: Writes to the usage table's file, on its thread.
        progress: Updated as the build goes.

    Raises:
        SparqlError: A query failed or returned an incomplete result.
    """
    skipped = await db.run(_skipped_types)
    count = PREFIXES + filled_query(_COUNT, dataset_clause, {_NO_TYPES: _not_in(skipped)})
    async with task_group() as group:
        started = group.create_task(db.run(_start))
        counted = group.create_task(sparql.select(dataset, count))
    scan = started.result()
    progress.triples = _count(counted.result().rows)
    scan_query = _SCAN.replace(_NO_TYPES, _not_in(skipped))
    async with sparql.bulk_select(dataset, scan_query, PREFIXES, dataset_clause) as stream:
        columns = [stream.variables.index(name) for name in ("subject", "predicate", "object")]
        ordered = columns == [0, 1, 2]
        s, p, o = columns
        async for batch in batched(stream):
            rows = batch if ordered else [(row[s], row[p], row[o]) for row in batch]
            await db.run(functools.partial(scan.add, rows))
            progress.scanned += len(batch)
    progress.scanned_at = time.monotonic()
    await db.run(lambda connection: _finish(connection, scan))


def _count(rows: list[tuple[Term | None, ...]]) -> int:
    match rows:
        case [(Literal(value=value),)] if value.isdigit():
            return int(value)
    raise SparqlError(f"the triple count query gave {rows!r}")


def _start(connection: sqlite3.Connection) -> _Scan:
    """Stage nothing yet, and rank every entity IRI in name order: its first entity's display name, then the IRI."""
    for statement in _STAGE.split(";"):  # not `executescript`, which would commit the build's transaction
        if statement.strip():
            connection.execute(statement)
    ordered, ids = _name_order(connection)
    return _Scan({iri: rank for rank, iri in enumerate(ordered)}, ids)


def sort_name(iri: str, display_name: str | None) -> str:
    """Return the name an entity IRI sorts by, given its first entity's display name: that, else the IRI, casefolded."""
    return (display_name or iri).casefold()


def _name_order(connection: sqlite3.Connection) -> tuple[list[str], list[int]]:
    """Return every entity IRI in name order, and each one's first `entity.id`."""
    first: dict[str, tuple[str, int]] = {}  # each IRI's sort name and first id
    for iri, number, name in connection.execute(f"SELECT iri, id, display_name FROM {DERIVED}.entity ORDER BY id"):
        if iri not in first:
            first[iri] = (sort_name(iri, name), number)
    ordered = sorted(first, key=lambda iri: (first[iri][0], iri))
    return ordered, [first[iri][1] for iri in ordered]


def _skipped_types(connection: sqlite3.Connection) -> list[str]:
    """Return the `rdf:type` objects the scan leaves out: OWL's vocabulary's that aren't entities; none if it is one.

    Their triples are 15% of NCBITaxon's (3.6 M of 24.0 M), and the scan is the build's longest part.
    """
    named = [*VOCABULARY_TYPES, f"{RDF}type"]
    entities = {
        iri
        for (iri,) in connection.execute(
            f"SELECT iri FROM {DERIVED}.entity WHERE iri IN ({', '.join('?' * len(named))})", named
        )
    }
    return [] if f"{RDF}type" in entities else [iri for iri in VOCABULARY_TYPES if iri not in entities]


def _not_in(types: Sequence[str]) -> str:
    """Return the scan's and its count's `NOT IN` list of the `rdf:type` objects left out."""
    return f"NOT IN ({', '.join(f'<{iri}>' for iri in types) or _NO_ENTITY})"


def _finish(connection: sqlite3.Connection, scan: _Scan) -> None:
    """Stage the owner map's roots, number every entity's usages in the tab's order, and count them."""
    _stage_roots(connection, scan)
    connection.executemany("INSERT INTO term (id, iri) VALUES (?, ?)", ((n, iri) for iri, n in scan.terms.items()))
    ordered = sorted(scan.terms.items())
    connection.executemany("INSERT INTO term_rank VALUES (?, ?)", ((n, rank) for rank, (_, n) in enumerate(ordered)))
    connection.execute(_POSITIONS)
    connection.execute(
        "INSERT INTO usage_count (entity, listed, count) SELECT entity, count(*), count(*) FROM usage GROUP BY entity"
    )
    # The count is Fuseki's (`browl.usages.count_usages`): triples with a blank subject the owner map lacks count too.
    held = dict(connection.execute(f"SELECT entity, object + predicate FROM {DERIVED}.usage_blank"))
    unlisted = [(entity, count - held.get(entity, 0)) for entity, count in scan.blank.items()]
    connection.executemany(
        "INSERT INTO usage_count (entity, listed, count) VALUES (?, 0, ?)"
        " ON CONFLICT (entity) DO UPDATE SET count = count + excluded.count",
        [row for row in unlisted if row[1]],
    )


def _stage_roots(connection: sqlite3.Connection, scan: _Scan) -> None:
    """Stage the owner map's roots that are usages (`usage_ref`), each under its owner, if it has one."""
    rows = connection.execute(
        f"SELECT f.entity, r.id, r.kind, coalesce(o.iri, r.owner_iri), r.predicate FROM {DERIVED}.usage_ref AS f"
        f" JOIN {DERIVED}.usage_root AS r ON r.id = f.root LEFT JOIN {DERIVED}.entity AS o ON o.id = r.owner"
    )
    while batch := rows.fetchmany(10_000):
        staged = []
        for entity, root, kind, owner, predicate in batch:
            group: tuple[int, int | None, int | None, int | None]
            group = (_NO_SUBJECT, None, None, None) if owner is None else scan.subject(owner)
            usage_kind = _root_kind(UsageRootKind(kind), predicate)
            term = None if predicate is None else scan.term(predicate)
            staged.append((entity, *group, Source.ROOT, usage_kind, term, root))
        connection.executemany(_STAGE_ROW, staged)


def _root_kind(kind: UsageRootKind, predicate: str | None) -> UsageKind:
    if kind == UsageRootKind.GENERAL:
        return UsageKind.NO_SUBJECT
    if kind == UsageRootKind.AXIOM:
        return UsageKind.AXIOM_ANNOTATION
    return KINDS.get(predicate or "", UsageKind.ASSERTION)
