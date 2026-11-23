"""The tree pages (U-7, U-17, DESIGN.md §2.9, §2.13): `tree/{hierarchy}`, and its nodes' children as htmx fragments.

A tree page shows the hierarchy's roots, and the children of each node its URL opens. Without JS, a node's toggle is a
link to the page with that node opened or closed too: `open=` names each open node by IRI, so a URL keeps its tree
through reloads and new data. A node opens wherever it appears, under each of its parents; the most nodes a page shows
is `MAX_SHOWN`. With htmx, the toggle fetches the node's children alone (`node=`), and `static/tree.js` opens and
closes nodes in place, and gives the tree the WAI-ARIA tree pattern's keyboard use.

Children come a page at a time (`browl.hierarchy.CHILD_PAGE`). With htmx, "Show more" adds the next page in place;
without, it shows that page of the node's children (`children-of=` and `offset=`), with a link back.
"""

import re
import sqlite3
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, field

from fastapi import APIRouter, Request
from fastapi.responses import Response

from browl.derived import EntityType
from browl.hierarchy import ChildPage, TreeNode, node_ids, node_iris, read_children, read_node
from browl.lookup import PANEL_HEADERS, not_found
from browl.pages import TREES, href, link_root, read_derived, templates
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

MAX_OPEN = 100
"""The most open nodes a URL names that a page takes; the rest are ignored. A node's toggle that would pass it opens
only the node and those above it."""

MAX_SHOWN = 5_000
"""About the most nodes a page or fragment shows: once it shows this many, the open nodes left are shown closed. A
node with several parents opens under each, so a few open nodes can multiply in a hierarchy with many joins. A node
shown closed that the URL opens gets a toggle that opens only it and the nodes above it, so it fits."""

MAX_LINK_CHARS = 2_000_000
"""About the most characters naming open nodes a page's, or fragment's, links hold in all: each toggle names them
all, so with many open nodes, or long IRIs, a page shows fewer than `MAX_SHOWN` nodes, as it does past that."""

ROOT_ID = "t"
"""The element id the roots' ids follow from: `t-0`, `t-1`, …; a node's children's from its own, `t-0-0`, …."""

MAX_AT_CHARS = 256
"""The longest element id `at=…` may give, as each child's ids and links repeat it; a deeper node's fragment is a 404,
so its toggle loads the page instead."""

_OFFSET = re.compile(r"[1-9][0-9]{0,8}")
"""An offset `offset=…` may give: a positive integer without a sign or leading zeros, so each page has one URL."""

_ELEMENT_ID = re.compile(r"[a-z]+(?:-[0-9]{1,9})+")
"""An element id `at=…` may give, as `ShownNode.id` makes them."""

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
    """

    node: TreeNode
    id: str
    toggle: str | None
    load: str | None
    children: "ShownList | None" = None


@dataclass(frozen=True)
class ShownList:
    """A page of a node's children, or of the roots, as a tree shows it.

    Attributes:
        page: The page.
        nodes: Its nodes.
        previous: The link to the page before it, without JS; None for the first, and in a fragment, which goes
            after the page before it.
        more: The link to the page after it, without JS; None for the last.
        more_load: The URL of the next page's fragment, for htmx, which adds it in place; None for the last, or if
            the next page is the roots', which `more` shows.
    """

    page: ChildPage
    nodes: list[ShownNode]
    previous: str | None = None
    more: str | None = None
    more_load: str | None = None


@dataclass(frozen=True)
class _Query:
    """What a tree page's URL asks for.

    Attributes:
        open: The open nodes' IRIs, in order, each once, at most `MAX_OPEN`.
        children_of: The IRI of the node whose children are shown from `offset`; None for the roots.
        offset: How many of its children, or of the roots, come before those shown.
        node: The IRI of the node whose children alone are asked for, as a fragment; None for the page.
        at: That node's element id.
    """

    open: list[str]
    children_of: str | None = None
    offset: int = 0
    node: str | None = None
    at: str = ROOT_ID


def _read_query(request: Request) -> _Query | None:
    """Return what a tree page's URL asks for; None if it repeats a parameter, or gives one that isn't valid."""
    params = request.query_params
    single: dict[str, str | None] = {}
    for name in (CHILDREN_OF_PARAM, OFFSET_PARAM, NODE_PARAM, AT_PARAM):
        values = params.getlist(name)
        if len(values) > 1:
            return None
        single[name] = values[0] if values else None
    offset, node, at = single[OFFSET_PARAM], single[NODE_PARAM], single[AT_PARAM]
    if offset is not None and not _OFFSET.fullmatch(offset):
        return None
    if node is None:
        if at is not None or (single[CHILDREN_OF_PARAM] is not None and offset is None):
            return None
    elif single[CHILDREN_OF_PARAM] is not None or at is None or len(at) > MAX_AT_CHARS or not _ELEMENT_ID.fullmatch(at):
        return None
    opened = list(dict.fromkeys(params.getlist(OPEN_PARAM)))[:MAX_OPEN]
    return _Query(opened, single[CHILDREN_OF_PARAM], int(offset or 0), node, at or ROOT_ID)


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
        opened: The open nodes the links name, by id and IRI: the nodes shown open, in tree order, after a fragment's
            node and the nodes open above it.
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
    opened: list[tuple[int, str]] = field(default_factory=list)
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
        """Return whether the page has room for another open node's children (`MAX_SHOWN`, `MAX_LINK_CHARS`)."""
        return self.shown < MAX_SHOWN and self.shown * self.open_chars < MAX_LINK_CHARS

    def _linked(self, shown: ShownList, parent: TreeNode | None, parent_id: str, path: list[str]) -> ShownList:
        """Return a page of nodes laid out, with their links, and those to the pages before and after it.

        Args:
            shown: The page.
            parent: Its node; None for the roots.
            parent_id: Its node's element id.
            path: The IRIs of the open nodes above the page's nodes, its own node last.
        """
        nodes = []
        for item in shown.nodes:
            node = item.node
            if not node.children:
                nodes.append(item)
                continue
            children = None
            if item.children is not None:
                toggle = self._toggle_link(self._closed(node.id), item.id)
                children = self._linked(item.children, node, item.id, [*path, node.link.iri])
            elif node.id not in self.open_ids and len(self.opened) < MAX_OPEN:
                toggle = self._toggle_link([*(iri for _, iri in self.opened), node.link.iri], item.id)
            else:  # it, or the page, has no room: only it and the nodes above it open, so it fits
                toggle = self._toggle_link([*path, node.link.iri], item.id)
            load = self.fragment_link(node, item.id, path)
            nodes.append(ShownNode(node, item.id, toggle, load, children))
        page = shown.page
        parent_iri = None if parent is None else parent.link.iri
        opened = [iri for _, iri in self.opened]
        previous = more = more_load = None
        if page.offset and self.query.node is None:  # a fragment's page goes after those before it
            anchor = None if parent is None else parent_id
            previous = self.page_link(opened, anchor, parent_iri, page.offset - page.previous_count)
        if page.next_count:
            more = self.page_link(opened, f"{parent_id}-{page.end}", parent_iri, page.end)
            more_load = None if parent is None else self.fragment_link(parent, parent_id, path[:-1], page.end)
        return ShownList(page, nodes, previous, more, more_load)

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
        if self.query.node is not None:  # a fragment's links have none
            return self.page_link(opened, anchor, None, 0)
        return self.page_link(opened, anchor, self.query.children_of, self.query.offset)

    def page_link(self, opened: list[str], anchor: str | None, children_of: str | None, offset: int) -> str:
        """Return a link to the tree page with these nodes open, at an element, showing a page of a node's children.

        Args:
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
        return href(self.request, with_query(self.page_path, params), anchor)

    def fragment_link(self, node: TreeNode, element_id: str, above: list[str], offset: int = 0) -> str:
        """Return the URL of a node's children's fragment, from an offset, given the open nodes above it."""
        params = [(NODE_PARAM, node.link.iri), (AT_PARAM, element_id), *[(OPEN_PARAM, iri) for iri in above]]
        if offset:
            params.append((OFFSET_PARAM, str(offset)))
        return href(self.request, with_query(self.page_path, params))


def _link_chars(iris: Iterable[str]) -> int:
    """Return about how many characters naming these open nodes takes in a link."""
    return sum(len(OPEN_PARAM) + 2 + len(urllib.parse.quote(iri, safe="")) for iri in iris)


@router.api_route("/tree/{slug}", methods=["GET", "HEAD"], include_in_schema=False)
async def tree_page(request: Request, slug: str) -> Response:
    """Serve a hierarchy's tree page, or with `node=`, a node's children alone, as an htmx fragment.

    The page shows the roots, and the children of each node `open=` names, a page of each; `children-of=` and
    `offset=` show a later page of a node's children, or with `offset=` alone, of the roots. A fragment shows a page
    of its node's children, from `offset=`, with their element ids following from `at=`; `open=` names the open nodes
    above it, for its links, and any open below it. An unknown node, or an offset past the last, is a 404.
    """
    tree = TREE_PAGES.get(slug)
    query = _read_query(request)
    if tree is None or query is None:
        return not_found(request)
    hierarchy = tree.hierarchy
    prefix: str = request.app.state.settings.reserved_prefix
    page_path = f"/{prefix}/tree/{slug}"

    def build(connection: sqlite3.Connection) -> ShownList | None:
        named = [*query.open, *(iri for iri in (query.children_of, query.node) if iri is not None)]
        ids = node_ids(connection, hierarchy, named)
        target = query.node if query.node is not None else query.children_of
        if target is not None and target not in ids:
            return None
        window = None if target is None else ids[target]
        known: dict[int, str] = {}  # the open nodes the URL names, each once, an IRI of each
        for iri in query.open:
            if iri in ids:
                known.setdefault(ids[iri], iri)
        chars = _link_chars([*known.values(), *([] if query.node is None else [query.node])])
        builder = _Builder(request, connection, hierarchy, page_path, query, set(known), window, chars)
        parent = None if window is None else read_node(connection, hierarchy, window, prefix, link_root(request.scope))
        if query.node is not None:
            assert parent is not None  # found by `node_ids`
            iris = node_iris(connection, (node for node in known if node != parent.id))
            above = [*iris.items(), (parent.id, parent.link.iri)]
            shown = builder.build(parent, query.at, query.offset, above)
        else:
            # A window on a node that isn't shown open is ignored, e.g. once a link has closed it or its parent.
            shown = builder.build(None, ROOT_ID, 0 if window is not None else query.offset, [])
        total = shown.page.total if parent is None else parent.children
        return None if query.offset and query.offset >= total else shown

    shown = await read_derived(request, build)
    if shown is None:
        return not_found(request)
    if query.node is not None:
        return templates.TemplateResponse(request, "tree_fragment.html", {"list": shown}, headers=PANEL_HEADERS)
    headers = PANEL_HEADERS if request.url.query else None  # the tree as it opens is at the plain URL
    return templates.TemplateResponse(request, "tree.html", {"name": tree.label, "tree": shown}, headers=headers)
