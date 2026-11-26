"""The tree pages, `tree/{hierarchy}`, and their htmx fragments (U-7–U-9, U-17; DESIGN.md §2.9 "Tree pages")."""

import asyncio
import re
import sqlite3
import urllib.parse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace

from fastapi import APIRouter, Request
from fastapi.responses import Response

from browl.derived import EntityType
from browl.hierarchy import (
    CHILD_PAGE,
    ChildPage,
    TreeNode,
    child_index,
    count_roots,
    node_ids,
    node_iris,
    page_offset,
    read_ancestors,
    read_children,
    read_node,
    read_nodes,
)
from browl.pages import (
    PANEL_HEADERS,
    TREES,
    TreePage,
    href,
    link_root,
    not_found,
    read_derived,
    read_offset,
    single_params,
    templates,
    tree_path,
)
from browl.tabs import with_query

TREE_PAGES = {tree.slug: tree for tree in TREES}
"""Each tree page, by its path segment under `tree/`."""

OPEN_PARAM = "open"
"""The query parameter, repeated, that names each node a tree page shows open, by IRI."""

NODE_PARAM = "node"
"""The query parameter that asks for a node's children alone, as an htmx fragment, by its IRI."""

AT_PARAM = "at"
"""The query parameter that gives the fragment's node's element id, from which its children's ids follow."""

CHILDREN_OF_PARAM = "children-of"
"""The query parameter that names the node, by IRI, whose children a tree page shows from `offset`; else the roots."""

OFFSET_PARAM = "offset"
"""The query parameter that says how many children, or roots, come before those shown; absent for none."""

SIBLINGS_PARAM = "siblings-of"
"""The query parameter that asks the hierarchy column's fragment for the page of `node`'s children, or of the roots,
that holds this node, by IRI: the page's entity, which is shown open and marked."""

BEFORE_PARAM = "before"
"""The query parameter that asks the hierarchy column's fragment for the page of `node`'s children, or of the roots,
before the one at this offset, which it goes before."""

MAX_OPEN = 100
"""The most open nodes a page takes from its URL; the rest are ignored."""

MAX_SHOWN = 5_000
"""About the most nodes a page or fragment shows; past it, the open nodes left are shown closed (§2.9, Limits)."""

MAX_LINK_CHARS = 2_000_000
"""About the most characters naming open nodes a page's, or fragment's, links hold in all, as each toggle names them."""

MAX_CHILDREN_READ = 100_000
"""About the most children, or roots, a page's or fragment's reads sort in all, as each sorts all of its node's."""

ROOT_ID = "t"
"""The element id the roots' ids follow from: `t-0`, `t-1`, …; a node's children's from its own, `t-0-0`, …."""

COLUMN_IDS = {
    EntityType.CLASS: "hc",
    EntityType.OBJECT_PROPERTY: "ho",
    EntityType.DATA_PROPERTY: "hd",
    EntityType.ANNOTATION_PROPERTY: "ha",
}
"""Each hierarchy's prefix of the hierarchy column's element ids (`browl.hierarchy_column`), in place of `ROOT_ID`, so
a punned IRI's trees can share the page."""

MAX_AT_CHARS = 256
"""The longest element id `at=…` may give, as each child's ids and links repeat it."""

_ELEMENT_ID = re.compile(r"[a-z]+(?:-[0-9]{1,9})+")
"""An element id `at=…` may give, as `ShownNode.id` makes them, its prefix one of `_ID_PREFIXES`."""

_ROOTS_ID = re.compile(r"[a-z]+")
"""The prefix of the roots' ids `at=…` may give, without `node=`: one of `_ID_PREFIXES`."""

_ID_PREFIXES = {ROOT_ID, *COLUMN_IDS.values()}
"""The prefixes of element ids: the tree pages', and the hierarchy column's."""

router = APIRouter()
"""The tree routes, under the reserved prefix."""


@dataclass(frozen=True)
class ShownNode:
    """A node as a tree shows it.

    Attributes:
        node: The node.
        id: Its element id, unique on the page; its children's follow from it.
        toggle: The link that opens or closes it without JS; None for a leaf.
        load: The URL of its children's fragment, for htmx; None for a leaf.
        children: Its children shown, if it's open; else None.
        current: Whether it's the node of the page's entity, in the hierarchy column.
        drawn: For a reference in the hierarchy column to a node drawn elsewhere in it, where: "above" or "below";
            else None.
    """

    node: TreeNode
    id: str
    toggle: str | None
    load: str | None
    children: "ShownList | None" = None
    current: bool = False
    drawn: str | None = None


@dataclass(frozen=True)
class ShownList:
    """A page of a node's children, or of the roots, as a tree shows it.

    Attributes:
        page: The page.
        nodes: Its nodes.
        previous: The link to the page before it, without JS; None for the first, and in a fragment, which goes
            after the page before it, except in the hierarchy column's siblings' and `before=` fragments.
        more: The link to the page after it, without JS; None for the last, and in a `before=` fragment.
        more_load: The URL of the next page's fragment, for htmx, which adds it in place; None if `more` is.
        siblings: In the hierarchy column, after the page's entity's first parent's children: its siblings' link.
        previous_load: In the hierarchy column, the URL of the page before's fragment (`BEFORE_PARAM`), which htmx
            adds in place; None if `previous` is.
    """

    page: ChildPage
    nodes: "Sequence[ShownNode | Elided]"
    previous: str | None = None
    more: str | None = None
    more_load: str | None = None
    siblings: "Siblings | None" = None
    previous_load: str | None = None


@dataclass(frozen=True)
class Elided:
    """The hierarchy column's row that stands for the levels it leaves out (`browl.hierarchy_column.MAX_COLUMN_DEPTH`).

    Attributes:
        id: Its element id, unique on the page; the ids of the nodes below it follow from it.
        levels: How many levels it stands for.
        href: The link to the tree page, with the path to the page's entity open.
        children: The nodes drawn below the levels left out.
    """

    id: str
    levels: int
    href: str
    children: ShownList


@dataclass(frozen=True)
class Siblings:
    """The hierarchy column's link that shows the page's entity's siblings, after its first parent's children shown.

    Attributes:
        count: How many siblings it has.
        href: Without JS, the page that shows them: its parent's, or the tree page with the roots.
        load: The URL of the fragment that shows them, in place of its parent's children shown (`SIBLINGS_PARAM`).
    """

    count: int
    href: str
    load: str


@dataclass(frozen=True)
class _Query:
    """What a tree page's URL asks for.

    Attributes:
        open: The open nodes' IRIs, in order, each once, at most `MAX_OPEN`.
        children_of: The IRI of the node whose children are shown from `offset`; None for the roots.
        offset: How many of its children, or of the roots, come before those shown.
        node: The IRI of the node whose children alone are asked for, as a fragment; None for the page, or the roots.
        at: For a fragment, its node's element id, or for the roots, their ids' prefix; None for the page.
        siblings_of: For the hierarchy column's fragment, the IRI of the node whose page of siblings it shows.
        before: For the hierarchy column's fragment, the offset of the page after the one it shows; else 0.
    """

    open: list[str]
    children_of: str | None = None
    offset: int = 0
    node: str | None = None
    at: str | None = None
    siblings_of: str | None = None
    before: int = 0


def _read_query(request: Request) -> _Query | None:
    """Return what a tree page's URL asks for; None if it repeats a parameter, or gives one that isn't valid."""
    names = (CHILDREN_OF_PARAM, OFFSET_PARAM, NODE_PARAM, AT_PARAM, SIBLINGS_PARAM, BEFORE_PARAM)
    single = single_params(request, names)
    if single is None:
        return None
    children_of, offset = single[CHILDREN_OF_PARAM], single[OFFSET_PARAM]
    node, at, siblings_of = single[NODE_PARAM], single[AT_PARAM], single[SIBLINGS_PARAM]
    number, before = read_offset(offset), read_offset(single[BEFORE_PARAM])
    if number is None or before is None or before % CHILD_PAGE:
        return None
    if siblings_of is not None and before:
        return None
    if (siblings_of is not None or before) and (at is None or not is_column_id(at) or offset is not None):
        return None  # the column's, which give the page by these, not by its offset
    if at is None:  # the page
        if node is not None or (children_of is not None and offset is None):
            return None
    elif (
        children_of is not None
        or len(at) > MAX_AT_CHARS
        or not (_ROOTS_ID if node is None else _ELEMENT_ID).fullmatch(at)
        or at.partition("-")[0] not in _ID_PREFIXES
    ):
        return None
    opened = list(dict.fromkeys(request.query_params.getlist(OPEN_PARAM)))[:MAX_OPEN]
    return _Query(opened, children_of, number, node, at, siblings_of, before)


@dataclass
class _Builder:
    """Builds a tree page's, or a fragment's, nodes, reading each page of children once.

    It lays the nodes out first, and then gives them their links, which name the nodes shown open.

    Attributes:
        request: The request.
        connection: The derived database.
        hierarchy: The hierarchy.
        page_path: The tree page's path, from the app's root.
        query: What the URL asks for.
        open_ids: The nodes the URL opens, by id.
        window: The node whose children the page shows from the query's offset, by id; None for the roots. For a
            fragment, its node.
        open_chars: About how many characters naming the open nodes takes in a link.
        shown: How many nodes have been shown so far.
        read: How many children, or roots, the reads so far have sorted (`MAX_CHILDREN_READ`).
        opened: The open nodes the links name, by id and IRI: the nodes shown open, in tree order, after a fragment's
            node and the nodes open above it.
        column: Whether the nodes are the hierarchy column's (`is_column_id`), whose links name no open nodes.
        current: In the hierarchy column, the node of the page's entity, shown open and marked.
        column_above: In the hierarchy column, the IRIs of the nodes above `window`, from a root down along first
            parents, which its links without JS to pages of children open; read when first needed if None.
    """

    request: Request
    connection: sqlite3.Connection
    hierarchy: EntityType
    page_path: str
    query: _Query
    open_ids: set[int]
    window: int | None
    open_chars: int
    shown: int = 0
    read: int = 0
    opened: list[tuple[int, str]] = field(default_factory=list)
    column: bool = False
    current: int | None = None
    column_above: list[str] | None = None
    _open_at: dict[int, list[str]] = field(default_factory=dict)  # each node shown open: its element ids
    _closing: dict[int, list[str]] = field(default_factory=dict)  # each node shown open: the links' IRIs once closed
    _pages: dict[tuple[int | None, int], ChildPage] = field(default_factory=dict)

    def build(self, parent: TreeNode | None, parent_id: str, offset: int, above: list[tuple[int, str]]) -> ShownList:
        """Return a page of a node's children, or of the roots, as shown, with the open nodes among them opened.

        Args:
            parent: The node; None for the roots.
            parent_id: Its element id, from which its children's follow.
            offset: How many children come before the page.
            above: For a fragment, the open nodes above its node, and the node, by id and IRI; else empty.
        """
        self.opened = list(above)
        shown = self._lay_out(parent, parent_id, offset)
        return self._linked(shown, parent, parent_id, [iri for _, iri in above])

    def children(self, parent: TreeNode | None, offset: int) -> ChildPage:
        """Return a page of a node's children, or of the roots, read once per request."""
        key = (None if parent is None else parent.id, offset)
        if key not in self._pages:
            prefix: str = self.request.app.state.settings.reserved_prefix
            root = link_root(self.request.scope)
            self._pages[key] = read_children(self.connection, self.hierarchy, parent, prefix, root, offset)
            self.read += self._pages[key].total
        return self._pages[key]

    def _lay_out(self, parent: TreeNode | None, parent_id: str, offset: int) -> ShownList:
        """Return a page of a node's children, or of the roots, with the open nodes among them opened, without links."""
        page = self.children(parent, offset)
        self.shown += len(page.nodes)
        nodes = []
        for index, node in enumerate(page.nodes, start=page.offset):
            element_id = f"{parent_id}-{index}"
            children = None
            if node.children and node.id in self.open_ids and self._has_room():
                if node.id not in self._open_at:
                    self.opened.append((node.id, node.link.iri))
                self._open_at.setdefault(node.id, []).append(element_id)
                start = self.query.offset if node.id == self.window else 0
                children = self._lay_out(node, element_id, start)
            nodes.append(ShownNode(node, element_id, None, None, children))
        return ShownList(page, nodes)

    def _has_room(self) -> bool:
        """Return whether the page has room for another open node's children.

        See `MAX_SHOWN`, `MAX_LINK_CHARS` and `MAX_CHILDREN_READ`.
        """
        return (
            self.shown < MAX_SHOWN and self.shown * self.open_chars < MAX_LINK_CHARS and self.read < MAX_CHILDREN_READ
        )

    def _linked(self, shown: ShownList, parent: TreeNode | None, parent_id: str, path: list[str]) -> ShownList:
        """Return a page of nodes laid out, with their links, and those to the pages before and after it.

        Args:
            shown: The page.
            parent: Its node; None for the roots.
            parent_id: Its node's element id.
            path: The IRIs of the open nodes above the page's nodes, its own node last.
        """
        nodes: list[ShownNode | Elided] = []
        for item in shown.nodes:
            assert isinstance(item, ShownNode)  # only the hierarchy column's merged tree elides levels
            node = item.node
            current = node.id == self.current
            if not node.children:
                nodes.append(replace(item, current=True) if current else item)
                continue
            if self.column:
                children = None
                if item.children is not None:
                    children = self._linked(item.children, node, item.id, [*path, node.link.iri])
                nodes.append(column_node(self.request, self.page_path, node, item.id, children, current))
                continue
            children = None
            if item.children is not None:
                toggle = self._toggle_link(self._closed(node.id), item.id)
                children = self._linked(item.children, node, item.id, [*path, node.link.iri])
            elif node.id not in self.open_ids and len(self.opened) < MAX_OPEN:
                toggle = self._toggle_link([*(iri for _, iri in self.opened), node.link.iri], item.id)
            else:  # it, or the page, has no room: only it and the nodes above it open, so it fits
                toggle = self._toggle_link([*path, node.link.iri], item.id)
            load = fragment_link(self.request, self.page_path, node, item.id, path)
            nodes.append(ShownNode(node, item.id, toggle, load, children))
        page = shown.page
        if self.column:
            return self._column_paged(ShownList(page, nodes), parent, parent_id, path)
        parent_iri = None if parent is None else parent.link.iri
        opened = [iri for _, iri in self.opened]
        previous = more = more_load = None
        if page.offset and self.query.at is None:  # a fragment's page goes after the page before it
            anchor = None if parent is None else parent_id
            start = page.offset - page.previous_count
            previous = page_link(self.request, self.page_path, opened, anchor, parent_iri, start)
        if page.next_count:
            more = page_link(self.request, self.page_path, opened, f"{parent_id}-{page.end}", parent_iri, page.end)
            more_load = fragment_link(self.request, self.page_path, parent, parent_id, path[:-1], page.end)
        return ShownList(page, nodes, previous, more, more_load)

    def _column_paged(self, shown: ShownList, parent: TreeNode | None, parent_id: str, path: list[str]) -> ShownList:
        """Return a page of the hierarchy column's nodes with the links to the pages before and after it.

        With htmx they add the page in place, after it or before it; without, they load the tree page showing it, the
        nodes above it open.

        Args:
            shown: The page, its nodes linked.
            parent: Its node; None for the roots.
            parent_id: Its node's element id.
            path: The IRIs of the nodes above the page's nodes in the fragment, its own node last.
        """
        page = shown.page
        previous = previous_load = more = more_load = None
        # A fragment's page goes after the page before it, but for one that shows the entity's siblings or goes before.
        if page.offset and (self.current is not None or self.query.before):
            previous = self._column_page(parent, path, page.offset - page.previous_count)
            previous_load = fragment_link(self.request, self.page_path, parent, parent_id, [], before=page.offset)
        if page.next_count and not self.query.before:  # one that goes before a page has it after
            more = self._column_page(parent, path, page.end)
            more_load = fragment_link(self.request, self.page_path, parent, parent_id, [], page.end)
        return replace(shown, previous=previous, more=more, more_load=more_load, previous_load=previous_load)

    def _column_page(self, parent: TreeNode | None, path: list[str], offset: int) -> str:
        """Return a link to the tree page showing a page of a node's children, or of the roots, the nodes above open."""
        if parent is None:
            return page_link(self.request, self.page_path, [], None, None, offset)
        if self.column_above is None:
            prefix: str = self.request.app.state.settings.reserved_prefix
            root = link_root(self.request.scope)
            above = (
                [] if self.window is None else first_path(self.connection, self.hierarchy, self.window, prefix, root)
            )
            self.column_above = above[:-1]
        opened = [*self.column_above, *path][:MAX_OPEN]
        return page_link(self.request, self.page_path, opened, None, parent.link.iri, offset)

    def _closed(self, node: int) -> list[str]:
        """Return the IRIs of the open nodes once a node shown open closes: less it, and those shown open only in it."""
        if node not in self._closing:
            inside = tuple(f"{at}-" for at in self._open_at[node])
            self._closing[node] = [
                iri
                for other, iri in self.opened
                if other != node and not all(at.startswith(inside) for at in self._open_at.get(other, [""]))
            ]
        return self._closing[node]

    def _toggle_link(self, opened: list[str], anchor: str) -> str:
        """Return a toggle's link: to the tree page with these nodes open, showing the page's own window, if any."""
        if self.query.at is not None:  # a fragment's links have none
            return page_link(self.request, self.page_path, opened, anchor, None, 0)
        return page_link(self.request, self.page_path, opened, anchor, self.query.children_of, self.query.offset)


def page_link(
    request: Request, page_path: str, opened: list[str], anchor: str | None, children_of: str | None, offset: int
) -> str:
    """Return a link to the tree page with these nodes open, at an element, showing a page of a node's children.

    Args:
        request: The request.
        page_path: The tree page's path, from the app's root.
        opened: The open nodes' IRIs.
        anchor: The element id to scroll to; None for the top.
        children_of: The IRI of the node whose children it shows from `offset`; None for the roots.
        offset: How many of its children, or of the roots, come before those shown.
    """
    params = [(OPEN_PARAM, iri) for iri in opened]
    if offset and children_of is not None:
        params.append((CHILDREN_OF_PARAM, children_of))
    if offset:
        params.append((OFFSET_PARAM, str(offset)))
    return href(request, with_query(page_path, params), anchor)


def fragment_link(
    request: Request,
    page_path: str,
    node: TreeNode | None,
    element_id: str,
    above: list[str],
    offset: int = 0,
    *,
    before: int = 0,
    siblings_of: str | None = None,
) -> str:
    """Return the URL of a node's children's fragment, or the roots', from an offset, given the open nodes above.

    Args:
        request: The request.
        page_path: The tree page's path, from the app's root.
        node: The node; None for the roots.
        element_id: Its element id, or for the roots, their ids' prefix.
        above: The open nodes above it, by IRI.
        offset: How many of its children, or of the roots, come before those shown.
        before: In the hierarchy column, the offset of the page after the one shown, instead (`BEFORE_PARAM`).
        siblings_of: In the hierarchy column, the IRI of the node whose page of siblings it shows (`SIBLINGS_PARAM`).
    """
    params = [*([(NODE_PARAM, node.link.iri)] if node is not None else []), (AT_PARAM, element_id)]
    params += [(OPEN_PARAM, iri) for iri in above]
    if offset:
        params.append((OFFSET_PARAM, str(offset)))
    if before:
        params.append((BEFORE_PARAM, str(before)))
    if siblings_of is not None:
        params.append((SIBLINGS_PARAM, siblings_of))
    return href(request, with_query(page_path, params))


def is_column_id(element_id: str) -> bool:
    """Return whether an element id is the hierarchy column's (`COLUMN_IDS`), not a tree page's."""
    return element_id.partition("-")[0] in COLUMN_IDS.values()


def first_path(connection: sqlite3.Connection, hierarchy: EntityType, node: int, prefix: str, root: str) -> list[str]:
    """Return the IRIs of the nodes from a root down to a node, along first parents by display name, the node last.

    The hierarchy column draws a node under its first parent (`browl.hierarchy_column`).
    """
    parents = read_ancestors(connection, hierarchy, node)
    nodes = read_nodes(connection, hierarchy, parents, prefix, root)
    rank = {each.id: index for index, each in enumerate(nodes)}
    iris = {each.id: each.link.iri for each in nodes}
    path = [node]
    while parents[path[-1]]:
        path.append(min(parents[path[-1]], key=rank.__getitem__))
    return [iris[each] for each in reversed(path)]


def column_node(
    request: Request,
    page_path: str,
    node: TreeNode,
    element_id: str,
    children: ShownList | None = None,
    current: bool = False,
) -> ShownNode:
    """Return a node with children as the hierarchy column shows it.

    Its links name no open nodes, which a deep node's would repeat for each of its children: NCBITaxon's are about 40
    deep. Without JS, its toggle loads its page, whose column shows its children; its fragment's URL gives no open nodes
    above it, as its children's links name none either.

    Args:
        request: The request.
        page_path: Its tree page's path, from the app's root.
        node: The node.
        element_id: Its element id.
        children: Its children shown, if it's open.
        current: Whether it's the node of the page's entity.
    """
    load = fragment_link(request, page_path, node, element_id, [])
    return ShownNode(node, element_id, node.link.href or href(request, page_path), load, children, current)


def shown_children(
    request: Request,
    connection: sqlite3.Connection,
    tree: TreePage,
    parent: TreeNode,
    parent_id: str,
    above: list[tuple[int, str]],
) -> ShownList:
    """Return the first page of a node's children, closed, as the hierarchy column shows them.

    Args:
        request: The request.
        connection: The derived database.
        tree: The node's tree page.
        parent: The node.
        parent_id: Its element id, from which its children's follow.
        above: The nodes above it, along its first parents, and the node, by id and IRI. Without JS, the link after
            the page loads the tree page with them open, showing the next page.
    """
    page_path = tree_path(request.app.state.settings.reserved_prefix, tree)
    query = _Query([], node=parent.link.iri, at=parent_id)
    column_above = [iri for _, iri in above[:-1]]
    builder = _Builder(
        request,
        connection,
        tree.hierarchy,
        page_path,
        query,
        set(),
        parent.id,
        0,
        column=True,
        column_above=column_above,
    )
    return builder.build(parent, parent_id, 0, [(parent.id, parent.link.iri)])


def _link_chars(iris: Iterable[str]) -> int:
    """Return about how many characters naming these open nodes takes in a link."""
    return sum(len(OPEN_PARAM) + 2 + len(urllib.parse.quote(iri, safe="")) for iri in iris)


@router.api_route("/tree/{slug}", methods=["GET", "HEAD"], include_in_schema=False)
async def tree_page(request: Request, slug: str) -> Response:
    """Serve a hierarchy's tree page, or with `at=`, a page of a node's children, or of the roots, as a fragment."""
    tree = TREE_PAGES.get(slug)
    query = _read_query(request)
    if tree is None or query is None:
        return not_found(request)
    request.state.current_tree = slug  # marked in the navigation bar
    hierarchy = tree.hierarchy
    prefix: str = request.app.state.settings.reserved_prefix
    page_path = tree_path(prefix, tree)

    def build(connection: sqlite3.Connection) -> ShownList | None:
        target = query.children_of if query.at is None else query.node
        wanted = [
            *query.open,
            *([] if target is None else [target]),
            *([] if query.siblings_of is None else [query.siblings_of]),
        ]
        ids = node_ids(connection, hierarchy, wanted)
        if target is not None and target not in ids:
            return None
        window = None if target is None else ids[target]
        offset, current = query.offset or max(query.before - CHILD_PAGE, 0), None
        if query.siblings_of is not None:
            current = ids.get(query.siblings_of)
            index = None if current is None else child_index(connection, hierarchy, window, current)
            if current is None or index is None:
                return None
            offset = page_offset(index)
        known: dict[int, str] = {}  # the open nodes the URL names, each once, an IRI of each
        for iri in query.open:
            if iri in ids:
                known.setdefault(ids[iri], iri)
        chars = _link_chars([*known.values(), *([] if query.node is None else [query.node])])
        column = query.at is not None and is_column_id(query.at)
        opens = set(known) | ({current} if current is not None else set())
        builder = _Builder(
            request, connection, hierarchy, page_path, query, opens, window, chars, column=column, current=current
        )
        parent = None if window is None else read_node(connection, hierarchy, window, prefix, link_root(request.scope))
        if query.offset or query.before:  # checked before any page is read
            total = count_roots(connection, hierarchy) if parent is None else parent.children
            if query.offset >= total or query.before > total:
                return None
        if query.at is not None:
            iris = node_iris(connection, (node for node in known if parent is None or node != parent.id))
            above = [*iris.items(), *([] if parent is None else [(parent.id, parent.link.iri)])]
            shown = builder.build(parent, query.at, offset, above)
        else:
            # A window on a node that isn't shown open is ignored, e.g. once a link has closed it or its parent.
            shown = builder.build(None, ROOT_ID, 0 if window is not None else query.offset, [])
        return shown

    shown = await read_derived(request, build)
    if shown is None:
        return not_found(request)
    # Rendered in the thread pool, as a page of `MAX_SHOWN` nodes takes about 100 ms.
    if query.at is not None:
        return await asyncio.to_thread(
            templates.TemplateResponse, request, "tree_fragment.html", {"list": shown}, headers=PANEL_HEADERS
        )
    headers = PANEL_HEADERS if request.url.query else None  # the tree as it opens is at the plain URL
    context = {"name": tree.label, "tree": shown}
    return await asyncio.to_thread(templates.TemplateResponse, request, "tree.html", context, headers=headers)
