"""Hierarchy nodes and their children (U-7–U-9), from the derived database's `edge`, `counts`, `root` and `equiv_group`.

A node is an entity of the hierarchy's type, or a group of equivalent classes, which its representative stands for
(DESIGN.md §2.5). Children and roots are read a page at a time, by display name, as a node can have tens of thousands
(NCBITaxon). The tree pages (`browl.trees`) build on this, and so will the hierarchy column (P-38) and the JSON API
(P-33).
"""

import sqlite3
from collections.abc import Iterable
from itertools import batched

from pydantic import BaseModel, ConfigDict

from browl.derived import EntityType
from browl.links import LOOKUP_BATCH, EntityLink, read_entity_links

CHILD_PAGE = 500
"""The most children, or roots, read and shown at a time; "Show more" reads the next page."""

MAX_GROUP_NAMES = 3
"""The most members of an equivalence group a node's label names, its representative included."""

_ORDER = "coalesce(e.display_name, e.iri) COLLATE NOCASE, e.iri"
"""The order of a node's children, and of a group's members: by display name, then IRI, so it's total."""


class TreeNode(BaseModel):
    """A hierarchy node, as a tree shows it.

    Attributes:
        id: Its representative's `entity.id`, which stands for it in `edge` and `counts`.
        link: Its representative's link, labeled with its display name.
        equivalents: The other members of its equivalence group that its label names, by name (`MAX_GROUP_NAMES`);
            empty if it's no group.
        more_equivalents: How many other members its label doesn't name.
        children: How many immediate children it has (U-7).
        descendants: How many distinct descendants it has (U-8).
    """

    model_config = ConfigDict(frozen=True)

    id: int
    link: EntityLink
    equivalents: list[EntityLink] = []
    more_equivalents: int = 0
    children: int
    descendants: int


class ChildPage(BaseModel):
    """A page of a node's children, or of a hierarchy's roots, by display name.

    Attributes:
        nodes: The page's nodes.
        offset: How many come before them.
        total: How many there are in all.
    """

    model_config = ConfigDict(frozen=True)

    nodes: list[TreeNode]
    offset: int
    total: int

    @property
    def end(self) -> int:
        """Return how many come before the next page."""
        return self.offset + len(self.nodes)

    @property
    def previous_count(self) -> int:
        """Return how many before it the page before it shows: up to `CHILD_PAGE`."""
        return min(self.offset, CHILD_PAGE)

    @property
    def next_count(self) -> int:
        """Return how many the page after it shows: up to `CHILD_PAGE`."""
        return min(self.total - self.end, CHILD_PAGE)


def node_ids(connection: sqlite3.Connection, hierarchy: EntityType, iris: Iterable[str]) -> dict[str, int]:
    """Return the node each IRI is in, by IRI: its group's representative's `entity.id`, or its own.

    An IRI with no entity of the hierarchy's type, or one in no hierarchy (`owl:Thing`, which has no `counts` row), is
    left out.
    """
    wanted = list(dict.fromkeys(iris))
    found: dict[str, int] = {}
    for batch in batched(wanted, LOOKUP_BATCH):
        rows = connection.execute(
            "SELECT e.iri, c.node FROM entity AS e LEFT JOIN equiv_group AS g ON g.entity = e.id"
            " JOIN counts AS c ON c.hierarchy = e.type AND c.node = coalesce(g.representative, e.id)"
            f" WHERE e.type = ? AND e.iri IN ({', '.join('?' * len(batch))})",
            (hierarchy, *batch),
        )
        found.update(rows)
    return found


def node_iris(connection: sqlite3.Connection, nodes: Iterable[int]) -> dict[int, str]:
    """Return the IRI of each node's representative, by its `entity.id` (`node_ids`)."""
    found: dict[int, str] = {}
    for batch in batched(dict.fromkeys(nodes), LOOKUP_BATCH):
        rows = connection.execute(f"SELECT id, iri FROM entity WHERE id IN ({', '.join('?' * len(batch))})", batch)
        found.update(rows)
    return found


def read_node(
    connection: sqlite3.Connection, hierarchy: EntityType, node: int, prefix: str, root: str
) -> TreeNode | None:
    """Return a node by its representative's `entity.id`; None if the hierarchy has no such node.

    Args:
        connection: The derived database.
        hierarchy: The hierarchy, by its entities' type.
        node: The node's representative (`node_ids`).
        prefix: The reserved prefix, for links.
        root: The app's root path, for links (`browl.pages.link_root`).
    """
    rows = connection.execute(
        "SELECT c.node, e.iri, c.children, c.descendants FROM counts AS c JOIN entity AS e ON e.id = c.node"
        " WHERE c.hierarchy = ? AND c.node = ?",
        (hierarchy, node),
    ).fetchall()
    nodes = _nodes(connection, rows, prefix, root)
    return nodes[0] if nodes else None


def read_children(
    connection: sqlite3.Connection,
    hierarchy: EntityType,
    parent: TreeNode | None,
    prefix: str,
    root: str,
    offset: int = 0,
    limit: int = CHILD_PAGE,
) -> ChildPage:
    """Return a page of a node's children, or of the hierarchy's roots, by display name, then IRI.

    Args:
        connection: The derived database.
        hierarchy: The hierarchy, by its entities' type.
        parent: The node; None for the roots.
        prefix: The reserved prefix, for links.
        root: The app's root path, for links (`browl.pages.link_root`).
        offset: How many to skip.
        limit: The most to return.
    """
    params: list[str | int]
    columns = "c.node, e.iri, c.children, c.descendants"
    counts = "JOIN counts AS c ON c.hierarchy = r.hierarchy AND c.node = e.id"
    if parent is None:
        total = connection.execute("SELECT count(*) FROM root WHERE hierarchy = ?", (hierarchy,)).fetchone()[0]
        source, params = "root AS r JOIN entity AS e ON e.id = r.node", [hierarchy]
        where = "r.hierarchy = ?"
    else:
        total = parent.children
        source, params = "edge AS r JOIN entity AS e ON e.id = r.child", [hierarchy, parent.id]
        where = "r.hierarchy = ? AND r.parent = ?"
    rows = connection.execute(
        f"SELECT {columns} FROM {source} {counts} WHERE {where} ORDER BY {_ORDER} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return ChildPage(nodes=_nodes(connection, rows, prefix, root), offset=offset, total=total)


def _nodes(
    connection: sqlite3.Connection, rows: list[tuple[int, str, int, int]], prefix: str, root: str
) -> list[TreeNode]:
    """Return the nodes for rows of (representative, its IRI, children, descendants), with their groups and links."""
    groups = _group_members(connection, [row[0] for row in rows])
    iris = [row[1] for row in rows] + [iri for members, _ in groups.values() for iri in members]
    links = read_entity_links(connection, iris, prefix, root)
    nodes = []
    for node, iri, children, descendants in rows:
        members, more = groups.get(node, ([], 0))
        equivalents = [links[member] for member in members]
        nodes.append(
            TreeNode(
                id=node,
                link=links[iri],
                equivalents=equivalents,
                more_equivalents=more,
                children=children,
                descendants=descendants,
            )
        )
    return nodes


def _group_members(connection: sqlite3.Connection, nodes: list[int]) -> dict[int, tuple[list[str], int]]:
    """Return the other members of each node's equivalence group that its label names, by name, and how many more.

    Returns:
        For each node that's a group, by representative, the IRIs of its first `MAX_GROUP_NAMES - 1` other members,
        and how many others there are beyond them.
    """
    groups: dict[int, tuple[list[str], int]] = {}
    for batch in batched(nodes, LOOKUP_BATCH):
        rows = connection.execute(
            "SELECT representative, iri, size FROM (SELECT g.representative, e.iri,"
            f"  row_number() OVER (PARTITION BY g.representative ORDER BY {_ORDER}) AS number,"
            "  count(*) OVER (PARTITION BY g.representative) AS size"
            "  FROM equiv_group AS g JOIN entity AS e ON e.id = g.entity"
            f"  WHERE g.representative IN ({', '.join('?' * len(batch))}) AND g.entity != g.representative)"
            " WHERE number < ? ORDER BY representative, number",
            (*batch, MAX_GROUP_NAMES),
        )
        for node, iri, size in rows:
            members, _ = groups.setdefault(node, ([], 0))
            members.append(iri)
            groups[node] = (members, size - len(members))
    return groups
