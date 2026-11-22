"""The owner-map pre-caching hook (P-27): `usage_root`, `usage_ref` and `usage_blank`, which DESIGN.md §2.5 describes.

One bulk query (`owner-map.rq`) streams the blank nodes' triples and IRI parents; the roots, and the entities each is a
usage of, are worked out on the database's thread. Entities are referred to by the first `entity.id` of their IRI.
"""

import functools
import logging
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum, IntEnum, auto
from typing import NamedTuple

from browl.derived import PROPERTY_TYPES, EntityType, UsageRootKind
from browl.fuseki import SparqlError
from browl.jobtypes import StageError
from browl.precache.core import PreCacheContext, batched, hook, sparql_file
from browl.rdf import IRI, OWL, RDF, RDFS, XSD, Literal, Term

logger = logging.getLogger(__name__)

SWRL = "http://www.w3.org/2003/11/swrl#"
"""The SWRL vocabulary, which OWL's RDF mapping uses for rules."""

WALK_BUDGET = 1_000_000
"""How many visits to blank nodes the roots' walks may make beyond one per node, as roots can share blank nodes; a
store that needs more fails the stage, rather than run for hours."""

_TYPE = f"{RDF}type"
_FIRST = f"{RDF}first"
_REST = f"{RDF}rest"
_NIL = f"{RDF}nil"
_SOURCE = f"{OWL}annotatedSource"
_PROPERTY = f"{OWL}annotatedProperty"
_TARGET = f"{OWL}annotatedTarget"
_ANNOTATED = frozenset({_SOURCE, _PROPERTY, _TARGET})
_REIFIED_TYPES = frozenset({f"{OWL}Axiom", f"{OWL}Annotation"})
_CLASS_AXIOM_PREDICATES = frozenset({f"{RDFS}subClassOf", f"{OWL}equivalentClass", f"{OWL}disjointWith"})
_LOGICAL_RDFS = frozenset({f"{RDFS}{name}" for name in ("subClassOf", "subPropertyOf", "domain", "range")})
_NOT_CLASS = frozenset(
    f"{OWL}{name}"
    for name in (
        "onProperty",
        "onProperties",
        "inverseOf",
        "hasValue",
        "oneOf",
        "onDataRange",
        "onDatatype",
        "datatypeComplementOf",
        "withRestrictions",
    )
)
"""The predicates of class expressions and data ranges whose objects, or list's members, aren't classes."""
_BLANK = "_:"  # a blank object's key in `_Triples.children` is its label after this; no IRI starts with it

type _Object = str | int
"""A blank node's triple's object: an IRI, a blank node as `_BLANK` and its label, or a literal's hash."""


class _Form(Enum):
    """What an unowned root's axiom is, which decides whose own axiom it is."""

    CLASS_AXIOM = auto()  # a general class axiom, or an n-ary DisjointClasses
    PROPERTY_AXIOM = auto()  # an n-ary DisjointObjectProperties or DisjointDataProperties
    OTHER = auto()  # a negative property assertion, a SWRL rule, n-ary DifferentIndividuals, …


class _Named(IntEnum):
    """How a root names an IRI: the most of the ways it does."""

    ANNOTATION = 0  # only in annotations
    LOGICAL = 1  # in its logical part, where no class goes, such as a restriction's property
    CLASS = 2  # in its logical part, where a class goes


class _Columns(NamedTuple):
    """The owner-map query's columns' numbers."""

    node: int
    predicate: int
    object: int
    blank: int
    parent: int


@dataclass
class _Triples:
    """The owner-map query's rows, deduplicated, as the query may give a node's rows several times.

    IRIs are interned, as a few predicates and objects recur in millions of rows.

    Attributes:
        children: Each blank node's triples, as (predicate, object), by its label.
        parents: Each blank node's IRI parents, as (subject, predicate), by its label.
    """

    children: dict[str, set[tuple[str, _Object]]] = field(default_factory=dict)
    parents: dict[str, set[tuple[str, str]]] = field(default_factory=dict)

    def add(self, rows: Iterable[tuple[Term | None, ...]], columns: _Columns) -> None:
        """Add a batch of rows."""
        for row in rows:
            node = _label(row[columns.node])
            predicate = sys.intern(str(row[columns.predicate]))
            parent = row[columns.parent]
            if parent is not None:
                self.parents.setdefault(node, set()).add((sys.intern(str(parent)), predicate))
                continue
            blank = row[columns.blank]
            item = _BLANK + _label(blank) if blank is not None else _object_key(row[columns.object])
            self.children.setdefault(node, set()).add((predicate, item))


def _add(triples: _Triples, rows: list[tuple[Term | None, ...]], columns: _Columns, _: sqlite3.Connection) -> None:
    """Add a batch of rows to the triples, on the database's thread (`DerivedWriter.run` passes the connection)."""
    triples.add(rows, columns)


def _label(term: Term | None) -> str:
    """Return a blank node's store label, which the query gives as a string literal (`afn:bnode`)."""
    if not isinstance(term, Literal):
        raise SparqlError(f"the owner map's query gave a blank node's label that isn't a string: {term!r}")
    return term.value


def _object_key(term: Term | None) -> _Object:
    if isinstance(term, IRI):
        return sys.intern(str(term))
    if isinstance(term, Literal):
        return hash(term)  # only told apart from the node's other objects, so a hash will do
    raise SparqlError(f"the owner map's query gave an object that isn't an IRI or a literal: {term!r}")


def _blank(item: _Object | None) -> str | None:
    """Return an object's label if it's a blank node, else None."""
    return item[len(_BLANK) :] if isinstance(item, str) and item.startswith(_BLANK) else None


def _named(item: _Object | None) -> str | None:
    """Return an object if it's an IRI, else None."""
    return item if isinstance(item, str) and not item.startswith(_BLANK) else None


@dataclass(frozen=True, slots=True)
class _Root:
    """A root of the owner map, with the IRIs it names.

    Attributes:
        kind: What it is.
        label: Its blank node's store label.
        owner: Its owner's IRI; None if it has none.
        predicate: A triple's predicate, or a reified axiom's `owl:annotatedProperty`; else None.
        iris: The IRIs it names, each with how.
        form: For a root with no owner, what its axiom is; else None.
    """

    kind: UsageRootKind
    label: str
    owner: str | None
    predicate: str | None
    iris: dict[str, _Named]
    form: _Form | None = None


@hook()
async def owner_map(context: PreCacheContext) -> None:
    """Fill `usage_root`, `usage_ref` and `usage_blank` from one bulk query (`owner-map.rq`), with the imports.

    The rows are gathered, and the roots worked out and written, on the database's thread, off the event loop.

    Raises:
        StageError: The roots' walks would exceed `WALK_BUDGET`.
    """
    triples = _Triples()
    async with context.bulk_select(sparql_file("owner-map.rq")) as stream:
        columns = _Columns(*(stream.variables.index(name) for name in _Columns._fields))
        async for batch in batched(stream):
            await context.db.run(functools.partial(_add, triples, batch, columns))
    await context.db.run(functools.partial(_write_owner_map, triples))
    for table in ("usage_root", "usage_ref", "usage_blank"):
        await context.db.create_indexes(table)


class _Walk:
    """Finds the owner map's roots (DESIGN.md §2.5), and walks from each to the IRIs it names."""

    def __init__(self, triples: _Triples) -> None:
        """Find which blank nodes are reified, which are part of another's root, and which are copies."""
        self._triples = triples
        children = triples.children
        self._reified = {
            node for node, edges in children.items() if any(p == _TYPE and o in _REIFIED_TYPES for p, o in edges)
        }
        # The reified nodes whose source is a blank node: a general class axiom's, or a reified axiom's, so another
        # annotation; each is part of that node's root.
        self._annotations: dict[str, list[str]] = {}
        self._linked: set[str] = set()  # blank nodes in another's root
        self._copies: set[str] = set()  # copies of a main triple's blank node, as a reified axiom's target or source
        for node, edges in children.items():
            reified = node in self._reified
            property_ = next((o for p, o in edges if p == _PROPERTY), None) if reified else None
            for predicate, item in edges:
                if (label := _blank(item)) is None:
                    continue
                if reified and predicate == _TARGET:
                    self._copies.add(label)
                elif reified and predicate == _SOURCE:
                    if any(p == property_ for p, _ in children.get(label, ())):  # it holds the main triple
                        self._annotations.setdefault(label, []).append(node)
                        self._linked.add(node)
                    else:
                        self._copies.add(label)
                else:
                    self._linked.add(label)
        self._steps_left = len(children) + WALK_BUDGET

    def roots(self) -> Iterator[_Root]:
        """Yield the roots: the triples' by blank node label, then the others by label.

        Raises:
            StageError: The walks would exceed `WALK_BUDGET`.
        """
        children, parents = self._triples.children, self._triples.parents
        for node in sorted(parents):
            iris = self._inside(node)
            for owner, predicate in sorted(parents[node]):
                yield _Root(UsageRootKind.TRIPLE, node, owner, predicate, iris)
        unlinked = children.keys() - self._linked - parents.keys()
        for node in sorted(unlinked - (self._copies - self._reified)):
            iris = self._inside(node)
            edges = children[node]
            if node in self._reified:
                annotated = {p: o for p, o in edges if p in _ANNOTATED}
                for each in annotated.values():  # the main triple's, counted from it, by its own root if it has one
                    if (label := _blank(each)) is not None:
                        for iri in self._inside(label):
                            iris.pop(iri, None)
                    elif isinstance(each, str):
                        iris.pop(each, None)
                source, property_ = annotated.get(_SOURCE), annotated.get(_PROPERTY)
                yield _Root(UsageRootKind.AXIOM, node, _named(source), _named(property_), iris)
            else:
                yield _Root(UsageRootKind.GENERAL, node, None, None, iris, _form(edges))

    def _inside(self, root: str) -> dict[str, _Named]:
        """Return the IRIs reached from a root's blank node through blank nodes and the annotations on them, with how.

        A reified axiom's source, property and target are skipped: they're the main triple's, counted from it. A list's
        member is named as the predicate the list is the object of names it.

        Raises:
            StageError: The walks would exceed `WALK_BUDGET`.
        """
        found: dict[str, _Named] = {}
        stack: list[tuple[str, str | None]] = [(root, None)]  # each node, with its list's predicate
        seen = {root}
        while stack:
            self._steps_left -= 1
            if self._steps_left < 0:
                raise StageError(f"the owner map's roots share blank nodes too much, over {WALK_BUDGET:,} visits")
            node, role = stack.pop()
            reified = node in self._reified
            for each in self._annotations.get(node, ()):
                if each not in seen:
                    seen.add(each)
                    stack.append((each, None))
            for predicate, item in self._triples.children.get(node, ()):
                if reified and predicate in _ANNOTATED:
                    continue
                annotation = _is_annotation(predicate)
                if annotation:
                    found.setdefault(predicate, _Named.ANNOTATION)
                if isinstance(item, int):
                    continue
                if (label := _blank(item)) is not None:
                    if label not in seen:
                        seen.add(label)
                        stack.append((label, role if predicate in (_FIRST, _REST) else predicate))
                elif not (item == _NIL or (predicate == _TYPE and item.startswith((OWL, RDF)))):
                    position = role if predicate == _FIRST else predicate
                    if annotation:
                        named = _Named.ANNOTATION
                    else:
                        named = _Named.LOGICAL if position in _NOT_CLASS else _Named.CLASS
                    found[item] = max(found.get(item, _Named.ANNOTATION), named)
        return found


def _form(edges: set[tuple[str, _Object]]) -> _Form:
    types = {o for p, o in edges if p == _TYPE}
    if f"{OWL}AllDisjointProperties" in types:
        return _Form.PROPERTY_AXIOM
    if f"{OWL}AllDisjointClasses" in types or any(p in _CLASS_AXIOM_PREDICATES for p, _ in edges):
        return _Form.CLASS_AXIOM
    return _Form.OTHER


@functools.lru_cache(maxsize=4096)
def _is_annotation(predicate: str) -> bool:
    """Return whether a blank node's predicate is an annotation property.

    That's one outside OWL's, RDF's, SWRL's and XSD's vocabularies (XSD's are a datatype restriction's facets), and not
    one of RDFS's axiom properties.
    """
    return not (predicate.startswith((OWL, RDF, SWRL, XSD)) or predicate in _LOGICAL_RDFS)


@dataclass(frozen=True, slots=True)
class _Entity:
    """What the owner map needs of an IRI in `entity`: its first id, and whether it's a class and a property."""

    id: int
    is_class: bool
    is_property: bool


def _read_entities(connection: sqlite3.Connection, iris: Iterable[str]) -> dict[str, _Entity]:
    """Return the IRIs that are entities, by IRI, through a temporary table joined to `entity`."""
    connection.execute("CREATE TEMP TABLE owner_map_iri (iri TEXT PRIMARY KEY) WITHOUT ROWID")
    try:
        connection.executemany("INSERT INTO owner_map_iri (iri) VALUES (?)", ((iri,) for iri in iris))
        properties = ", ".join("?" * len(PROPERTY_TYPES))
        rows = connection.execute(
            f"SELECT e.iri, min(e.id), max(e.type = ?), max(e.type IN ({properties}))"
            " FROM owner_map_iri AS t JOIN entity AS e ON e.iri = t.iri GROUP BY e.iri",
            (EntityType.CLASS, *sorted(PROPERTY_TYPES)),
        )
        return {iri: _Entity(number, bool(is_class), bool(is_property)) for iri, number, is_class, is_property in rows}
    finally:
        connection.execute("DROP TABLE temp.owner_map_iri")


def _is_own(root: _Root, entity: _Entity, named: _Named) -> bool:
    """Return whether a root is an entity's own axiom, which its Overview lists, not its usage (DESIGN.md §2.5)."""
    if root.form is _Form.CLASS_AXIOM:
        return entity.is_class and named is _Named.CLASS
    if root.form is _Form.PROPERTY_AXIOM:
        return entity.is_property and named >= _Named.LOGICAL
    return False


def _write_owner_map(triples: _Triples, connection: sqlite3.Connection) -> None:
    """Work out the roots (`_Walk`) and fill `usage_root`, `usage_ref` and `usage_blank`.

    Only IRIs in `entity` are kept: an IRI that's no entity has no Usage tab, and an owner that's none is kept as its
    IRI (`usage_root.owner_iri`).
    """
    blank_object: Counter[str] = Counter()
    blank_predicate: Counter[str] = Counter()
    for edges in triples.children.values():
        for predicate, item in edges:
            blank_predicate[predicate] += 1
            if (iri := _named(item)) is not None:
                blank_object[iri] += 1
    # Every IRI a root names or is owned by is a blank node's predicate or object, or a triple's subject.
    owners = {owner for parents in triples.parents.values() for owner, _ in parents}
    entities = _read_entities(connection, blank_object.keys() | blank_predicate.keys() | owners)
    root_rows: list[tuple[int, str, str, int | None, str | None, str | None]] = []
    refs: list[tuple[int, int]] = []
    for root in _Walk(triples).roots():
        used = sorted(
            {
                entity.id
                for iri, named in root.iris.items()
                if (entity := entities.get(iri)) is not None and iri != root.owner and not _is_own(root, entity, named)
            }
        )
        if not used:
            continue
        number = len(root_rows) + 1
        owner = entities.get(root.owner) if root.owner is not None else None
        owner_iri = root.owner if owner is None else None
        root_rows.append(
            (number, root.kind, root.label, None if owner is None else owner.id, owner_iri, root.predicate)
        )
        refs.extend((entity, number) for entity in used)
    refs.sort()
    blank_rows = sorted(
        (entity.id, blank_object[iri], blank_predicate[iri])
        for iri, entity in entities.items()
        if blank_object[iri] or blank_predicate[iri]
    )
    connection.executemany(
        "INSERT INTO usage_root (id, kind, label, owner, owner_iri, predicate) VALUES (?, ?, ?, ?, ?, ?)", root_rows
    )
    connection.executemany("INSERT INTO usage_ref (entity, root) VALUES (?, ?)", refs)
    connection.executemany("INSERT INTO usage_blank (entity, object, predicate) VALUES (?, ?, ?)", blank_rows)
    logger.info("The owner map has %d roots that are usages, and %d usages through them", len(root_rows), len(refs))
