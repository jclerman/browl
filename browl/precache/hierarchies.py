"""The hierarchies pre-caching hook (P-12): the `equiv_group`, `edge` and `counts` tables (DESIGN.md §2.5)."""

import functools
import itertools
import logging
import sqlite3
from collections.abc import Iterable
from typing import NamedTuple

import numpy as np
import numpy.typing as npt
from pyroaring import BitMap
from scipy.sparse import coo_array
from scipy.sparse.csgraph import connected_components

from browl.derived import EntityType
from browl.jobtypes import StageError
from browl.precache.core import PreCacheContext, batched, hook, logged_iri
from browl.rdf import OWL, RDFS, Term

logger = logging.getLogger(__name__)


EDGES = """SELECT ?child ?relation ?parent
WHERE {
  VALUES ?relation { rdfs:subClassOf rdfs:subPropertyOf owl:equivalentClass }
  ?child ?relation ?parent .
  FILTER (isIRI(?child) && isIRI(?parent) && ?child != ?parent)
}"""
"""Every direct subclass, sub-property and class equivalence between two IRIs. The dataset doesn't infer RDFS, so these
are the asserted triples, or the reasoned ones if a reasoner ran."""

HIERARCHIES = (EntityType.CLASS, EntityType.OBJECT_PROPERTY, EntityType.DATA_PROPERTY, EntityType.ANNOTATION_PROPERTY)
"""The hierarchies, each named in `edge.hierarchy` and `counts.hierarchy` by its entities' type."""

EQUIVALENT_CLASS = f"{OWL}equivalentClass"

RELATION_TYPES = {
    f"{RDFS}subClassOf": (EntityType.CLASS,),
    f"{RDFS}subPropertyOf": (EntityType.OBJECT_PROPERTY, EntityType.DATA_PROPERTY, EntityType.ANNOTATION_PROPERTY),
    EQUIVALENT_CLASS: (EntityType.CLASS,),
}
"""The hierarchies each of the edges query's relations links entities in."""

RELATION_CODES = {iri: code for code, iri in enumerate(RELATION_TYPES)}
"""Each relation's code, as `edge_staging.relation` stores it: small, as most of NCBITaxon's 3 M rows hold one."""

OUTSIDE = (
    f"{OWL}Thing",
    f"{OWL}Nothing",
    f"{OWL}topObjectProperty",
    f"{OWL}bottomObjectProperty",
    f"{OWL}topDataProperty",
    f"{OWL}bottomDataProperty",
)
"""The top and bottom entities, which are in no hierarchy, even if declared: so `owl:Thing`'s children are roots."""

DESCENDANT_BUDGET = 300_000_000
"""The most members `count_descendants` may hold at once across its descendant sets: a few hundred MB at most.

NCBITaxon, a tree, needs 3 M, as its root's set holds every class. A dense or adversarial hierarchy might otherwise
need tens of GB (DESIGN.md §2.14)."""

LOGGED_CYCLES = 3
"""The most cycles the cycle warning lists."""

LOGGED_MEMBERS = 5
"""The most members of each cycle the cycle warning lists."""

Edges = npt.NDArray[np.int64]
"""Edges between node numbers, as an array of shape (n, 2): a row is (child, parent)."""


@hook()
async def hierarchies(context: PreCacheContext) -> None:
    """Fill `equiv_group`, `edge` and `counts` for the class hierarchy and each property hierarchy.

    The edges come from one bulk query (`EDGES`), over the imports too; an edge whose ends aren't declared entities of
    a hierarchy's type, or are `OUTSIDE` it, is left out. Classes linked by `owl:equivalentClass`, and the members of a
    cycle of edges, form one group, a single node (`group_nodes`). Each node's counts come from `count_descendants`.

    Raises:
        StageError: A hierarchy is too dense to count its descendants within `DESCENDANT_BUDGET`.
    """
    await context.db.run(_create_staging)
    async with context.bulk_select(EDGES) as stream:
        columns = tuple(stream.variables.index(name) for name in ("child", "relation", "parent"))
        async for batch in batched(stream):
            await context.db.run(functools.partial(_stage, batch, columns))
    for hierarchy in HIERARCHIES:
        await context.db.run(functools.partial(_fill, hierarchy))
    await context.db.run(lambda connection: connection.execute("DROP TABLE edge_staging"))
    for table in ("equiv_group", "edge", "counts"):
        await context.db.create_indexes(table)


def _create_staging(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TEMP TABLE edge_staging (child TEXT NOT NULL, relation INTEGER NOT NULL, parent TEXT NOT NULL)"
    )


def _stage(batch: list[tuple[Term | None, ...]], columns: tuple[int, ...], connection: sqlite3.Connection) -> None:
    """Stage a batch of the edges query's rows, on the database's thread."""
    child, relation, parent = columns
    connection.executemany(
        "INSERT INTO edge_staging VALUES (?, ?, ?)",
        ((str(row[child]), RELATION_CODES[str(row[relation])], str(row[parent])) for row in batch),
    )


def _fill(hierarchy: EntityType, connection: sqlite3.Connection) -> None:
    """Fill a hierarchy's rows of `equiv_group`, `edge` and `counts`.

    Raises:
        StageError: The hierarchy is too dense to count its descendants within `DESCENDANT_BUDGET`.
    """
    outside = ", ".join("?" * len(OUTSIDE))
    # Nodes are numbered in IRI order, so a group's representative, its lowest number, is its first IRI.
    cursor = connection.execute(
        f"SELECT id FROM entity WHERE type = ? AND iri NOT IN ({outside}) ORDER BY iri", (hierarchy, *OUTSIDE)
    )
    ids = np.fromiter((row[0] for row in cursor), np.int64)
    count = len(ids)
    number = np.full(connection.execute("SELECT coalesce(max(id), -1) + 1 FROM entity").fetchone()[0], -1)
    number[ids] = np.arange(count)

    def edges(*relations: str) -> Edges:
        """Return the staged edges for these relations between the hierarchy's nodes.

        Returns:
            The (child, parent) edges, by node number, as an `Edges` array; empty if no relation links the hierarchy.
        """
        codes = [RELATION_CODES[relation] for relation in relations if hierarchy in RELATION_TYPES[relation]]
        if not codes:
            return np.empty((0, 2), np.int64)
        rows = connection.execute(
            "SELECT c.id, p.id FROM edge_staging AS s"
            " JOIN entity AS c ON c.iri = s.child AND c.type = ?1 JOIN entity AS p ON p.iri = s.parent AND p.type = ?1"
            f" WHERE s.relation IN ({', '.join('?' * len(codes))})",
            (hierarchy, *codes),
        )
        found = number[np.fromiter(itertools.chain.from_iterable(rows), np.int64).reshape(-1, 2)]
        return found[(found >= 0).all(axis=1)]  # without the edges to or from `OUTSIDE`

    sub = [relation for relation in RELATION_TYPES if relation != EQUIVALENT_CLASS]
    nodes = group_nodes(count, edges(*sub), edges(EQUIVALENT_CLASS))
    if nodes.cycles:
        logger.warning(
            "The %s hierarchy has %d %s of edges: %s. A cycle's members are equivalent, so each cycle is shown as one"
            " node",
            hierarchy,
            len(nodes.cycles),
            "cycle" if len(nodes.cycles) == 1 else "cycles",
            _describe_cycles(connection, ids, nodes.cycles),
        )
    numbers = np.arange(count)
    grouped = np.isin(nodes.representative, nodes.representative[nodes.representative != numbers])
    _insert(connection, "equiv_group (entity, representative)", ids[grouped], ids[nodes.representative[grouped]])
    parents, children = ids[nodes.edges[:, 1]], ids[nodes.edges[:, 0]]
    _insert(connection, "edge (hierarchy, parent, child)", hierarchy.value, parents, children)
    del parents, children
    try:
        child_counts, descendant_counts = count_descendants(count, nodes.edges)
    except StageError as error:
        raise StageError(f"The {hierarchy} hierarchy: {error}") from error
    is_node = nodes.representative == numbers
    _insert(
        connection,
        "counts (hierarchy, node, children, descendants)",
        hierarchy.value,
        ids[is_node],
        child_counts[is_node],
        descendant_counts[is_node],
    )


def _describe_cycles(
    connection: sqlite3.Connection, ids: npt.NDArray[np.int64], cycles: list[npt.NDArray[np.int64]]
) -> str:
    """Describe the first `LOGGED_CYCLES` cycles for a log line, each by its first `LOGGED_MEMBERS` members' IRIs.

    Args:
        connection: The database, to look up the IRIs.
        ids: Each node number's `entity.id`.
        cycles: The cycles, each as its members' node numbers (`Nodes.cycles`).

    Returns:
        The cycles as `{IRI, IRI, …}`, separated by `; `, each IRI shortened (`logged_iri`); `…` marks a cycle with
        more members, and `and N more` more cycles.
    """
    described = []
    for members in cycles[:LOGGED_CYCLES]:
        iris = [
            logged_iri(connection.execute("SELECT iri FROM entity WHERE id = ?", (entity,)).fetchone()[0])
            for entity in ids[members[:LOGGED_MEMBERS]].tolist()
        ]
        more = ", …" if len(members) > LOGGED_MEMBERS else ""
        described.append(f"{{{', '.join(iris)}{more}}}")
    more = f" and {len(cycles) - LOGGED_CYCLES} more" if len(cycles) > LOGGED_CYCLES else ""
    return "; ".join(described) + more


def _insert(connection: sqlite3.Connection, table: str, *columns: str | npt.NDArray[np.int64]) -> None:
    """Insert rows into a table, given as its columns: a string is the same in every row, an array a column's values.

    The rows are inserted in key order, sorted by the array columns from the first, which is cheaper than in no order.
    """
    arrays = [column for column in columns if not isinstance(column, str)]
    order = np.lexsort(arrays[::-1])
    values = [
        itertools.repeat(column) if isinstance(column, str) else memoryview(np.ascontiguousarray(column[order]))
        for column in columns
    ]
    connection.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(columns))})", zip(*values, strict=False))


class Nodes(NamedTuple):
    """A hierarchy's nodes: its entities, grouped (`group_nodes`).

    Attributes:
        representative: Each entity's group's representative, by entity number: the group's lowest number.
        edges: The (child, parent) edges between representatives, each once, without self-loops, sorted.
        cycles: The groups that merge a cycle of edges, by representative, each as its members' numbers, ascending.
            A group also holds any entities equivalent to the cycle's members.
    """

    representative: npt.NDArray[np.int64]
    edges: Edges
    cycles: list[npt.NDArray[np.int64]]


def group_nodes(count: int, edges: Iterable[tuple[int, int]], equivalent: Iterable[tuple[int, int]]) -> Nodes:
    """Group a hierarchy's entities into its nodes, and map its edges onto them.

    Equivalent entities form a group, and so do the members of a cycle of edges, which are equivalent too. Merging the
    equivalent entities may make cycles, so the cycles are found (as strongly connected components) once that's done;
    merging them leaves no cycle.

    Args:
        count: How many entities there are, numbered from 0.
        edges: The (child, parent) edges.
        equivalent: The pairs of equivalent entities.

    Returns:
        Each entity's representative, the edges between representatives, and the groups that merge cycles.
    """
    edge_array = np.asarray(edges, np.int64).reshape(-1, 2)
    representative = _lowest(count, np.asarray(equivalent, np.int64).reshape(-1, 2), directed=False)
    merged = _distinct(count, representative[edge_array])
    found = _lowest(count, merged, directed=True)
    in_cycles = np.unique(found[found != np.arange(count)])  # the representatives of the cycles' groups
    if not len(in_cycles):
        return Nodes(representative, merged, [])
    representative = found[representative]
    members = np.flatnonzero(np.isin(representative, in_cycles))  # ascending
    members = members[np.argsort(representative[members], kind="stable")]
    _, starts = np.unique(representative[members], return_index=True)
    return Nodes(representative, _distinct(count, representative[edge_array]), np.split(members, starts[1:]))


def _lowest(count: int, edges: Edges, directed: bool) -> npt.NDArray[np.int64]:
    """Find a graph's connected components: weakly connected if not directed, else strongly.

    Args:
        count: How many nodes there are, numbered from 0.
        edges: The edges.
        directed: Whether to find strongly connected components rather than weakly connected ones.

    Returns:
        Each node's component's lowest number, by node number: the node's own number if it's alone.
    """
    if not len(edges):
        return np.arange(count)
    graph = coo_array((np.ones(len(edges), np.int8), (edges[:, 0], edges[:, 1])), shape=(count, count)).tocsr()
    _, labels = connected_components(graph, directed=directed, connection="strong")
    _, first = np.unique(labels, return_index=True)  # the lowest number with each label
    return first[labels].astype(np.int64)


def _distinct(count: int, edges: Edges) -> Edges:
    """Return the edges each once, sorted by child then parent, without self-loops.

    Args:
        count: How many nodes there are, numbered from 0.
        edges: The edges, perhaps with duplicates and self-loops.
    """
    keys = np.unique(edges[edges[:, 0] != edges[:, 1]] @ np.array([count, 1], np.int64))
    return np.stack(np.divmod(keys, count), axis=1)


def count_descendants(
    count: int, edges: Iterable[tuple[int, int]], budget: int | None = None
) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.int64]]:
    """Return each node's number of children and of distinct descendants (U-8), by node number.

    A bottom-up pass in topological order (S-3) gives each node a roaring bitmap of its descendants. A node's parents
    are given its set, with the node, as soon as it's complete, each merging it into the set it's building; so only
    the sets of nodes some of whose children are still to come are held. The nodes that aren't in an edge count 0.

    Args:
        count: How many node numbers there are, from 0.
        edges: The (child, parent) edges, each once, with no cycles (`group_nodes`).
        budget: The most members to hold at once across the sets being built; `DESCENDANT_BUDGET` if not given.

    Returns:
        Two arrays by node number: each node's number of children, and its number of distinct descendants.

    Raises:
        ValueError: The edges have a cycle.
        StageError: Counting would hold more than `budget` members.
    """
    budget = DESCENDANT_BUDGET if budget is None else budget
    edge_array = np.asarray(edges, np.int64).reshape(-1, 2)
    children = np.bincount(edge_array[:, 1], minlength=count)
    # Each node's parents, as a compressed sparse row: node n's are above[start[n]:start[n + 1]].
    start = np.zeros(count + 1, np.int64)
    np.cumsum(np.bincount(edge_array[:, 0], minlength=count), out=start[1:])
    above = memoryview(edge_array[np.argsort(edge_array[:, 0], kind="stable"), 1])
    starts = memoryview(start)
    del edge_array
    pending_array = children.copy()  # children not yet merged into the node
    pending = memoryview(pending_array)
    descendants_array = np.zeros(count, np.int64)
    descendants = memoryview(descendants_array)
    sets: dict[int, BitMap] = {}  # the sets being built, each partly merged
    held = 0
    leaves = iter(np.flatnonzero(children == 0).tolist())
    ready: list[int] = []
    while (node := ready.pop() if ready else next(leaves, None)) is not None:
        found = sets.pop(node, None)  # None for a leaf, which a parent just adds
        size = 1
        if found is not None:
            held -= len(found)
            descendants[node] = len(found)
            found.add(node)
            found.run_optimize()
            size = len(found)
        given = False  # whether a parent has taken the node's own set
        for position in range(starts[node], starts[node + 1]):
            parent = above[position]
            building = sets.get(parent)
            if found is None:
                if building is None:
                    sets[parent] = BitMap((node,))
                else:
                    building.add(node)
                held += 1
            elif building is None:
                sets[parent] = found.copy() if given else found
                given = True
                held += size
            else:
                before = len(building)
                building |= found
                held += len(building) - before
            pending[parent] -= 1
            if pending[parent] == 0:
                ready.append(parent)
        if held > budget:
            raise StageError(f"counting distinct descendants would hold more than {budget:,} set members at once")
    if pending_array.any():
        raise ValueError("the hierarchy has a cycle")
    return children, descendants_array
