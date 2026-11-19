"""The hierarchies pre-caching hook (P-12): the `equiv_group`, `edge` and `counts` tables (DESIGN.md §2.5)."""

import functools
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pyroaring import BitMap

from browl.derived import EntityType
from browl.precache.core import PreCacheContext, batched, hook
from browl.rdf import Term

logger = logging.getLogger(__name__)

OWL = "http://www.w3.org/2002/07/owl#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"

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
    f"{RDFS}subPropertyOf": HIERARCHIES[1:],
    EQUIVALENT_CLASS: (EntityType.CLASS,),
}
"""The hierarchies each of the edges query's relations links entities in."""

OUTSIDE = (
    f"{OWL}Thing",
    f"{OWL}Nothing",
    f"{OWL}topObjectProperty",
    f"{OWL}bottomObjectProperty",
    f"{OWL}topDataProperty",
    f"{OWL}bottomDataProperty",
)
"""The top and bottom entities, which are in no hierarchy, even if declared: so `owl:Thing`'s children are roots."""


@hook()
async def hierarchies(context: PreCacheContext) -> None:
    """Fill `equiv_group`, `edge` and `counts` for the class hierarchy and each property hierarchy.

    The edges come from one bulk query (`EDGES`), over the imports too; an edge whose ends aren't declared entities of
    a hierarchy's type, or are `OUTSIDE` it, is left out. Classes linked by `owl:equivalentClass`, and the members of a
    cycle of edges, form one group, a single node (`group_nodes`). Each node's counts come from `count_descendants`.
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
        "CREATE TEMP TABLE edge_staging (child TEXT NOT NULL, relation TEXT NOT NULL, parent TEXT NOT NULL)"
    )


def _stage(batch: list[tuple[Term | None, ...]], columns: tuple[int, ...], connection: sqlite3.Connection) -> None:
    """Stage a batch of the edges query's rows, on the database's thread."""
    child, relation, parent = columns
    connection.executemany(
        "INSERT INTO edge_staging VALUES (?, ?, ?)",
        ((str(row[child]), str(row[relation]), str(row[parent])) for row in batch),
    )


def _fill(hierarchy: EntityType, connection: sqlite3.Connection) -> None:
    """Fill a hierarchy's rows of `equiv_group`, `edge` and `counts`."""
    outside = ", ".join("?" * len(OUTSIDE))
    # Nodes are numbered in IRI order, so a group's representative, its lowest number, is its first IRI.
    ids = [
        row[0]
        for row in connection.execute(
            f"SELECT id FROM entity WHERE type = ? AND iri NOT IN ({outside}) ORDER BY iri", (hierarchy, *OUTSIDE)
        )
    ]
    number = {entity: n for n, entity in enumerate(ids)}
    relations = [iri for iri, types in RELATION_TYPES.items() if hierarchy in types]
    rows = connection.execute(
        "SELECT c.id, s.relation, p.id FROM edge_staging AS s"
        " JOIN entity AS c ON c.iri = s.child AND c.type = ?1 JOIN entity AS p ON p.iri = s.parent AND p.type = ?1"
        f" WHERE s.relation IN ({', '.join('?' * len(relations))})",
        (hierarchy, *relations),
    )
    edges: list[tuple[int, int]] = []
    equivalent: list[tuple[int, int]] = []
    for child, relation, parent in rows:
        if child in number and parent in number:
            pair = (number[child], number[parent])
            (equivalent if relation == EQUIVALENT_CLASS else edges).append(pair)
    nodes = group_nodes(len(ids), edges, equivalent)
    if nodes.cycles:
        logger.warning(
            "The %s hierarchy has %d cycles; each one's members are shown as one node", hierarchy, nodes.cycles
        )
    connection.executemany(
        "INSERT INTO equiv_group (entity, representative) VALUES (?, ?)",
        sorted((ids[n], ids[rep]) for n, rep in enumerate(nodes.representative) if nodes.size[rep] > 1),
    )
    connection.executemany(
        "INSERT INTO edge (hierarchy, parent, child) VALUES (?, ?, ?)",
        sorted((hierarchy.value, ids[parent], ids[child]) for child, parent in nodes.edges),
    )
    children, descendants = count_descendants(len(ids), nodes.edges)
    connection.executemany(
        "INSERT INTO counts (hierarchy, node, children, descendants) VALUES (?, ?, ?, ?)",
        sorted(
            (hierarchy.value, ids[n], children[n], descendants[n])
            for n, rep in enumerate(nodes.representative)
            if rep == n
        ),
    )


@dataclass(frozen=True)
class Nodes:
    """A hierarchy's nodes: its entities, grouped (`group_nodes`).

    Attributes:
        representative: Each entity's group's representative, by entity number: the group's lowest number.
        size: Each representative's group's size, by its number; 0 for any other entity.
        edges: The (child, parent) edges between representatives, each once, without self-loops.
        cycles: How many groups merged cycles of edges.
    """

    representative: list[int]
    size: list[int]
    edges: list[tuple[int, int]]
    cycles: int


def group_nodes(count: int, edges: Iterable[tuple[int, int]], equivalent: Iterable[tuple[int, int]]) -> Nodes:
    """Group a hierarchy's entities into its nodes, and map its edges onto them.

    Equivalent entities form a group (union-find), and so do the members of a cycle of edges, which are equivalent too.

    Args:
        count: How many entities there are, numbered from 0.
        edges: The (child, parent) edges.
        equivalent: The pairs of equivalent entities.
    """
    edges = list(edges)
    parent = list(range(count))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: int, b: int) -> None:
        a, b = find(a), find(b)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for a, b in equivalent:
        union(a, b)
    cycles = 0
    while True:
        mapped = sorted({(c, p) for c, p in ((find(c), find(p)) for c, p in edges) if c != p})
        found = strongly_connected(mapped)
        if not found:
            break
        cycles += len(found)
        for component in found:
            for member in component[1:]:
                union(component[0], member)
    representative = [find(node) for node in range(count)]
    size = [0] * count
    for rep in representative:
        size[rep] += 1
    return Nodes(representative, size, mapped, cycles)


def strongly_connected(edges: Sequence[tuple[int, int]]) -> list[list[int]]:
    """Return the components of more than one node, i.e. the cycles, of a graph given by its edges (Tarjan's).

    Iterative, so a deep hierarchy can't exceed Python's recursion limit.
    """
    successors: dict[int, list[int]] = {}
    for a, b in edges:
        successors.setdefault(a, []).append(b)
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    stack: list[int] = []
    on_stack: set[int] = set()
    components: list[list[int]] = []
    for start in successors:
        if start in index:
            continue
        work = [(start, 0)]
        while work:
            node, position = work.pop()
            if position == 0:
                index[node] = low[node] = len(index)
                stack.append(node)
                on_stack.add(node)
            following = successors.get(node, [])
            if position < len(following):
                work.append((node, position + 1))
                successor = following[position]
                if successor not in index:
                    work.append((successor, 0))
                elif successor in on_stack:
                    low[node] = min(low[node], index[successor])
                continue
            if work:  # pass the low link up to the node that visited this one
                caller = work[-1][0]
                low[caller] = min(low[caller], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    components.append(component)
    return components


def count_descendants(count: int, edges: Iterable[tuple[int, int]]) -> tuple[list[int], list[int]]:
    """Return each node's number of children and of distinct descendants (U-8), by node number.

    A bottom-up pass in topological order merges each node's children's descendant sets, as roaring bitmaps, freeing
    each set once all its parents have merged it (S-3). The nodes that aren't in an edge count 0.

    Args:
        count: How many node numbers there are, from 0.
        edges: The (child, parent) edges, each once, with no cycles (`group_nodes`).

    Raises:
        ValueError: The edges have a cycle.
    """
    children: list[list[int]] = [[] for _ in range(count)]
    parents: list[list[int]] = [[] for _ in range(count)]
    for child, parent in edges:
        children[parent].append(child)
        parents[child].append(parent)
    pending = [len(each) for each in children]  # children not yet merged into the node
    waiting = [len(each) for each in parents]  # parents that haven't yet merged the node
    sets: dict[int, BitMap] = {}
    descendants = [0] * count
    ready = [node for node in range(count) if pending[node] == 0]
    while ready:
        node = ready.pop()
        found = BitMap()
        for child in children[node]:
            found |= sets[child]
            found.add(child)
            waiting[child] -= 1
            if waiting[child] == 0:
                del sets[child]
        descendants[node] = len(found)
        if parents[node]:
            found.run_optimize()
            sets[node] = found
            for parent in parents[node]:
                pending[parent] -= 1
                if pending[parent] == 0:
                    ready.append(parent)
    if any(pending):
        raise ValueError("the hierarchy has a cycle")
    return [len(each) for each in children], descendants
