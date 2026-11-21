"""The owner-map pre-caching hook (P-27): the blank-node owner map, `usage_root`, `usage_ref` and `usage_blank`.

The Usage tab finds most of an entity's usages in Fuseki (`?s ?p <entity>`, `?s <entity> ?o`), but not those that
reach it through blank nodes: class expressions, lists, annotations on reified axioms. The owner map holds those, by
root: the top-level triple, reified axiom or unlinked blank node that each such axiom hangs from (DESIGN.md §2.5,
[S-7 findings](../research/S-7/README.md)). One bulk query (`owner-map.rq`) streams the blank nodes' triples and IRI
parents; the roots, and the entities each is a usage of, are worked out on the database's thread.

- **Roots** (`browl.derived.UsageRootKind`): one per triple with an IRI subject and a blank object, as a store can
  share a blank node between several subjects' axioms; one per reified axiom (`owl:Axiom`, `owl:Annotation`), owned by
  its source if that's an IRI; one per other blank node nothing links to (general class axioms, n-ary axioms, negative
  property assertions, SWRL rules), with no owner. A blank node linked only as a reified axiom's target is a copy of
  its main triple's, and isn't a root.
- **The IRIs in a root** are the objects reached from it through blank nodes, but OWL and RDF types and `rdf:nil`,
  and the properties and values of annotations. A reified axiom's are only its annotations': its source, property and
  target are the main triple's, counted from it.
- **A root is a usage** of each entity it names (`usage_ref`), but its owner; and but an entity whose own axiom it is,
  as its Overview lists it (DESIGN.md §2.8): a class named in the logical part of a general class axiom or n-ary
  class axiom, a property among the members of an n-ary property axiom. Roots that are no one's usage aren't stored.
- **`usage_blank`** counts, per entity, the triples with a blank subject that have it as object or predicate, which
  the usage count takes from Fuseki's unfiltered counts (`browl.usages.count_usages`).

Entities are referred to by the first `entity.id` of their IRI, as usages are an IRI's, whatever its types.
"""

import functools
import logging
import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum, auto

from browl.derived import PROPERTY_TYPES, EntityType, UsageRootKind
from browl.fuseki import SparqlError
from browl.precache.core import PreCacheContext, batched, hook, sparql_file
from browl.rdf import IRI, OWL, RDF, RDFS, XSD, Literal, Term

logger = logging.getLogger(__name__)

SWRL = "http://www.w3.org/2003/11/swrl#"
"""The SWRL vocabulary, which OWL's RDF mapping uses for rules."""

_TYPE = f"{RDF}type"
_NIL = f"{RDF}nil"
_SOURCE = f"{OWL}annotatedSource"
_PROPERTY = f"{OWL}annotatedProperty"
_TARGET = f"{OWL}annotatedTarget"
_ANNOTATED = frozenset({_SOURCE, _PROPERTY, _TARGET})
_REIFIED_TYPES = frozenset({f"{OWL}Axiom", f"{OWL}Annotation"})
_CLASS_AXIOM_PREDICATES = frozenset({f"{RDFS}subClassOf", f"{OWL}equivalentClass", f"{OWL}disjointWith"})
_LOGICAL_RDFS = frozenset({f"{RDFS}{name}" for name in ("subClassOf", "subPropertyOf", "domain", "range")})
_BLANK = "_:"  # a blank object's key in `Triples.children` is its label after this; no IRI starts with it

type _Object = str | int
"""A blank node's triple's object: an IRI, a blank node as `_BLANK` and its label, or a literal's hash."""


class _Form(Enum):
    """What an unowned root's axiom is, which decides whose own axiom it is."""

    CLASS_AXIOM = auto()  # a general class axiom, or an n-ary DisjointClasses
    PROPERTY_AXIOM = auto()  # an n-ary DisjointObjectProperties or DisjointDataProperties
    OTHER = auto()  # a negative property assertion, a SWRL rule, n-ary DifferentIndividuals, …


@dataclass
class Triples:
    """The owner-map query's rows, deduplicated, as the query may give a node's rows several times.

    Attributes:
        children: Each blank node's triples, as (predicate, object), by its label.
        parents: Each blank node's IRI parents, as (subject, predicate), by its label.
    """

    children: dict[str, set[tuple[str, _Object]]] = field(default_factory=dict)
    parents: dict[str, set[tuple[str, str]]] = field(default_factory=dict)

    def add(self, rows: Iterable[tuple[Term | None, ...]], columns: tuple[int, ...]) -> None:
        """Add a batch of rows, given the node, predicate, object, blank and parent columns."""
        node_column, predicate_column, object_column, blank_column, parent_column = columns
        for row in rows:
            node = _label(row[node_column])
            predicate = str(row[predicate_column])
            parent = row[parent_column]
            if parent is not None:
                self.parents.setdefault(node, set()).add((str(parent), predicate))
                continue
            blank = row[blank_column]
            value = row[object_column]
            item: _Object = _BLANK + _label(blank) if blank is not None else _object_key(value)
            self.children.setdefault(node, set()).add((predicate, item))


def _add(
    triples: Triples, rows: list[tuple[Term | None, ...]], columns: tuple[int, ...], connection: sqlite3.Connection
) -> None:
    """Add a batch of rows to the triples, on the database's thread (`DerivedWriter.run` passes the connection)."""
    triples.add(rows, columns)


def _label(term: Term | None) -> str:
    """Return a blank node's store label, which the query gives as a string literal (`afn:bnode`)."""
    if not isinstance(term, Literal):
        raise SparqlError(f"the owner map's query gave a blank node's label that isn't a string: {term!r}")
    return term.value


def _object_key(term: Term | None) -> _Object:
    if isinstance(term, IRI):
        return str(term)
    if isinstance(term, Literal):
        return hash(term)  # only told apart from the node's other objects, so a hash will do
    raise SparqlError(f"the owner map's query gave an object that isn't an IRI or a literal: {term!r}")


@dataclass(frozen=True, slots=True)
class Root:
    """A root of the owner map, with the IRIs it names.

    Attributes:
        kind: What it is.
        label: Its blank node's store label.
        owner: Its owner's IRI; None if it has none.
        predicate: A triple's predicate, or a reified axiom's `owl:annotatedProperty`; else None.
        iris: The IRIs it names, each with whether its logical part does, rather than only its annotations.
        form: For a root with no owner, what its axiom is; else None.
    """

    kind: UsageRootKind
    label: str
    owner: str | None
    predicate: str | None
    iris: dict[str, bool]
    form: _Form | None = None


@hook()
async def owner_map(context: PreCacheContext) -> None:
    """Fill `usage_root`, `usage_ref` and `usage_blank` from one bulk query (`owner-map.rq`), with the imports.

    The rows are gathered, and the roots worked out and written, on the database's thread, off the event loop.
    """
    triples = Triples()
    async with context.bulk_select(sparql_file("owner-map.rq")) as stream:
        columns = tuple(stream.variables.index(name) for name in ("node", "predicate", "object", "blank", "parent"))
        async for batch in batched(stream):
            await context.db.run(functools.partial(_add, triples, batch, columns))
    await context.db.run(functools.partial(write_owner_map, triples))
    for table in ("usage_root", "usage_ref", "usage_blank"):
        await context.db.create_indexes(table)


def find_roots(triples: Triples) -> Iterator[Root]:
    """Yield the owner map's roots (the module's docstring says which), with the IRIs each names."""
    children = triples.children
    reified = {node for node, edges in children.items() if any(p == _TYPE and o in _REIFIED_TYPES for p, o in edges)}
    linked: set[str] = set()  # blank nodes with a blank parent, but as a reified axiom's source or target
    targets: set[str] = set()  # blank nodes that are a reified axiom's target
    for edges in children.values():
        for predicate, item in edges:
            if not (isinstance(item, str) and item.startswith(_BLANK)):
                continue
            if predicate == _TARGET:
                targets.add(item[2:])
            elif predicate != _SOURCE:
                linked.add(item[2:])
    for node, parents in triples.parents.items():
        iris = _inside(node, children, reified)
        for owner, predicate in sorted(parents):
            yield Root(UsageRootKind.TRIPLE, node, owner, predicate, iris)
    for node in sorted((children.keys() | targets) - linked - triples.parents.keys()):
        edges = children.get(node, set())
        iris = _inside(node, children, reified)
        if node in reified:
            annotated = {p: o for p, o in edges if p in _ANNOTATED}
            for each in annotated.values():  # the main triple's, counted from it
                if isinstance(each, str):
                    iris.pop(each, None)
            source, property_ = annotated.get(_SOURCE), annotated.get(_PROPERTY)
            yield Root(UsageRootKind.AXIOM, node, _named(source), _named(property_), iris)
        elif node not in targets:  # a target's copy of its main triple's blank node, else
            yield Root(UsageRootKind.GENERAL, node, None, None, iris, _form(edges))


def _named(item: _Object | None) -> str | None:
    """Return an object if it's an IRI, else None."""
    return item if isinstance(item, str) and not item.startswith(_BLANK) else None


def _form(edges: set[tuple[str, _Object]]) -> _Form:
    types = {o for p, o in edges if p == _TYPE}
    if f"{OWL}AllDisjointProperties" in types:
        return _Form.PROPERTY_AXIOM
    if f"{OWL}AllDisjointClasses" in types or any(p in _CLASS_AXIOM_PREDICATES for p, _ in edges):
        return _Form.CLASS_AXIOM
    return _Form.OTHER


def _is_annotation(predicate: str) -> bool:
    """Return whether a blank node's predicate is an annotation property.

    That's one outside OWL's, RDF's, SWRL's and XSD's vocabularies (XSD's are a datatype restriction's facets), and not
    one of RDFS's axiom properties.
    """
    return not (predicate.startswith((OWL, RDF, SWRL, XSD)) or predicate in _LOGICAL_RDFS)


def _inside(root: str, children: dict[str, set[tuple[str, _Object]]], reified: set[str]) -> dict[str, bool]:
    """Return the IRIs reached from a blank node through blank nodes, each with whether a logical triple reaches it.

    A reified axiom's source, property and target are skipped, but not their annotations.
    """
    found: dict[str, bool] = {}
    stack = [root]
    seen = {root}
    while stack:
        node = stack.pop()
        skip = _ANNOTATED if node in reified else frozenset()
        for predicate, item in children.get(node, ()):
            if predicate in skip:
                continue
            annotation = _is_annotation(predicate)
            if annotation:
                found.setdefault(predicate, False)
            if isinstance(item, int):
                continue
            if item.startswith(_BLANK):
                if item[2:] not in seen:
                    seen.add(item[2:])
                    stack.append(item[2:])
            elif not (item == _NIL or (predicate == _TYPE and item.startswith((OWL, RDF)))):
                found[item] = found.get(item, False) or not annotation
    return found


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


def _is_own(root: Root, entity: _Entity, logical: bool) -> bool:
    """Return whether a root is an entity's own axiom, not its usage (the module's docstring says which)."""
    if root.owner is not None:
        return False
    return logical and (
        (root.form is _Form.CLASS_AXIOM and entity.is_class)
        or (root.form is _Form.PROPERTY_AXIOM and entity.is_property)
    )


def write_owner_map(triples: Triples, connection: sqlite3.Connection) -> None:
    """Work out the roots (`find_roots`) and fill `usage_root`, `usage_ref` and `usage_blank`.

    Only IRIs in `entity` are kept: an IRI that's no entity has no Usage tab, and an owner that's none is kept as its
    IRI (`usage_root.owner_iri`).
    """
    roots = list(find_roots(triples))
    blank_object: Counter[str] = Counter()
    blank_predicate: Counter[str] = Counter()
    for edges in triples.children.values():
        for predicate, item in edges:
            blank_predicate[predicate] += 1
            if isinstance(item, str) and not item.startswith(_BLANK):
                blank_object[item] += 1
    iris = {iri for root in roots for iri in root.iris}
    iris.update(root.owner for root in roots if root.owner is not None)
    iris.update(blank_object, blank_predicate)
    entities = _read_entities(connection, iris)
    root_rows: list[tuple[int, str, str, int | None, str | None, str | None]] = []
    refs: list[tuple[int, int]] = []
    for root in roots:
        owner = entities.get(root.owner) if root.owner is not None else None
        number = len(root_rows) + 1
        used = sorted(
            {
                entity.id
                for iri, logical in root.iris.items()
                if (entity := entities.get(iri)) is not None
                and iri != root.owner
                and not _is_own(root, entity, logical)
            }
        )
        if not used:
            continue
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
    connection.executemany(
        "INSERT INTO usage_blank (entity, object, predicate) VALUES (?, ?, ?)",
        blank_rows,
    )
    logger.info("The owner map has %d roots that are usages, and %d usages through them", len(root_rows), len(refs))
