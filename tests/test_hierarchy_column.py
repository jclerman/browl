"""Tests for the hierarchy column on entity pages (P-38, U-45, DESIGN.md §2.9 "Hierarchy column")."""

import html.parser
import itertools
import json
import re
import urllib.parse
from collections.abc import Iterator
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from browl.derived import EntityType
from browl.hierarchy import CHILD_PAGE
from tests.helpers import connect, derived_stage, make_app, make_settings
from tests.test_trees import EX, SUB_CLASS, SUB_PROPERTY, THING, Tree, ex, loaded, opened, query, tree_stage

pytestmark = pytest.mark.usefixtures("data_dir")


@pytest.fixture
def app() -> FastAPI:
    """Return an app with default settings."""
    return make_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Yield a test client for the app, with `test_trees`' hierarchies loaded."""
    with loaded(app, tree_stage()) as test_client:
        yield test_client


class Column(html.parser.HTMLParser):
    """An entity page's hierarchy column: its headings, and the HTML of its trees, for `Tree`.

    Attributes:
        headings: Each tree's heading's text, and the link after it.
        trees: Each tree's HTML.
    """

    def __init__(self, page: str) -> None:
        """Parse a page."""
        super().__init__()
        self.headings: list[tuple[str, str]] = []
        match = re.search(r'<nav class="hierarchy-column".*?</nav>', page, re.DOTALL)
        self.html = match.group(0) if match else ""
        self.trees = re.findall(r'<ul class="tree" role="tree".*?\n  </ul>', self.html, re.DOTALL)
        self._heading: list[str] | None = None
        self._href: str | None = None
        self.feed(self.html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Start a heading's text."""
        if tag == "h2":
            self._heading, self._href = [], None
        elif tag == "a" and self._heading is not None:
            self._href = dict(attrs).get("href") or ""

    def handle_endtag(self, tag: str) -> None:
        """End a heading."""
        if tag == "h2" and self._heading is not None:
            self.headings.append(("".join(self._heading), self._href or ""))
            self._heading = None

    def handle_data(self, data: str) -> None:
        """Collect a heading's text."""
        if self._heading is not None and self._href is None:
            self._heading.append(data)


def column(client: TestClient, path: str) -> Column:
    """Return an entity page's column, checking that the page is served."""
    response = client.get(path)
    assert response.status_code == 200, response.text
    return Column(response.text)


def shown(found: Column, index: int = 0) -> list[tuple[int, str]]:
    """Return a column tree's items shown: their depths and texts."""
    return Tree(found.trees[index]).shown()


def test_merged_tree(client: TestClient) -> None:
    """Check that a node with two parents is drawn under its first, by name, and is a reference under the other."""
    found = column(client, "/Deep")
    assert found.headings == [("Local hierarchy", "/_browl/tree/classes")]
    assert shown(found) == [
        (0, "Top"),
        (1, "Left"),
        (2, "Bottom"),
        (3, "Deep"),
        (1, "Right"),
        (2, "Bottom (also above)"),
    ]
    items = Tree(found.trees[0]).items
    assert [item.id for item in items] == ["hc-0", "hc-0-0", "hc-0-0-0", "hc-0-0-0-0", "hc-0-1", "hc-0-1-0"]
    reference = items[-1]
    assert (reference.href, reference.toggle, reference.expanded) == ("/Bottom", None, None)


def test_entity_highlighted_with_children(client: TestClient) -> None:
    """Check that the entity is marked as the page's, open, with its children closed below it."""
    page = client.get("/Bottom").text
    assert page.count('aria-current="page"') == 1
    assert re.search(r'id="hc-0-0-0" href="/Bottom" class="tree-current" aria-current="page"', page)
    found = Column(page)
    assert shown(found) == [
        (0, "Top"),
        (1, "Left"),
        (2, "Bottom"),
        (3, "Deep"),
        (2, "Show its sibling (1)"),  # after its first parent's children shown
        (1, "Right"),
        (2, "Bottom (also above)"),
    ]
    deep = Tree(found.trees[0]).item("Deep")
    assert (deep.id, deep.expanded) == ("hc-0-0-0-0", None)  # a leaf


def test_reference_below(app: FastAPI) -> None:
    """Check that a reference drawn before the node it stands for says it's below."""
    names = {"Ant", "Zed", "Pa", "Pb", "X"}
    entities = [(ex(name), EntityType.CLASS) for name in names]
    edges = [(ex("Pa"), SUB_CLASS, ex("Zed")), (ex("Pb"), SUB_CLASS, ex("Ant"))]
    edges += [(ex("X"), SUB_CLASS, ex("Pa")), (ex("X"), SUB_CLASS, ex("Pb"))]  # under Pa, its first parent
    stage = derived_stage(entities, names={ex(name): (name, None) for name in names}, edges=edges)
    with loaded(app, stage) as client:
        found = column(client, "/X")
    assert shown(found) == [(0, "Ant"), (1, "Pb"), (2, "X (also below)"), (0, "Zed"), (1, "Pa"), (2, "X")]


def test_root(client: TestClient) -> None:
    """Check that a root's column shows only it and its children, closed, by name, and a link to the other roots."""
    found = column(client, "/Top")
    assert shown(found) == [(0, "Top"), (1, "Big"), (1, "Left"), (1, "Right"), (0, "Show its sibling (1)")]
    siblings = Tree(found.trees[0]).items[-1]
    assert siblings.href == "/_browl/tree/classes"  # without JS, the roots' first page, which holds Top
    assert query(siblings.load or "") == [("at", "hc"), ("siblings-of", ex("Top"))]
    big = Tree(found.trees[0]).item("Big")
    assert big.expanded == "false"
    assert big.toggle == "/Big"  # without JS, its page, whose column shows its children
    assert query(big.load or "") == [("node", ex("Big")), ("at", "hc-0-0")]  # naming no open nodes


def test_open_toggles_load_pages(client: TestClient) -> None:
    """Check that, without JS, an open node's toggle loads its page too, and its fragment's URL names no open nodes."""
    items = {item.text: item for item in Tree(column(client, "/Deep").trees[0]).items}
    assert (items["Top"].toggle, items["Bottom"].toggle) == ("/Top", "/Bottom")
    assert query(items["Bottom"].load or "") == [("node", ex("Bottom")), ("at", "hc-0-0-0")]


def test_links_independent_of_depth(app: FastAPI) -> None:
    """Check that the column's links don't grow with the entity's depth, as they'd repeat for each of its children."""
    names = [f"c{index:02}" for index in range(40)]
    entities = [(ex(name), EntityType.CLASS) for name in [*names, "leaf"]]
    edges = [(ex(child), SUB_CLASS, ex(parent)) for parent, child in itertools.pairwise(names)]
    edges += [(ex("leaf"), SUB_CLASS, ex(names[-1]))]
    with loaded(app, derived_stage(entities, edges=edges)) as client:
        page = client.get(f"/{names[-2]}").text
    links = re.findall(r'(?:href|hx-get)="([^"]*)"', Column(page).html)
    assert len(links) > 2 * 40
    assert max(urllib.parse.unquote(link).count(EX) for link in links) == 1  # its node's, not those above it


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/Free", [(0, "Free"), (0, "Show its sibling (1)")]),
        ("/_browl/iri?iri=http%3A%2F%2Fexample.org%2Fnote3", [(0, "note 003"), (0, "Show its siblings (519)")]),
    ],
)
def test_isolated(client: TestClient, path: str, expected: list[tuple[int, str]]) -> None:
    """Check that an entity with neither parents nor children has a column: itself, and a link to the other roots."""
    assert shown(column(client, path)) == expected


def test_fold_control(client: TestClient) -> None:
    """Check that a page with a column has its fold control, unfolded by default, and the handle that widens it."""
    page = client.get("/Top").text
    assert '<input type="checkbox" id="hierarchy-shown" class="visually-hidden" checked' in page
    assert '<div class="hierarchy-resize" role="separator"' in page


def test_not_in_panel(client: TestClient) -> None:
    """Check that a tab panel served alone has no column, as the page keeps its own."""
    assert "hierarchy-column" not in client.get("/Top?panel=1").text
    assert "hierarchy-column" in client.get("/Top?tab=usage").text


def test_equivalence_group(client: TestClient) -> None:
    """Check that an equivalence group is one node, also when its page is a member's other than its representative."""
    group = "Alias ≡ Alias2 ≡ Alias3 ≡ … (+1)"
    assert shown(column(client, "/Sub")) == [(0, "Top"), (1, "Left"), (2, group), (3, "Sub")]
    sibling = (2, "Show its sibling (1)")
    assert shown(column(client, "/Alias")) == [(0, "Top"), (1, "Left"), (2, group), (3, "Sub"), sibling]
    page = client.get("/Same").text
    assert re.search(r'href="/Alias" class="tree-current" aria-current="page"', page)


def test_property(client: TestClient) -> None:
    """Check that a property's column is its property hierarchy's."""
    found = column(client, "/q")
    assert found.headings == [("Local hierarchy", "/_browl/tree/object-properties")]
    assert shown(found) == [(0, ex("p")), (1, ex("q"))]  # unnamed, so shown by IRI
    assert Tree(found.trees[0]).items[0].id == "ho-0"


def test_punned(app: FastAPI) -> None:
    """Check that a punned IRI gets a tree for each hierarchy it's in, in the navigation bar's order, each named."""
    entities = [(ex(name), EntityType.CLASS) for name in ("Top", "pun")]
    entities += [(ex(name), EntityType.OBJECT_PROPERTY) for name in ("p", "pun")]
    entities += [(ex("pun"), EntityType.ANNOTATION_PROPERTY)]  # a root, alone
    edges = [(ex("pun"), SUB_CLASS, ex("Top")), (ex("pun"), SUB_PROPERTY, ex("p"))]
    stage = derived_stage(entities, names={ex(name): (name, None) for name in ("Top", "pun", "p")}, edges=edges)
    with loaded(app, stage) as client:
        found = column(client, "/pun")
    assert [heading for heading, _ in found.headings] == [
        "Local hierarchy: Classes",
        "Local hierarchy: Object properties",
        "Local hierarchy: Annotation properties",
    ]
    assert [shown(found, index) for index in (0, 1, 2)] == [
        [(0, "Top"), (1, "pun")],
        [(0, "p"), (1, "pun")],
        [(0, "pun")],
    ]
    assert [Tree(tree).items[0].id for tree in found.trees] == ["hc-0", "ho-0", "ha-0"]


def test_children_paged(client: TestClient) -> None:
    """Check that the entity's children are paged as on the tree pages, and that the next page's fragment is served."""
    items = Tree(column(client, "/Big").trees[0]).items
    assert len(items) == 2 + CHILD_PAGE + 2  # Top, Big, its first page, "Show the next 500" and its siblings' link
    more = items[-2]
    assert more.text == f"Show the next {CHILD_PAGE} (501–1,000 of 1,250)"
    assert query(more.href) == [*opened("Top", "Big"), ("children-of", ex("Big")), ("offset", str(CHILD_PAGE))]
    assert query(more.load or "") == [("node", ex("Big")), ("at", "hc-0-0"), ("offset", str(CHILD_PAGE))]
    fragment = client.get(more.load or "")
    assert fragment.status_code == 200
    items = Tree(fragment.text).items
    assert items[0].id == f"hc-0-0-{CHILD_PAGE}"
    assert items[-1].href == "/Big"  # without JS, Big's page, which shows its children's pages
    assert query(items[-1].load or "") == [("node", ex("Big")), ("at", "hc-0-0"), ("offset", str(2 * CHILD_PAGE))]


def test_child_fragment(client: TestClient) -> None:
    """Check that a child's toggle fetches its children, with the column's ids and links."""
    left = Tree(column(client, "/Top").trees[0]).item("Left")
    fragment = client.get(left.load or "")
    assert fragment.status_code == 200
    items = Tree(fragment.text).items
    assert [(item.id, item.text) for item in items] == [
        ("hc-0-1-0", "Alias ≡ Alias2 ≡ Alias3 ≡ … (+1)"),
        ("hc-0-1-1", "Bottom"),
    ]
    bottom = items[1]
    assert bottom.toggle == "/Bottom"
    assert query(bottom.load or "") == [("node", ex("Bottom")), ("at", "hc-0-1-1")]


def test_owl_thing_not_shown(client: TestClient) -> None:
    """Check that owl:Thing, which is in no hierarchy, isn't an ancestor, and has no column."""
    assert all(text != THING for _, text in shown(column(client, "/Left")))
    assert "hierarchy-column" not in client.get(f"/_browl/iri?iri={THING}").text


def test_reads_use_index(client: TestClient) -> None:
    """Check that walking up `edge` looks each level's nodes up by its `edge_child` index, not by a scan."""
    with closing(connect(make_settings(), 1)) as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT child, parent FROM edge"
            " WHERE hierarchy = ? AND child IN (SELECT value FROM json_each(?))",
            ("class", json.dumps([1, 2])),
        ).fetchall()
    assert any("edge_child" in row[-1] for row in plan), plan


def test_siblings(client: TestClient) -> None:
    """Check that the siblings' fragment shows the first parent's children, the entity among them, open and marked."""
    siblings = Tree(column(client, "/Bottom").trees[0]).item("Show its sibling (1)")
    assert siblings.href == "/Left"  # without JS, the parent's page, whose column shows its children
    assert query(siblings.load or "") == [("node", ex("Left")), ("at", "hc-0-0"), ("siblings-of", ex("Bottom"))]
    fragment = client.get(siblings.load or "")
    assert fragment.status_code == 200
    items = Tree(fragment.text).items
    group = "Alias ≡ Alias2 ≡ Alias3 ≡ … (+1)"
    assert [(item.id, item.text, item.expanded) for item in items] == [
        ("hc-0-0-0", group, "false"),
        ("hc-0-0-1", "Bottom", "true"),
        ("hc-0-0-1-0", "Deep", None),
    ]
    assert items[0].toggle == "/Alias"
    assert re.search(r'id="hc-0-0-1" href="/Bottom" class="tree-current" aria-current="page"', fragment.text)


def test_siblings_page_holds_entity(client: TestClient) -> None:
    """Check that the siblings' fragment shows the page of them that holds the entity, after a link to those before."""
    siblings = Tree(column(client, "/kid1249").trees[0]).item("Show its siblings (1,249)")
    items = Tree(client.get(siblings.load or "").text).items
    previous = items[0]
    assert (previous.text, previous.href) == (f"Show the previous {CHILD_PAGE} (501–1,000 of 1,250)", "/Big")
    assert [item.id for item in items[1:3]] == ["hc-0-0-1000", "hc-0-0-1001"]
    assert len(items) == 1 + 250
    assert re.search(r'href="/kid1249" class="tree-current" aria-current="page"', client.get(siblings.load or "").text)


def test_siblings_of_root(client: TestClient) -> None:
    """Check that a root's siblings' fragment shows the roots, with the column's links."""
    siblings = Tree(column(client, "/Free").trees[0]).items[-1]
    items = Tree(client.get(siblings.load or "").text).items
    assert [(item.id, item.text) for item in items] == [("hc-0", "Free"), ("hc-1", "Top")]
    assert items[1].toggle == "/Top"


@pytest.mark.parametrize(
    "params",
    [
        [("node", ex("Left")), ("at", "hc-0-0"), ("siblings-of", ex("Deep"))],  # not its child
        [("node", ex("Left")), ("at", "hc-0-0"), ("siblings-of", ex("Nope"))],
        [("node", ex("Left")), ("at", "t-0"), ("siblings-of", ex("Bottom"))],  # a tree page's id
        [("node", ex("Left")), ("at", "hc-0-0"), ("siblings-of", ex("Bottom")), ("offset", "500")],
        [("node", ex("Left")), ("siblings-of", ex("Bottom"))],  # no `at`
        [("at", "hc"), ("siblings-of", ex("Left"))],  # not a root
    ],
)
def test_siblings_not_found(client: TestClient, params: list[tuple[str, str]]) -> None:
    """Check that a siblings' fragment for a node that isn't the parent's child, or with another id, is a 404."""
    assert client.get("/_browl/tree/classes", params=params).status_code == 404
