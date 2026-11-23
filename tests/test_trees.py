"""Tests for the tree pages (P-18, U-7–U-9, U-17, DESIGN.md §2.13) and the hierarchy reads under them."""

import html.parser
import re
import urllib.parse
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import browl.trees
from browl.derived import EntityType
from browl.hierarchy import CHILD_PAGE, node_ids, read_children, read_node
from browl.jobs import Stage
from tests.helpers import connect, derived_stage, make_app, make_settings

pytestmark = pytest.mark.usefixtures("data_dir")

EX = "http://example.org/"
SUB_CLASS = "http://www.w3.org/2000/01/rdf-schema#subClassOf"
SUB_PROPERTY = "http://www.w3.org/2000/01/rdf-schema#subPropertyOf"
EQUIVALENT = "http://www.w3.org/2002/07/owl#equivalentClass"
THING = "http://www.w3.org/2002/07/owl#Thing"
KIDS = CHILD_PAGE * 5 // 2
"""How many children `Big` has: two and a half pages, 1,250."""
NOTES = CHILD_PAGE + 20
"""How many annotation properties there are, all roots: more than a page, 520."""

TREE = "/_browl/tree/classes"


def ex(name: str) -> str:
    """Return the IRI of a name in `EX`."""
    return f"{EX}{name}"


def kid(number: int) -> str:
    """Return the name of one of `Big`'s children; they sort by number."""
    return f"Kid {number:04}"


CLASSES = ["Top", "Left", "Right", "Bottom", "Deep", "Same", "Alias", "Alias2", "Alias3", "Sub", "Free", "Big"]
CLASS_EDGES = [
    ("Left", SUB_CLASS, "Top"),
    ("Right", SUB_CLASS, "Top"),
    ("Bottom", SUB_CLASS, "Left"),
    ("Bottom", SUB_CLASS, "Right"),  # a diamond: Bottom under each parent, with its subtree
    ("Deep", SUB_CLASS, "Bottom"),
    ("Same", SUB_CLASS, "Left"),
    ("Same", EQUIVALENT, "Alias"),  # Alias, Alias2, Alias3 and Same are one node, Alias its representative
    ("Alias2", EQUIVALENT, "Same"),
    ("Alias3", EQUIVALENT, "Same"),
    ("Sub", SUB_CLASS, "Same"),  # under the group, by a member other than its representative
    ("Big", SUB_CLASS, "Top"),
    ("Top", SUB_CLASS, "Thing"),
]
NAMES = {name: name for name in CLASSES} | {"Alias": "Alias", "Same": "same"}  # sorted case-insensitively


def tree_stage() -> object:
    """Return a stage that builds the classes, object properties `p` > `q`, no data properties, and `NOTES`."""
    entities = [(ex(name), EntityType.CLASS) for name in CLASSES]
    entities += [(ex(f"kid{n}"), EntityType.CLASS) for n in range(KIDS)]
    entities += [(THING, EntityType.CLASS)]
    entities += [(ex("p"), EntityType.OBJECT_PROPERTY), (ex("q"), EntityType.OBJECT_PROPERTY)]
    entities += [(ex(f"note{n}"), EntityType.ANNOTATION_PROPERTY) for n in range(NOTES)]
    edges = [(ex(c), r, THING if p == "Thing" else ex(p)) for c, r, p in CLASS_EDGES]
    edges += [(ex(f"kid{n}"), SUB_CLASS, ex("Big")) for n in range(KIDS)]
    edges += [(ex("q"), SUB_PROPERTY, ex("p"))]
    names: dict[str, tuple[str | None, str | None]] = {ex(n): (name, None) for n, name in NAMES.items()}
    names |= {ex(f"kid{n}"): (kid(n), None) for n in range(KIDS)}
    names |= {ex(f"note{n}"): (f"note {n:03}", None) for n in range(NOTES)}
    return derived_stage(entities, names=names, edges=edges)


@dataclass
class Item:
    """A tree item as a page shows it.

    Attributes:
        id: Its element id; None for a paging link.
        text: Its text, whitespace collapsed.
        href: Its link.
        depth: How deep it is: 0 for the roots.
        expanded: Its `aria-expanded`; None for a leaf.
        hidden: Whether it's in a closed group.
        toggle: Its toggle's link, if it has one.
        load: Its toggle's htmx URL, or a paging link's.
        counts: Its counts' text, if it has children.
    """

    id: str | None
    text: str = ""
    href: str = ""
    depth: int = 0
    expanded: str | None = None
    hidden: bool = False
    toggle: str | None = None
    load: str | None = None
    counts: str | None = None


class Tree(html.parser.HTMLParser):
    """A page's tree items, in document order (`Item`).

    Attributes:
        items: The items.
    """

    def __init__(self, page: str) -> None:
        """Parse a page, or a fragment."""
        super().__init__()
        self.items: list[Item] = []
        self._groups: list[bool] = []  # each open group: hidden?
        self._toggle: tuple[str, str | None] | None = None
        self._text: list[str] | None = None
        self._counts: Item | None = None
        self._counts_text: list[str] = []
        self.feed(page)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Start an item, a toggle, a group or counts."""
        at = dict(attrs)
        if tag == "ul" and at.get("role") == "group":
            self._groups.append("hidden" in at)
        elif tag == "a" and at.get("class") == "tree-toggle":
            self._toggle = (at["href"] or "", at.get("hx-get"))
        elif at.get("role") == "treeitem":
            toggle, self._toggle = self._toggle, None
            item = Item(
                at.get("id"),
                href=at.get("href") or "",
                depth=len(self._groups),
                expanded=at.get("aria-expanded"),
                hidden=any(self._groups),
                toggle=None if toggle is None else toggle[0],
                load=at.get("hx-get") if toggle is None else toggle[1],
            )
            self.items.append(item)
            self._text = []
        elif tag == "span" and at.get("class") == "tree-counts":
            self._counts = self.items[-1]
            self._counts_text = []

    def handle_endtag(self, tag: str) -> None:
        """End an item, a group or counts."""
        if tag == "ul" and self._groups:
            self._groups.pop()
        elif tag == "a" and self._text is not None:
            self.items[-1].text = " ".join("".join(self._text).split())
            self._text = None
        elif tag == "span" and self._counts is not None:
            self._counts.counts = " ".join("".join(self._counts_text).split())
            self._counts = None

    def handle_data(self, data: str) -> None:
        """Collect an item's text, or its counts'."""
        if self._text is not None:
            self._text.append(data)
        if self._counts is not None:
            self._counts_text.append(data)

    def shown(self) -> list[tuple[int, str]]:
        """Return the shown items' depths and texts."""
        return [(item.depth, item.text) for item in self.items if not item.hidden]

    def item(self, text: str) -> Item:
        """Return the first item with this text."""
        return next(item for item in self.items if item.text == text)


def query(url: str) -> list[tuple[str, str]]:
    """Return a URL's query parameters, in order."""
    return urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)


def opened(*names: str) -> list[tuple[str, str]]:
    """Return the query parameters that open these nodes, by name."""
    return [("open", ex(name)) for name in names]


@pytest.fixture
def app() -> FastAPI:
    """Return an app with default settings."""
    return make_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Yield a test client for the app, with the hierarchies loaded."""
    with TestClient(app) as test_client:
        app.state.jobs.register(Stage.PRE_CACHING, tree_stage())
        test_client.portal.call(app.state.jobs.start)
        test_client.portal.call(app.state.jobs.wait)
        yield test_client


def tree(client: TestClient, params: Sequence[tuple[str, str]] = (), path: str = TREE) -> Tree:
    """Return the tree of a tree page, checking that it's served."""
    response = client.get(path, params=list(params))
    assert response.status_code == 200, response.text
    return Tree(response.text)


def test_roots(client: TestClient) -> None:
    """Check that the page shows the roots, closed, by name, with their counts and links to their pages (U-7)."""
    response = client.get(TREE)
    assert "<title>Classes · browl</title>" in response.text
    found = Tree(response.text)
    assert found.shown() == [(0, "Free"), (0, "Top")]  # not owl:Thing, under which Top is
    free, top = found.items
    assert (free.id, free.href, free.expanded, free.toggle, free.counts) == ("t-0", "/Free", None, None, None)
    assert (top.id, top.href, top.expanded, top.counts) == ("t-1", "/Top", "false", "3 children · 1,257 descendants")
    assert query(top.toggle or "") == opened("Top")
    assert top.toggle is not None and top.toggle.endswith("#t-1")  # back where it was
    assert query(top.load or "") == [("node", ex("Top")), ("at", "t-1")]  # no open nodes above it


def test_crawlers_kept_out(client: TestClient) -> None:
    """Check that toggles and paging links are `nofollow`, and a tree page with a query is marked noindex (S3)."""
    plain = client.get(TREE)
    assert "X-Robots-Tag" not in plain.headers
    response = client.get(TREE, params=[*opened("Top", "Big"), ("children-of", ex("Big")), ("offset", "500")])
    assert response.headers["X-Robots-Tag"] == "noindex"
    links = re.findall(
        r'<a (?:class="tree-toggle"|role="treeitem") href="[^"]*/tree/classes[^"]*"[^>]*>', response.text
    )
    assert len(links) > 4  # the toggles, "previous" and "more"
    assert all('rel="nofollow"' in link for link in links), links


def test_open_nodes(client: TestClient) -> None:
    """Check that open nodes show their children, and a node with two parents shows under each, with its subtree."""
    found = tree(client, opened("Top", "Left", "Right"))
    assert found.shown() == [
        (0, "Free"),
        (0, "Top"),
        (1, "Big"),
        (1, "Left"),
        (2, "Alias ≡ Alias2 ≡ Alias3 ≡ … (+1)"),
        (2, "Bottom"),
        (1, "Right"),
        (2, "Bottom"),
    ]
    left = found.item("Left")
    assert (left.id, left.expanded, left.counts) == ("t-1-1", "true", "2 children · 4 descendants")
    assert query(left.toggle or "") == opened("Top", "Right")  # closes it
    assert [item.id for item in found.items if item.text == "Bottom"] == ["t-1-1-1", "t-1-2-0"]


def test_links_name_nodes_shown_open(client: TestClient) -> None:
    """Check that links name the nodes shown open, each once, by its representative, in tree order (M1).

    IRIs no node has, and open nodes under a closed one, are left out.
    """
    response = client.get(TREE, params=opened("Same", "Nope", "Alias", "Left", "Right", "Deep", "Top"))
    for name in ("Same", "Nope", "Deep"):
        assert urllib.parse.quote(ex(name), safe="") not in response.text
    found = Tree(response.text)
    assert query(found.item("Big").toggle or "") == opened("Top", "Left", "Alias", "Right", "Big")
    assert query(found.item("Bottom").load or "") == [("node", ex("Bottom")), ("at", "t-1-1-1"), *opened("Top", "Left")]


def test_close_drops_nodes_open_inside(client: TestClient) -> None:
    """Check that a node's close link leaves out the nodes shown open only inside it."""
    found = tree(client, opened("Top", "Left", "Right", "Bottom", "Alias"))
    assert [item.id for item in found.items if item.text == "Deep"] == ["t-1-1-1-0", "t-1-2-0-0"]
    assert query(found.item("Left").toggle or "") == opened("Top", "Bottom", "Right")  # Bottom's open under Right too
    assert query(found.item("Right").toggle or "") == opened("Top", "Left", "Alias", "Bottom")
    assert query(found.item("Top").toggle or "") == []
    stale = tree(client, opened("Left"))  # Left is under a closed node
    assert query(stale.item("Top").toggle or "") == opened("Top")


def test_open_needs_parents_open(client: TestClient) -> None:
    """Check that an open node under a closed one isn't shown."""
    assert tree(client, opened("Left")).shown() == [(0, "Free"), (0, "Top")]


def test_equivalence_group_one_node(client: TestClient) -> None:
    """Check that an equivalence group is one node, linking to its representative, and counted once (U-9)."""
    found = tree(client, opened("Top", "Left"))
    group = found.item("Alias ≡ Alias2 ≡ Alias3 ≡ … (+1)")
    assert (group.href, group.expanded) == ("/Alias", "false")
    with closing(connect(make_settings(), 1)) as connection:
        [node] = node_ids(connection, EntityType.CLASS, [ex("Same")]).values()
        read = read_node(connection, EntityType.CLASS, node, "_browl", "")
    assert read is not None
    assert [link.name for link in read.equivalents] == ["Alias2", "Alias3"]
    assert read.more_equivalents == 1


def test_open_by_other_member(client: TestClient) -> None:
    """Check that naming any member of a group opens it, and a fragment for any member is the group's."""
    found = tree(client, opened("Top", "Left", "Same"))
    group = found.item("Alias ≡ Alias2 ≡ Alias3 ≡ … (+1)")
    assert group.expanded == "true"
    assert found.item("Sub").id == "t-1-1-0-0"
    assert query(group.toggle or "") == opened("Top", "Left")
    assert query(found.item("Big").toggle or "") == opened("Top", "Left", "Alias", "Big")  # by its representative
    by_member = Tree(client.get(TREE, params=[("node", ex("Same")), ("at", "t-1-1-0"), *opened("Top", "Left")]).text)
    sub = by_member.item("Sub")
    assert (sub.id, sub.expanded) == ("t-1-1-0-0", None)


def test_fragment(client: TestClient) -> None:
    """Check that `node=` serves the node's children alone, their ids following from `at=`, marked noindex."""
    response = client.get(TREE, params=[("node", ex("Top")), ("at", "t-1"), *opened("Top")])
    assert response.status_code == 200
    assert response.headers["X-Robots-Tag"] == "noindex"
    assert "<html" not in response.text and "<li" in response.text
    found = Tree(response.text)
    assert [(item.id, item.text) for item in found.items] == [
        ("t-1-0", "Big"),
        ("t-1-1", "Left"),
        ("t-1-2", "Right"),
    ]
    left = found.item("Left")
    assert query(left.toggle or "") == opened("Top", "Left")
    assert query(left.load or "") == [("node", ex("Left")), ("at", "t-1-1"), *opened("Top")]


def test_fragment_opens_open_children(client: TestClient) -> None:
    """Check that a fragment shows its open children's children too."""
    found = Tree(client.get(TREE, params=[("node", ex("Top")), ("at", "t-1"), *opened("Top", "Right")]).text)
    assert found.shown() == [(0, "Big"), (0, "Left"), (0, "Right"), (1, "Bottom")]


@pytest.mark.parametrize(
    "params",
    [
        [("node", ex("Top"))],  # no `at`
        [("at", "t-1")],  # the roots' ids' prefix, without `node`, is a bare prefix
        [("at", "T")],
        [("at", "t"), ("children-of", ex("Top")), ("offset", "1")],
        [("at", "t"), ("offset", "2")],  # past the two roots
        [("node", ex("Top")), ("at", "x")],
        [("node", ex("Top")), ("at", "t-1"), ("at", "t-2")],
        [("node", ex("Top")), ("at", "t" + "-1" * 128)],  # one past `MAX_AT_CHARS`
        [("node", ex("Nope")), ("at", "t-1")],
        [("node", THING), ("at", "t-1")],  # in no hierarchy
        [("node", ex("p")), ("at", "t-1")],  # in another hierarchy
        [("node", ex("Top")), ("at", "t-1"), ("children-of", ex("Top"))],
        [("offset", "0")],
        [("offset", "01")],
        [("offset", "-1")],
        [("offset", "2")],  # past the two roots
        [("offset", "1"), ("offset", "1")],
        [("children-of", ex("Big")), *opened("Top", "Big")],  # no `offset`: the plain URL's page
        [("children-of", ex("Nope")), ("offset", "500")],
        [("children-of", ex("Big")), ("offset", "1250"), *opened("Top", "Big")],
        [("node", ex("Big")), ("at", "t-1-0"), ("offset", "1500")],
    ],
)
def test_not_found(client: TestClient, params: list[tuple[str, str]]) -> None:
    """Check that a missing or unknown node, a repeated parameter, or an offset past the last, is a 404."""
    assert client.get(TREE, params=params).status_code == 404


def test_long_at(client: TestClient) -> None:
    """Check that `at=` of `MAX_AT_CHARS` is served, its children's ids following from it."""
    at = "t" + "-1" * 127 + "0"
    assert len(at) == browl.trees.MAX_AT_CHARS
    found = Tree(client.get(TREE, params=[("node", ex("Top")), ("at", at)]).text)
    assert found.items[0].id == f"{at}-0"


def test_unknown_hierarchy(client: TestClient) -> None:
    """Check that a tree page other than the four is a 404."""
    assert client.get("/_browl/tree/individuals").status_code == 404


def test_unknown_open_ignored(client: TestClient) -> None:
    """Check that `open=` naming no node, or `owl:Thing`, is ignored."""
    assert tree(client, [("open", ex("Nope")), ("open", THING)]).shown() == [(0, "Free"), (0, "Top")]


def test_property_and_empty_trees(client: TestClient) -> None:
    """Check a property hierarchy's tree, and a hierarchy without entities."""
    found = tree(client, [("open", ex("p"))], "/_browl/tree/object-properties")
    assert found.shown() == [(0, "http://example.org/p"), (1, "http://example.org/q")]
    response = client.get("/_browl/tree/data-properties")
    assert response.status_code == 200
    assert Tree(response.text).items == []
    assert '<p class="note">None.</p>' in response.text


def test_children_paged(client: TestClient) -> None:
    """Check that a node shows a page of children, and a link to the next: in place with htmx, a page without."""
    found = tree(client, opened("Top", "Big"))
    kids = [item for item in found.items if item.depth == 2]
    assert [item.text for item in kids[:-1]] == [kid(n) for n in range(CHILD_PAGE)]
    more = kids[-1]
    assert more.id is None
    assert more.text == "Show the next 500 (501–1,000 of 1,250)"
    assert query(more.href) == [*opened("Top", "Big"), ("children-of", ex("Big")), ("offset", "500")]
    assert more.href.endswith("#t-1-0-500")
    assert query(more.load or "") == [("node", ex("Big")), ("at", "t-1-0"), *opened("Top"), ("offset", "500")]


def test_children_page_without_js(client: TestClient) -> None:
    """Check that `children-of=` and `offset=` show a later page of a node's children, with links either side."""
    params = [*opened("Top", "Big"), ("children-of", ex("Big")), ("offset", "1000")]
    found = tree(client, params)
    kids = [item for item in found.items if item.depth == 2]
    previous, *shown = kids
    assert previous.text == "Show the previous 500 (501–1,000 of 1,250)"
    assert query(previous.href) == [*opened("Top", "Big"), ("children-of", ex("Big")), ("offset", "500")]
    assert [item.text for item in shown] == [kid(n) for n in range(1000, KIDS)]  # the last page: no "more"
    assert shown[0].id == "t-1-0-1000"
    # A toggle keeps the page shown, so opening another node doesn't lose it.
    assert query(found.item("Left").toggle or "") == [*opened("Top", "Big", "Left"), *params[2:]]


def test_children_page_first_previous(client: TestClient) -> None:
    """Check that the link back to the first page has no offset, and says so, from an offset of less than a page."""
    params = [*opened("Top", "Big"), ("children-of", ex("Big")), ("offset", "200")]
    previous = next(item for item in tree(client, params).items if item.text.startswith("Show the "))
    assert previous.text == "Show the first 200 (1–200)"
    assert query(previous.href) == opened("Top", "Big")
    assert previous.href.endswith("#t-1-0")


def test_fragment_paged(client: TestClient) -> None:
    """Check that a fragment from an offset shows that page, with the link to the next if there is one."""
    response = client.get(TREE, params=[("node", ex("Big")), ("at", "t-1-0"), ("offset", "1000"), *opened("Big")])
    found = Tree(response.text)
    assert [item.text for item in found.items] == [kid(n) for n in range(1000, KIDS)]
    assert found.items[0].id == "t-1-0-1000"


def test_fragment_second_page(client: TestClient) -> None:
    """Check that a fragment from an offset links to the page after it, in place and without JS."""
    params = [("node", ex("Big")), ("at", "t-1-0"), *opened("Top"), ("offset", "500")]
    found = Tree(client.get(TREE, params=params).text)
    more = found.items[-1]
    assert more.text == "Show the remaining 250 (1,001–1,250)"
    assert query(more.load or "") == [("node", ex("Big")), ("at", "t-1-0"), *opened("Top"), ("offset", "1000")]
    assert query(more.href) == [*opened("Top", "Big"), ("children-of", ex("Big")), ("offset", "1000")]


def test_window_on_closed_node_ignored(client: TestClient) -> None:
    """Check that a window on a node that isn't shown open is ignored: the page shows its first page, if any."""
    found = tree(client, [*opened("Top"), ("children-of", ex("Big")), ("offset", "500")])
    assert found.shown() == [(0, "Free"), (0, "Top"), (1, "Big"), (1, "Left"), (1, "Right")]
    assert found.item("Big").expanded == "false"


def test_roots_paged(client: TestClient) -> None:
    """Check that the roots come a page at a time too: in place with htmx, a page without."""
    path = "/_browl/tree/annotation-properties"
    first = tree(client, path=path).items
    assert len(first) == CHILD_PAGE + 1
    more = first[-1]
    assert more.text == "Show the remaining 20 (501–520)"
    assert query(more.load or "") == [("at", "t"), ("offset", "500")]
    assert query(more.href) == [("offset", "500")]
    assert more.href.endswith("#t-500")
    second = tree(client, [("offset", "500")], path).items
    assert query(second[0].href) == []
    assert [item.text for item in second[1:]] == [f"note {n:03}" for n in range(500, NOTES)]
    assert client.get(path, params=[("offset", "520")]).status_code == 404


def test_roots_fragment(client: TestClient) -> None:
    """Check that a fragment without `node=` serves a page of the roots, their ids following from `at=`."""
    path = "/_browl/tree/annotation-properties"
    response = client.get(path, params=[("at", "t"), ("offset", "500")])
    assert response.headers["X-Robots-Tag"] == "noindex"
    assert "<html" not in response.text
    found = Tree(response.text)
    assert [(item.id, item.text) for item in found.items] == [(f"t-{n}", f"note {n:03}") for n in range(500, NOTES)]
    first = Tree(client.get(path, params=[("at", "t")]).text).items
    assert [item.text for item in first[:2]] == ["note 000", "note 001"]
    assert query(first[-1].load or "") == [("at", "t"), ("offset", "500")]


@pytest.mark.parametrize(("limit", "value"), [("MAX_SHOWN", 3), ("MAX_LINK_CHARS", 400), ("MAX_CHILDREN_READ", 5)])
def test_most_shown(client: TestClient, monkeypatch: pytest.MonkeyPatch, limit: str, value: int) -> None:
    """Check that past `MAX_SHOWN`, `MAX_LINK_CHARS` or `MAX_CHILDREN_READ`, open nodes show closed (S1, C3).

    Their toggles open only them and the nodes above them, so they fit.
    """
    monkeypatch.setattr(browl.trees, limit, value)
    found = tree(client, opened("Top", "Left", "Right"))
    # The roots and Top's children make 5, read from 5, and the open nodes' IRIs take 111 characters in each link.
    assert found.shown() == [(0, "Free"), (0, "Top"), (1, "Big"), (1, "Left"), (1, "Right")]
    left = found.item("Left")
    assert left.expanded == "false"
    assert query(left.toggle or "") == opened("Top", "Left")
    assert query(found.item("Top").toggle or "") == []  # closes it


@pytest.mark.parametrize(
    "params",
    [
        [*opened("Top", "Big"), ("children-of", ex("Big")), ("offset", "1250")],
        [("node", ex("Big")), ("at", "t-1-0"), ("offset", "1250")],
        [("offset", "2")],
        [("at", "t"), ("offset", "2")],
    ],
)
def test_offset_past_last_not_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, params: list[tuple[str, str]]
) -> None:
    """Check that an offset past the last child, or root, is a 404 before any page of them is read (C3)."""

    def read_children(*args: object, **kwargs: object) -> None:
        raise AssertionError("read")

    monkeypatch.setattr(browl.trees, "read_children", read_children)
    assert client.get(TREE, params=params).status_code == 404


def test_most_open(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that only the first `MAX_OPEN` open nodes are taken, each once."""
    monkeypatch.setattr(browl.trees, "MAX_OPEN", 2)
    found = tree(client, opened("Top", "Top", "Left", "Right"))
    assert (found.item("Left").expanded, found.item("Right").expanded) == ("true", "false")
    # A third would pass it, so Right's toggle opens only it and the node above it.
    assert query(found.item("Right").toggle or "") == opened("Top", "Right")
    assert query(found.item("Big").toggle or "") == opened("Top", "Big")


def test_read_children_order(client: TestClient) -> None:
    """Check that children come by display name, case-insensitively, then IRI, from an offset."""
    with closing(connect(make_settings(), 1)) as connection:
        ids = node_ids(connection, EntityType.CLASS, [ex("Left"), ex("Big")])
        left = read_node(connection, EntityType.CLASS, ids[ex("Left")], "_browl", "/root")
        big = read_node(connection, EntityType.CLASS, ids[ex("Big")], "_browl", "")
        assert left is not None and big is not None
        page = read_children(connection, EntityType.CLASS, left, "_browl", "/root")
        assert [(node.link.name, node.link.href) for node in page.nodes] == [
            ("Alias", "/root/Alias"),
            ("Bottom", "/root/Bottom"),
        ]
        page = read_children(connection, EntityType.CLASS, big, "_browl", "", offset=1245, limit=3)
        assert ([node.link.name for node in page.nodes], page.offset, page.end, page.total) == (
            [kid(1245), kid(1246), kid(1247)],
            1245,
            1248,
            KIDS,
        )
        roots = read_children(connection, EntityType.CLASS, None, "_browl", "")
        assert [(node.link.name, node.children, node.descendants) for node in roots.nodes] == [
            ("Free", 0, 0),
            ("Top", 3, 1257),
        ]
