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
from dataclasses import dataclass, field

from fastapi import APIRouter, Request
from fastapi.responses import Response

from browl.derived import EntityType
from browl.hierarchy import CHILD_PAGE, ChildPage, TreeNode, node_ids, read_children, read_node
from browl.lookup import PANEL_HEADERS, not_found
from browl.pages import TREES, href, link_root, read_derived, templates
from browl.tabs import with_query

HIERARCHIES = {
    "classes": EntityType.CLASS,
    "object-properties": EntityType.OBJECT_PROPERTY,
    "data-properties": EntityType.DATA_PROPERTY,
    "annotation-properties": EntityType.ANNOTATION_PROPERTY,
}
"""Each tree page's hierarchy, by its path segment under `tree/` (`browl.pages.TREES`)."""

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
"""The most open nodes a URL names that a page takes; the rest are ignored."""

MAX_SHOWN = 5_000
"""About the most nodes a page or fragment shows: once it shows this many, the open nodes left are shown closed. A
node with several parents opens under each, so a few open nodes can multiply in a hierarchy with many joins."""

ROOT_ID = "t"
"""The element id the roots' ids follow from: `t-0`, `t-1`, …; a node's children's from its own, `t-0-0`, …."""

_OFFSET = re.compile(r"[1-9][0-9]{0,8}")
"""An offset `offset=…` may give: a positive integer without a sign or leading zeros, so each page has one URL."""

_ELEMENT_ID = re.compile(r"[a-z]+(?:-[0-9]{1,9}){1,500}")
"""An element id `at=…` may give, as `ShownNode.id` makes them."""

router = APIRouter()
"""The tree routes, under the reserved prefix."""

templates.env.globals["CHILD_PAGE"] = CHILD_PAGE


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
        more_count: How many nodes the next page shows.
    """

    page: ChildPage
    nodes: list[ShownNode]
    previous: str | None = None
    more: str | None = None
    more_load: str | None = None
    more_count: int = 0


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
        if at is not None:
            return None
    elif single[CHILDREN_OF_PARAM] is not None or at is None or not _ELEMENT_ID.fullmatch(at):
        return None
    opened = list(dict.fromkeys(params.getlist(OPEN_PARAM)))[:MAX_OPEN]
    return _Query(opened, single[CHILDREN_OF_PARAM], int(offset or 0), node, at or ROOT_ID)


@dataclass
class _Builder:
    """Builds a tree page's, or a fragment's, nodes, reading each page of children once.

    Attributes:
        request: The request.
        connection: The derived database.
        hierarchy: The hierarchy.
        page_path: The tree page's path, from the app's root.
        query: What the URL asks for.
        ids: The node of each IRI the URL names, by IRI (`browl.hierarchy.node_ids`).
        window: The node whose children the page shows from the query's offset, by id; None for the roots. For a
            fragment, its node.
        shown: How many nodes have been shown so far.
    """

    request: Request
    connection: sqlite3.Connection
    hierarchy: EntityType
    page_path: str
    query: _Query
    ids: dict[str, int]
    window: int | None
    shown: int = 0
    _pages: dict[tuple[int | None, int], ChildPage] = field(default_factory=dict)

    @property
    def open_ids(self) -> set[int]:
        """Return the open nodes, by id."""
        return {self.ids[iri] for iri in self.query.open if iri in self.ids}

    def page_link(self, opened: list[str], anchor: str | None, window: tuple[str, int] | None = None) -> str:
        """Return a link to the tree page with these nodes open, at an element, showing a page of a node's children.

        Args:
            opened: The open nodes' IRIs.
            anchor: The element id to scroll to; None for the top.
            window: The IRI of the node whose children it shows from an offset, and the offset; the node's IRI is
                empty for the roots. None for the page's own window: a fragment's links have none.
        """
        if window is None and self.query.node is None and self.query.offset:
            window = (self.query.children_of or "", self.query.offset)
        params = [(OPEN_PARAM, iri) for iri in opened]
        if window is not None and window[1]:
            params += [*([(CHILDREN_OF_PARAM, window[0])] if window[0] else []), (OFFSET_PARAM, str(window[1]))]
        return href(self.request, with_query(self.page_path, params), anchor)

    def fragment_link(self, node: TreeNode, element_id: str, opened: list[str], offset: int = 0) -> str:
        """Return the URL of a node's children's fragment, with these nodes open, from an offset."""
        params = [(NODE_PARAM, node.link.iri), (AT_PARAM, element_id), *[(OPEN_PARAM, iri) for iri in opened]]
        if offset:
            params.append((OFFSET_PARAM, str(offset)))
        return href(self.request, with_query(self.page_path, params))

    def children(self, parent: TreeNode | None, offset: int) -> ChildPage:
        """Return a page of a node's children, or of the roots, read once per request."""
        key = (None if parent is None else parent.id, offset)
        if key not in self._pages:
            prefix: str = self.request.app.state.settings.reserved_prefix
            root = link_root(self.request.scope)
            self._pages[key] = read_children(self.connection, self.hierarchy, parent, prefix, root, offset)
        return self._pages[key]

    def shown_list(self, parent: TreeNode | None, parent_id: str, opened: list[str], offset: int) -> ShownList:
        """Return a page of a node's children, or of the roots, as shown, with the open nodes among them opened.

        Args:
            parent: The node; None for the roots.
            parent_id: Its element id, from which its children's follow.
            opened: The open nodes' IRIs, for links; the node among them.
            offset: How many children come before the page.
        """
        page = self.children(parent, offset)
        self.shown += len(page.nodes)
        open_ids = self.open_ids
        nodes = []
        for index, node in enumerate(page.nodes, start=page.offset):
            element_id = f"{parent_id}-{index}"
            if not node.children:
                nodes.append(ShownNode(node, element_id, None, None))
                continue
            is_open = node.id in open_ids
            others = [iri for iri in opened if self.ids.get(iri) != node.id]
            toggle = self.page_link(others if is_open else [*opened, node.link.iri], element_id)
            load = self.fragment_link(node, element_id, [*others, node.link.iri])
            children = None
            if is_open and self.shown < MAX_SHOWN:
                start = self.query.offset if node.id == self.window else 0
                children = self.shown_list(node, element_id, opened, start)
            nodes.append(ShownNode(node, element_id, toggle, load, children))
        return self._with_paging(ShownList(page, nodes), parent, parent_id, opened)

    def _with_paging(self, shown: ShownList, parent: TreeNode | None, parent_id: str, opened: list[str]) -> ShownList:
        """Return a list with its links to the pages before and after it."""
        page = shown.page
        iri = "" if parent is None else parent.link.iri
        previous = None
        if page.offset and self.query.node is None:  # a fragment's page goes after those before it
            anchor = None if parent is None else parent_id
            previous = self.page_link(opened, anchor, (iri, max(page.offset - CHILD_PAGE, 0)))
        if page.end >= page.total:
            return ShownList(page, shown.nodes, previous)
        more = self.page_link(opened, f"{parent_id}-{page.end}", (iri, page.end))
        more_load = None if parent is None else self.fragment_link(parent, parent_id, opened, page.end)
        count = min(CHILD_PAGE, page.total - page.end)
        return ShownList(page, shown.nodes, previous, more, more_load, count)


@router.api_route("/tree/{slug}", methods=["GET", "HEAD"], include_in_schema=False)
async def tree_page(request: Request, slug: str) -> Response:
    """Serve a hierarchy's tree page, or with `node=`, a node's children alone, as an htmx fragment.

    The page shows the roots, and the children of each node `open=` names, a page of each; `children-of=` and
    `offset=` show a later page of a node's children, or with `offset=` alone, of the roots. A fragment shows a page
    of its node's children, from `offset=`, with their element ids following from `at=`. An unknown node, or an offset
    past the last, is a 404.
    """
    hierarchy = HIERARCHIES.get(slug)
    query = _read_query(request)
    if hierarchy is None or query is None:
        return not_found(request)
    prefix: str = request.app.state.settings.reserved_prefix
    page_path = f"/{prefix}/tree/{slug}"

    def build(connection: sqlite3.Connection) -> ShownList | None:
        named = [*query.open, *(iri for iri in (query.children_of, query.node) if iri is not None)]
        ids = node_ids(connection, hierarchy, named)
        target = query.node if query.node is not None else query.children_of
        if target is not None and target not in ids:
            return None
        window = None if target is None else ids[target]
        builder = _Builder(request, connection, hierarchy, page_path, query, ids, window)
        parent = None if window is None else read_node(connection, hierarchy, window, prefix, link_root(request.scope))
        if query.node is not None:
            assert parent is not None  # found by `node_ids`
            ids.setdefault(parent.link.iri, parent.id)  # `node` may name another member of its group
            opened = [*query.open, *([] if parent.link.iri in query.open else [parent.link.iri])]
            shown = builder.shown_list(parent, query.at, opened, query.offset)
        else:
            # A window on a node that isn't shown open is ignored, e.g. once a link has closed it or its parent.
            shown = builder.shown_list(None, ROOT_ID, query.open, 0 if window is not None else query.offset)
        total = shown.page.total if parent is None else parent.children
        return None if query.offset and query.offset >= total else shown

    shown = await read_derived(request, build)
    if shown is None:
        return not_found(request)
    if query.node is not None:
        return templates.TemplateResponse(request, "tree_fragment.html", {"list": shown}, headers=PANEL_HEADERS)
    return templates.TemplateResponse(request, "tree.html", {"name": dict(TREES)[slug], "tree": shown})
