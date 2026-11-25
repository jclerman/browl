"""The hierarchy column on entity pages (U-45; DESIGN.md §2.9 "Hierarchy column").

The column shows the entity's ancestors as one merged tree, so its size grows with the number of ancestors, not with
the number of paths. Each node is drawn once, under its first parent by display name, and under each other parent as a
one-line reference to it. The entity's direct children are listed below it, closed, and a link after its first parent's
children shown fetches its siblings in their place. The nodes are the tree pages'
(`browl.trees`), but their links name no open nodes (`browl.trees.column_node`). They're read from SQLite alone: the
ancestors by walking up `edge` through its `edge_child` index.
"""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import chain

from fastapi import Request

from browl.derived import Entity, EntityType
from browl.hierarchy import ChildPage, node_ids, read_nodes
from browl.pages import TREES, TreePage, link_root, tree_href, tree_path
from browl.trees import ShownList, ShownNode, column_node, shown_children, siblings_link

COLUMN_IDS = {
    EntityType.CLASS: "hc",
    EntityType.OBJECT_PROPERTY: "ho",
    EntityType.DATA_PROPERTY: "hd",
    EntityType.ANNOTATION_PROPERTY: "ha",
}
"""Each hierarchy's prefix of its column tree's element ids, unlike the tree pages' (`browl.trees.ROOT_ID`), so a
punned IRI's trees can share the page."""

HEADING = "Local hierarchy"
"""The heading of the column's tree, which shows the entity's ancestors but not their siblings; for each of a punned
IRI's trees, followed by its hierarchy's name."""


@dataclass(frozen=True)
class ColumnTree:
    """A hierarchy's tree in the column.

    Attributes:
        tree: The hierarchy's tree page.
        heading: The tree's heading (`HEADING`).
        href: The link to its tree page, after the heading.
        id: The prefix of the tree's element ids; its heading's id is `{id}-h`.
        roots: The tree's roots, as shown.
    """

    tree: TreePage
    heading: str
    href: str
    id: str
    roots: ShownList


def read_column(request: Request, connection: sqlite3.Connection, entities: Sequence[Entity]) -> list[ColumnTree]:
    """Return the hierarchy column of an IRI's page, given its entities, one of each type.

    Returns:
        A tree for each hierarchy the IRI is in, in the navigation bar's order: a punned IRI may be in several.
    """
    found = []
    for tree in TREES:
        entity = next((each for each in entities if each.type == tree.hierarchy), None)
        roots = None if entity is None else _merged_tree(request, connection, tree, entity.iri)
        if roots is not None:
            found.append((tree, roots))
    return [
        ColumnTree(
            tree,
            HEADING if len(found) == 1 else f"{HEADING}: {tree.label}",
            tree_href(request, tree),
            COLUMN_IDS[tree.hierarchy],
            roots,
        )
        for tree, roots in found
    ]


def _merged_tree(request: Request, connection: sqlite3.Connection, tree: TreePage, iri: str) -> ShownList | None:
    """Return the roots of an entity's merged tree, as shown; None if it isn't a node of the tree's hierarchy."""
    hierarchy = tree.hierarchy
    entity = node_ids(connection, hierarchy, [iri]).get(iri)
    if entity is None:
        return None
    parents = _parents(connection, hierarchy, entity)
    prefix: str = request.app.state.settings.reserved_prefix
    nodes = read_nodes(connection, hierarchy, parents, prefix, link_root(request.scope))
    by_id = {node.id: node for node in nodes}
    rank = {node.id: index for index, node in enumerate(nodes)}
    first = {node: min(above, key=rank.__getitem__) for node, above in parents.items() if above}
    under: dict[int, list[int]] = {}  # each node's children in the tree, in order, as `nodes` is
    for node in nodes:
        for parent in parents[node.id]:
            under.setdefault(parent, []).append(node.id)
    roots = [node.id for node in nodes if not parents[node.id]]
    page_path = tree_path(prefix, tree)
    id_prefix = COLUMN_IDS[hierarchy]

    places: dict[int, tuple[int, ...]] = {}  # where each node is drawn, as its indices from the roots down

    def place(node: int, at: tuple[int, ...]) -> None:
        places[node] = at
        for index, child in enumerate(under.get(node, ())):
            if first[child] == node:
                place(child, (*at, index))

    def shown(node: int, at: tuple[int, ...], above: list[tuple[int, str]]) -> ShownNode:
        """Return a node drawn at a place, its children shown open, given the nodes above it, by id and IRI."""
        item = by_id[node]
        element_id = id_prefix + "".join(f"-{index}" for index in at)
        if not item.children:
            return ShownNode(item, element_id, None, None, current=node == entity)
        path = [*above, (node, item.link.iri)]
        if node == entity:
            children = shown_children(request, connection, tree, item, element_id, path)
            return column_node(request, page_path, item, element_id, children, current=True)
        shown_list = []
        for index, child in enumerate(under[node]):
            child_at = (*at, index)
            if first[child] == node:
                shown_list.append(shown(child, child_at, path))
            else:
                child_id = id_prefix + "".join(f"-{each}" for each in child_at)
                drawn = "above" if places[child] < child_at else "below"
                shown_list.append(ShownNode(by_id[child], child_id, None, None, drawn=drawn))
        page = ChildPage(nodes=[by_id[child] for child in under[node]], offset=0, total=len(shown_list))
        siblings = None
        if first.get(entity) == node:  # the entity's siblings: the rest of its first parent's children
            siblings = siblings_link(request, connection, tree, item, element_id, by_id[entity])
        return column_node(request, page_path, item, element_id, ShownList(page, shown_list, siblings=siblings))

    for index, root in enumerate(roots):
        place(root, (index,))
    shown_roots = [shown(root, (index,), []) for index, root in enumerate(roots)]
    siblings = None if parents[entity] else siblings_link(request, connection, tree, None, id_prefix, by_id[entity])
    page = ChildPage(nodes=[by_id[root] for root in roots], offset=0, total=len(roots))
    return ShownList(page, shown_roots, siblings=siblings)


def _parents(connection: sqlite3.Connection, hierarchy: EntityType, node: int) -> dict[int, list[int]]:
    """Return the parents of a node and of each of its ancestors, by node, walking up `edge` a level at a time."""
    parents: dict[int, list[int]] = {}
    level = [node]
    while level:
        found: dict[int, list[int]] = {each: [] for each in level}
        rows = connection.execute(
            "SELECT child, parent FROM edge WHERE hierarchy = ? AND child IN (SELECT value FROM json_each(?))",
            (hierarchy, json.dumps(level)),
        )
        for child, parent in rows:
            found[child].append(parent)
        parents.update(found)
        level = [each for each in dict.fromkeys(chain.from_iterable(found.values())) if each not in parents]
    return parents
