"""The hierarchy column on entity pages (U-45; DESIGN.md §2.9 "Hierarchy column").

The entity's ancestors as one merged tree, each drawn once, under its first parent by display name, from SQLite alone.
The nodes are the tree pages' (`browl.trees`), with links that name no open nodes.
"""

import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from fastapi import Request

from browl.derived import Entity
from browl.hierarchy import (
    ChildPage,
    TreeNode,
    child_index,
    count_roots,
    node_ids,
    page_offset,
    read_ancestors,
    read_nodes,
)
from browl.pages import TREES, TreePage, link_root, tree_href, tree_path
from browl.trees import (
    COLUMN_IDS,
    MAX_OPEN,
    ROOT_ID,
    Elided,
    ShownList,
    ShownNode,
    Siblings,
    column_node,
    fragment_link,
    page_link,
    shown_children,
)

HEADING = "Local hierarchy"
"""The heading of the column's tree; for each of a punned IRI's trees, followed by its hierarchy's name."""

MAX_COLUMN_DEPTH = 15
"""The most levels the column draws down to the entity's, its own counted; past it, a row stands for those between
the top `TOP_LEVELS` and the levels nearest the entity (`browl.trees.Elided`)."""

TOP_LEVELS = 3
"""How many levels from the top the column keeps when it leaves levels out (`MAX_COLUMN_DEPTH`)."""


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


def siblings_link(
    request: Request,
    connection: sqlite3.Connection,
    tree: TreePage,
    parent: TreeNode | None,
    parent_id: str,
    node: TreeNode,
) -> Siblings | None:
    """Return the column's link to a node's siblings; None if it has none.

    Args:
        request: The request.
        connection: The derived database.
        tree: The node's tree page.
        parent: Its first parent, under which the column draws it; None for a root.
        parent_id: Its parent's element id, or for a root, the roots' ids' prefix.
        node: The node.
    """
    count = (count_roots(connection, tree.hierarchy) if parent is None else parent.children) - 1
    if count < 1:
        return None
    page_path = tree_path(request.app.state.settings.reserved_prefix, tree)
    load = fragment_link(request, page_path, parent, parent_id, [], siblings_of=node.link.iri)
    if parent is not None:  # its page's column shows its children
        return Siblings(count, parent.link.href or page_link(request, page_path, [], None, None, 0), load)
    index = child_index(connection, tree.hierarchy, None, node.id) or 0
    return Siblings(count, page_link(request, page_path, [], f"{ROOT_ID}-{index}", None, page_offset(index)), load)


def _merged_tree(request: Request, connection: sqlite3.Connection, tree: TreePage, iri: str) -> ShownList | None:
    """Return the roots of an entity's merged tree, as shown; None if it isn't a node of the tree's hierarchy.

    It draws `MAX_COLUMN_DEPTH` levels at most. When the entity is deeper, a row under each node of the last of the top
    `TOP_LEVELS` stands for the levels down to those nearest the entity, and holds the nodes and references below it
    at the first of those; the rest at the levels it stands for are left out. Below the entity's level, or the last
    drawn, if deeper, nodes are drawn closed.
    """
    hierarchy = tree.hierarchy
    entity = node_ids(connection, hierarchy, [iri]).get(iri)
    if entity is None:
        return None
    parents = read_ancestors(connection, hierarchy, entity)
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
    for index, root in enumerate(roots):
        stack: list[tuple[int, tuple[int, ...]]] = [(root, (index,))]
        while stack:
            placed, at = stack.pop()
            places[placed] = at
            stack += [(child, (*at, i)) for i, child in enumerate(under.get(placed, [])) if first[child] == placed]

    def chain(node: int) -> list[tuple[int, str]]:
        """Return the nodes from a root down to a node, along first parents, by id and IRI."""
        found = [node]
        while found[-1] in first:
            found.append(first[found[-1]])
        return [(each, by_id[each].link.iri) for each in reversed(found)]

    depth = len(places[entity]) - 1
    last = max(depth, MAX_COLUMN_DEPTH - 1)  # the deepest level drawn
    band = None if depth < MAX_COLUMN_DEPTH else (TOP_LEVELS, depth - (MAX_COLUMN_DEPTH - TOP_LEVELS - 1) + 1)

    def below(node: int, at: tuple[int, ...], level: int) -> Iterator[tuple[int, tuple[int, ...]]]:
        """Yield the nodes and references drawn under a node at a level, in order, and their places."""
        for index, child in enumerate(under.get(node, ())):
            child_at = (*at, index)
            if len(child_at) - 1 == level:
                yield child, child_at
            elif first[child] == node:
                yield from below(child, child_at, level)

    def reference(node: int, at: tuple[int, ...], element_id: str) -> ShownNode:
        drawn = "above" if places[node] < at else "below"
        return ShownNode(by_id[node], element_id, None, None, drawn=drawn)

    def shown(node: int, at: tuple[int, ...], element_id: str) -> ShownNode:
        """Return a node drawn at a place, given its element id, its children shown open."""
        item = by_id[node]
        if node == entity:
            if not item.children:
                return ShownNode(item, element_id, None, None, current=True)
            children = shown_children(request, connection, tree, item, element_id, chain(node))
            return column_node(request, page_path, item, element_id, children, current=True)
        level = len(at) - 1
        if level == last:
            return column_node(request, page_path, item, element_id)
        if band is not None and level == band[0] - 1:
            return elided(node, at, element_id)
        shown_list: list[ShownNode | Elided] = []
        for index, child in enumerate(under[node]):
            child_at, child_id = (*at, index), f"{element_id}-{index}"
            is_first = first[child] == node
            shown_list.append(shown(child, child_at, child_id) if is_first else reference(child, child_at, child_id))
        page = ChildPage(nodes=[by_id[child] for child in under[node]], offset=0, total=len(shown_list))
        siblings = None
        if first.get(entity) == node:  # the entity's siblings: the rest of its first parent's children
            siblings = siblings_link(request, connection, tree, item, element_id, by_id[entity])
        return column_node(request, page_path, item, element_id, ShownList(page, shown_list, siblings=siblings))

    def elided(node: int, at: tuple[int, ...], element_id: str) -> ShownNode:
        """Return a node of the last top level, with a row below it for the levels left out, holding those after."""
        assert band is not None
        item = by_id[node]
        drawn = list(below(node, at, band[1]))
        if not drawn:
            return column_node(request, page_path, item, element_id)
        row_id = f"{element_id}-0"
        held = [
            (shown if places[child] == child_at else reference)(child, child_at, f"{row_id}-{index}")
            for index, (child, child_at) in enumerate(drawn)
        ]
        opened = [each_iri for _, each_iri in chain(entity)[:-1]][:MAX_OPEN]
        row = Elided(
            row_id,
            band[1] - band[0],
            page_link(request, page_path, opened, None, None, 0),
            ShownList(ChildPage(nodes=[by_id[child] for child, _ in drawn], offset=0, total=len(held)), held),
        )
        children = ShownList(ChildPage(nodes=[], offset=0, total=1), [row])
        return column_node(request, page_path, item, element_id, children)

    shown_roots: list[ShownNode | Elided] = [
        shown(root, (index,), f"{id_prefix}-{index}") for index, root in enumerate(roots)
    ]
    siblings = None if parents[entity] else siblings_link(request, connection, tree, None, id_prefix, by_id[entity])
    page = ChildPage(nodes=[by_id[root] for root in roots], offset=0, total=len(roots))
    return ShownList(page, shown_roots, siblings=siblings)
