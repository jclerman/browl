"""Tests for the hierarchy column on entity pages (P-38, U-45, DESIGN.md §2.9 "Hierarchy column")."""

import asyncio
import html.parser
import itertools
import logging
import re
import urllib.parse
from collections.abc import Iterator
from contextlib import closing
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import browl.hierarchy
import browl.hierarchy_column
import browl.lookup
from browl.derived import EntityType
from browl.fuseki import UNION_FROM, FusekiClient, SelectResult, SparqlError, SparqlTimeoutError
from browl.hierarchy import CHILD_PAGE
from browl.hierarchy_column import MAX_COLUMN_DEPTH, NO_NAMED_TYPE, IndividualTypes, fetch_types
from browl.rdf import IRI, OWL, RDF, Literal
from tests.helpers import connect, derived_stage, fuseki_dataset, make_app, make_settings
from tests.test_trees import (
    CLASS_EDGES,
    CLASSES,
    EX,
    SUB_CLASS,
    SUB_PROPERTY,
    THING,
    TREE,
    Tree,
    ex,
    loaded,
    opened,
    query,
    tree_stage,
)

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
    assert siblings.href == "/_browl/tree/classes#t-1"  # without JS, the roots' first page, which holds Top
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
    names = [f"c{index:02}" for index in range(MAX_COLUMN_DEPTH)]
    entities = [(ex(name), EntityType.CLASS) for name in [*names, "leaf"]]
    edges = [(ex(child), SUB_CLASS, ex(parent)) for parent, child in itertools.pairwise(names)]
    edges += [(ex("leaf"), SUB_CLASS, ex(names[-1]))]
    with loaded(app, derived_stage(entities, edges=edges)) as client:
        page = client.get(f"/{names[-2]}").text
    links = re.findall(r'(?:href|hx-get)="([^"]*)"', Column(page).html)
    assert len(links) > 2 * MAX_COLUMN_DEPTH
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
    # Without JS, the tree page, with the nodes above them open, at the next page.
    assert query(items[-1].href) == [*opened("Top", "Big"), ("children-of", ex("Big")), ("offset", str(2 * CHILD_PAGE))]
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
    """Check that walking up `edge`, and reading each ancestor's parents, look them up by `edge_child`, not a scan."""
    with closing(connect(make_settings(), 1)) as connection:
        plan = connection.execute(
            f"EXPLAIN QUERY PLAN {browl.hierarchy._ANCESTORS}", (1, "class", 10, "class")
        ).fetchall()
    assert sum("USING COVERING INDEX edge_child" in row[-1] for row in plan) == 2, plan


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
    fragment = client.get(siblings.load or "").text
    items = Tree(fragment).items
    previous = items[0]
    assert previous.text == f"Show the previous {CHILD_PAGE} (501–1,000 of 1,250)"
    # Without JS, the tree page with the nodes above them open, at that page; with htmx, it goes before this one.
    assert query(previous.href) == [*opened("Top", "Big"), ("children-of", ex("Big")), ("offset", str(CHILD_PAGE))]
    assert query(previous.load or "") == [("node", ex("Big")), ("at", "hc-0-0"), ("before", "1000")]
    assert [item.id for item in items[1:3]] == ["hc-0-0-1000", "hc-0-0-1001"]
    assert len(items) == 1 + 250
    assert re.search(r'href="/kid1249" class="tree-current" aria-current="page"', fragment)


def siblings_page(client: TestClient, path: str, tree: int = 0) -> Tree:
    """Return the siblings' fragment that an entity page's column links to."""
    siblings = Tree(column(client, path).trees[tree]).items[-1]
    assert siblings.text.startswith("Show its sibling"), siblings
    fragment = client.get(siblings.load or "")
    assert fragment.status_code == 200
    return Tree(fragment.text)


def test_siblings_first_page(client: TestClient) -> None:
    """Check that the siblings' first page has no link before it, and one after it, to the next page."""
    items = siblings_page(client, "/kid0").items
    assert [item.id for item in items[:2]] == ["hc-0-0-0", "hc-0-0-1"]
    assert items[0].expanded is None and items[0].text == "Kid 0000"
    more = items[-1]
    assert (more.id, more.text) == (None, f"Show the next {CHILD_PAGE} (501–1,000 of 1,250)")
    assert query(more.href) == [*opened("Top", "Big"), ("children-of", ex("Big")), ("offset", str(CHILD_PAGE))]
    assert query(more.load or "") == [("node", ex("Big")), ("at", "hc-0-0"), ("offset", str(CHILD_PAGE))]
    assert len(items) == CHILD_PAGE + 1


def test_siblings_middle_page(client: TestClient) -> None:
    """Check that a middle page of siblings links to the pages before and after it, with and without JS."""
    items = siblings_page(client, "/kid600").items
    previous, more = items[0], items[-1]
    assert previous.text == f"Show the first {CHILD_PAGE} (1–500)"
    assert query(previous.href) == opened("Top", "Big")  # the first page, which needs no offset
    assert query(previous.load or "") == [("node", ex("Big")), ("at", "hc-0-0"), ("before", str(CHILD_PAGE))]
    assert more.text == "Show the remaining 250 (1,001–1,250)"
    assert query(more.href) == [*opened("Top", "Big"), ("children-of", ex("Big")), ("offset", "1000")]
    assert query(more.load or "") == [("node", ex("Big")), ("at", "hc-0-0"), ("offset", "1000")]
    assert [item.id for item in items[1:3]] == ["hc-0-0-500", "hc-0-0-501"]
    assert items[101].text == "Kid 0600" and items[101].id == "hc-0-0-600"


def test_page_before(client: TestClient) -> None:
    """Check that a `before=` page links to the page before it, if any, not to the one after, which it goes before."""
    items = Tree(client.get(TREE, params=[("node", ex("Big")), ("at", "hc-0-0"), ("before", "1000")]).text).items
    previous = items[0]
    assert previous.text == f"Show the first {CHILD_PAGE} (1–500)"
    assert query(previous.load or "") == [("node", ex("Big")), ("at", "hc-0-0"), ("before", str(CHILD_PAGE))]
    assert [item.id for item in (items[1], items[-1])] == ["hc-0-0-500", "hc-0-0-999"]
    assert len(items) == 1 + CHILD_PAGE  # and no "more"
    items = Tree(client.get(previous.load or "").text).items
    assert [item.id for item in (items[0], items[-1])] == ["hc-0-0-0", "hc-0-0-499"]
    assert len(items) == CHILD_PAGE


def test_siblings_of_later_root(client: TestClient) -> None:
    """Check that a root past the first page of roots links to its page of roots, which links to the one before."""
    path = "/_browl/iri?iri=http%3A%2F%2Fexample.org%2Fnote510"
    siblings = Tree(column(client, path).trees[0]).items[-1]
    assert siblings.text == "Show its siblings (519)"
    assert siblings.href == f"/_browl/tree/annotation-properties?offset={CHILD_PAGE}#t-510"
    assert query(siblings.load or "") == [("at", "ha"), ("siblings-of", ex("note510"))]
    fragment = client.get(siblings.load or "").text
    items = Tree(fragment).items
    previous = items[0]
    assert (previous.text, previous.href) == (
        f"Show the first {CHILD_PAGE} (1–500)",
        "/_browl/tree/annotation-properties",
    )
    assert query(previous.load or "") == [("at", "ha"), ("before", str(CHILD_PAGE))]
    assert [item.id for item in (items[1], items[-1])] == ["ha-500", "ha-519"]
    assert len(items) == 1 + 20
    assert re.search(r'id="ha-510" href="[^"]*" class="tree-current" aria-current="page"', fragment)


def test_siblings_in_property_hierarchy(app: FastAPI) -> None:
    """Check that a property's siblings' fragment has its hierarchy's ids, and that its counts are a node's."""
    entities = [(ex(name), EntityType.OBJECT_PROPERTY) for name in ("p", "q", "r")]
    edges = [(ex("q"), SUB_PROPERTY, ex("p")), (ex("r"), SUB_PROPERTY, ex("p"))]
    with loaded(app, derived_stage(entities, edges=edges)) as client:
        found = Tree(column(client, "/q").trees[0])
        assert [(item.id, item.text) for item in found.items] == [
            ("ho-0", ex("p")),
            ("ho-0-0", ex("q")),
            (None, "Show its sibling (1)"),
        ]
        assert found.items[0].counts == "2 children · 2 descendants"  # the tree pages' words, for properties too
        assert query(found.items[-1].load or "") == [("node", ex("p")), ("at", "ho-0"), ("siblings-of", ex("q"))]
        items = Tree(client.get(found.items[-1].load or "").text).items
    assert [(item.id, item.text) for item in items] == [("ho-0-0", ex("q")), ("ho-0-1", ex("r"))]


def chain(length: int) -> tuple[list[str], object]:
    """Return the names of a chain of classes, each the last's subclass, and a stage that loads them."""
    names = [f"c{index:03}" for index in range(length)]
    entities = [(ex(name), EntityType.CLASS) for name in names]
    edges = [(ex(child), SUB_CLASS, ex(parent)) for parent, child in itertools.pairwise(names)]
    return names, derived_stage(entities, names={ex(name): (name, None) for name in names}, edges=edges)


def test_deep_chain_elided(app: FastAPI) -> None:
    """Check that past `MAX_COLUMN_DEPTH` levels, one row stands for those left out, linking to the entity's path."""
    names, stage = chain(40)
    with loaded(app, stage) as client:
        page = client.get("/c039").text
    found = Tree(Column(page).trees[0])
    hidden = 40 - MAX_COLUMN_DEPTH + 1
    bottom = MAX_COLUMN_DEPTH - 3 - 1
    assert found.shown() == [
        (0, "c000"),
        (1, "c001"),
        (2, "c002"),
        (3, f"… {hidden} levels hidden"),
        *((4 + index, name) for index, name in enumerate(names[-bottom:])),
    ]
    assert len(names[-bottom:]) + 4 == MAX_COLUMN_DEPTH
    row = found.item(f"… {hidden} levels hidden")
    assert (row.id, row.expanded) == ("hc-0-0-0-0", "true")
    assert query(row.href) == opened(*names[:-1])
    assert row.toggle == row.href
    assert found.items[4].id == "hc-0-0-0-0-0"
    assert re.search(r'data-depth="14">.*?id="hc(?:-0){15}" href="/c039" class="tree-current"', page, re.DOTALL)
    assert page.count('aria-current="page"') == 1


@pytest.mark.parametrize("length", [MAX_COLUMN_DEPTH, MAX_COLUMN_DEPTH + 1])
def test_chain_at_cap(app: FastAPI, length: int) -> None:
    """Check that a chain of `MAX_COLUMN_DEPTH` levels is drawn whole, and one more is elided, two levels in a row."""
    names, stage = chain(length)
    with loaded(app, stage) as client:
        found = shown(column(client, f"/{names[-1]}"))
    if length == MAX_COLUMN_DEPTH:
        assert found == list(enumerate(names))
    else:
        assert found == [*enumerate(names[:3]), (3, "… 2 levels hidden"), *enumerate(names[5:], start=4)]


def test_very_deep_chain(app: FastAPI) -> None:
    """Check that an entity hundreds of levels deep gets its page, with its column elided."""
    _, stage = chain(300)
    with loaded(app, stage) as client:
        found = shown(column(client, "/c299"))
    assert found[3] == (3, "… 286 levels hidden")
    assert found[-1] == (MAX_COLUMN_DEPTH - 1, "c299")


def test_deeper_branch_closed(app: FastAPI) -> None:
    """Check that, where a parent other than the entity's first is deeper, its branch stops at the last level drawn."""
    names, _ = chain(20)
    entities = [(ex(name), EntityType.CLASS) for name in [*names, "A", "E"]]
    edges = [(ex(child), SUB_CLASS, ex(parent)) for parent, child in itertools.pairwise(names)]
    edges += [(ex("E"), SUB_CLASS, ex("A")), (ex("E"), SUB_CLASS, ex(names[-1]))]  # under A, its first parent
    labels = {ex(name): (name, None) for name in [*names, "A", "E"]}
    with loaded(app, derived_stage(entities, names=labels, edges=edges)) as client:
        found = Tree(column(client, "/E").trees[0])
    assert found.shown() == [(0, "A"), (1, "E"), *enumerate(names[:MAX_COLUMN_DEPTH])]
    last = found.items[-1]
    assert (last.text, last.expanded) == (names[MAX_COLUMN_DEPTH - 1], "false")  # opens to its children, as any node


def test_elided_dag(app: FastAPI) -> None:
    """Check that the row for the levels left out holds only those after them, and a reference elsewhere stays."""
    names, _ = chain(20)
    entities = [(ex(name), EntityType.CLASS) for name in [*names, "zz"]]
    edges = [(ex(child), SUB_CLASS, ex(parent)) for parent, child in itertools.pairwise(names)]
    edges += [(ex(names[-1]), SUB_CLASS, ex("zz"))]
    labels = {ex(name): (name, None) for name in [*names, "zz"]}
    with loaded(app, derived_stage(entities, names=labels, edges=edges)) as client:
        found = shown(column(client, f"/{names[-1]}"))
    assert found == [
        *enumerate(names[:3]),
        (3, "… 6 levels hidden"),
        *enumerate(names[9:], start=4),
        (0, "zz"),
        (1, f"{names[-1]} (also above)"),
    ]


def test_most_ancestors(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that past `MAX_ANCESTORS`, the column keeps those nearest the entity, the farthest at the top."""
    monkeypatch.setattr(browl.hierarchy, "MAX_ANCESTORS", 3)
    _, stage = chain(10)
    with loaded(app, stage) as client:
        assert shown(column(client, "/c009")) == [(0, "c006"), (1, "c007"), (2, "c008"), (3, "c009")]


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
        [("node", ex("Left")), ("at", "hc-0-0"), ("siblings-of", ex("Bottom")), ("children-of", ex("Left"))],
        [("node", ex("Left")), ("at", "hc-0-0"), ("siblings-of", ex("Bottom")), ("siblings-of", ex("Bottom"))],
        [("node", ex("Left")), ("at", "x-0"), ("siblings-of", ex("Bottom"))],  # neither prefix
        [("node", ex("Big")), ("at", "hc-0-0"), ("before", "1000"), ("siblings-of", ex("kid0"))],
        [("node", ex("Big")), ("at", "hc-0-0"), ("before", "1000"), ("offset", "500")],
        [("node", ex("Big")), ("at", "hc-0-0"), ("before", "600")],  # not where a page starts
        [("node", ex("Big")), ("at", "hc-0-0"), ("before", "1500")],  # past the last
        [("node", ex("Big")), ("at", "hc-0-0"), ("before", "0")],
        [("node", ex("Big")), ("at", "t-0-0"), ("before", "500")],  # a tree page's
        [("node", ex("Big")), ("before", "500")],  # no `at`
    ],
)
def test_siblings_not_found(client: TestClient, params: list[tuple[str, str]]) -> None:
    """Check that a column's fragment for a node not the parent's child, or with bad parameters, is a 404."""
    assert client.get("/_browl/tree/classes", params=params).status_code == 404


IND = "◆ind (named individual)"
"""An individual's node's text in its tree: a diamond, its name, and words for screen readers, as no class's."""


def individual_stage(*punned: EntityType) -> object:
    """Return `tree_stage` with an individual, `ind`, which may be punned with entities of other types too."""
    entities = [(ex("ind"), EntityType.NAMED_INDIVIDUAL), *((ex("ind"), each) for each in punned)]
    edges = [(ex("ind"), SUB_CLASS, ex("Right"))] if EntityType.CLASS in punned else []
    names: dict[str, tuple[str | None, str | None]] = {ex("ind"): ("ind", None)}
    names |= {ex(name): (name, None) for name in CLASSES}
    return derived_stage(
        [*entities, *((ex(name), EntityType.CLASS) for name in CLASSES)],
        names=names,
        edges=[*edges, *((ex(c), r, ex(p)) for c, r, p in CLASS_EDGES if p != "Thing")],
    )


def with_types(monkeypatch: pytest.MonkeyPatch, *names: str, count: int | None = None) -> None:
    """Make the page's individual's types those named, without a query, `count` of them in all."""

    async def fetched(*args: object) -> IndividualTypes:
        iris = tuple(ex(name) for name in names)
        return IndividualTypes(iris, len(iris) if count is None else count)

    monkeypatch.setattr(browl.lookup, "fetch_types", fetched)


def test_individual(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check an individual's tree: its types' ancestors merged, it beneath its types, with no children or siblings."""
    with_types(monkeypatch, "Deep", "Right", "Nowhere")  # Nowhere is in no hierarchy
    with loaded(app, individual_stage()) as client:
        found = column(client, "/ind")
    assert found.headings == [("Local hierarchy", "/_browl/tree/classes")]
    assert shown(found) == [
        (0, "Top"),
        (1, "Left"),
        (2, "Bottom"),
        (3, "Deep"),
        (4, IND),  # under Deep, though it has no subclasses; marked as no class
        (1, "Right"),
        (2, "Bottom (also above)"),
        (2, f"{IND} (also above)"),
    ]
    items = Tree(found.trees[0]).items
    assert [item.id for item in items][:5] == ["hi-0", "hi-0-0", "hi-0-0-0", "hi-0-0-0-0", "hi-0-0-0-0-0"]
    ind = items[4]
    assert (ind.href, ind.toggle, ind.expanded) == ("/ind", None, None)  # no children
    assert "Show its sibling" not in found.html  # nor siblings
    assert '<p class="note">' not in found.html


def test_individual_fragment(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a toggle in an individual's tree fetches a type's children with the individual tree's ids."""
    with_types(monkeypatch, "Top")
    with loaded(app, individual_stage()) as client:
        top = Tree(column(client, "/ind").trees[0]).item("Top")
        assert top.id == "hi-0"
        fragment = client.get(top.load or "")
    assert fragment.status_code == 200
    items = Tree(fragment.text).items
    assert [(item.id, item.text) for item in items] == [("hi-0-0", "Big"), ("hi-0-1", "Left"), ("hi-0-2", "Right")]
    assert query(items[1].load or "") == [("node", ex("Left")), ("at", "hi-0-1")]


@pytest.mark.parametrize("names", [(), ("Nowhere",)], ids=["none", "unknown"])
def test_individual_no_named_type(app: FastAPI, monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...]) -> None:
    """Check that an individual with no type in the class hierarchy is alone in its tree, with a note saying so."""
    with_types(monkeypatch, *names)
    with loaded(app, individual_stage()) as client:
        found = column(client, "/ind")
    assert shown(found) == [(0, IND)]
    assert f'<p class="note">{NO_NAMED_TYPE}</p>' in found.html
    assert "Show its sibling" not in found.html


@pytest.mark.parametrize(
    ("count", "note"), [(3, "1 more type isn't shown."), (1_002, "1,000 more types aren't shown.")]
)
def test_individual_types_left_out(app: FastAPI, monkeypatch: pytest.MonkeyPatch, count: int, note: str) -> None:
    """Check that an individual's tree says how many of its types it leaves out (`MAX_ANCESTORS`)."""
    with_types(monkeypatch, "Left", "Right", count=count)
    with loaded(app, individual_stage()) as client:
        found = column(client, "/ind")
    assert f'<p class="note">{note.replace("'", "&#39;")}</p>' in found.html


def test_individual_types_left_out_none_shown(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that if none of the types read is in the class hierarchy but more were left out, the note says so.

    One left out may be a named class, so it doesn't say "No named type".
    """
    with_types(monkeypatch, "Nowhere", count=5)
    with loaded(app, individual_stage()) as client:
        found = column(client, "/ind")
    assert shown(found) == [(0, IND)]
    assert re.findall(r'<p class="note">([^<]*)</p>', found.html) == ["4 more types aren&#39;t shown."]


def test_individual_ancestors_capped(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that past `MAX_ANCESTORS`, an individual's tree keeps the ancestors nearest its types."""
    monkeypatch.setattr(browl.hierarchy_column, "MAX_ANCESTORS", 3)
    with_types(monkeypatch, "Deep")
    with loaded(app, individual_stage()) as client:
        assert shown(column(client, "/ind")) == [(0, "Left"), (1, "Bottom"), (2, "Deep"), (3, IND)]


def test_individual_punned(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that an IRI that's a class and an individual has both trees, side by side, each named."""
    with_types(monkeypatch, "Left")
    with loaded(app, individual_stage(EntityType.CLASS)) as client:
        found = column(client, "/ind")
    assert found.headings == [
        ("Local hierarchy: Classes", "/_browl/tree/classes"),
        ("Local hierarchy: Named individual", "/_browl/tree/classes"),
    ]
    assert [shown(found, index) for index in (0, 1)] == [
        [(0, "Top"), (1, "Right"), (2, "ind"), (2, "Show its sibling (1)")],
        [(0, "Top"), (1, "Left"), (2, IND)],  # no siblings; the class tree's ind is unmarked
    ]
    assert [Tree(tree).items[0].id for tree in found.trees] == ["hc-0", "hi-0"]


def test_individual_types_fail(app: FastAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Check that if an individual's types can't be read, its page is served, with no tree for them, and it's logged."""

    async def failed(*args: object) -> IndividualTypes:
        raise SparqlError("Error 500")

    monkeypatch.setattr(browl.lookup, "fetch_types", failed)
    with loaded(app, individual_stage(EntityType.CLASS)) as client, caplog.at_level(logging.ERROR, "browl.lookup"):
        found = column(client, "/ind")
    assert found.headings == [("Local hierarchy", "/_browl/tree/classes")]  # the class's, alone
    [record] = [record for record in caplog.records if record.name == "browl.lookup"]
    assert record.getMessage() == f"Couldn't read the types of {ex('ind')} for its hierarchy column"
    assert record.exc_info is not None


def test_individual_types_concurrent(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that an individual's types are read while its tab is, not after."""
    started = asyncio.Event()
    read_overview = browl.lookup.read_overview

    async def fetched(*args: object) -> IndividualTypes:
        started.set()
        return IndividualTypes((ex("Left"),), 1)

    async def overview(*args: Any, **kwargs: Any) -> object:
        async with asyncio.timeout(5):  # read after the tab, the types would never start
            await started.wait()
        return await read_overview(*args, **kwargs)

    monkeypatch.setattr(browl.lookup, "fetch_types", fetched)
    monkeypatch.setattr(browl.lookup, "read_overview", overview)
    with loaded(app, individual_stage()) as client:
        assert shown(column(client, "/ind")) == [(0, "Top"), (1, "Left"), (2, IND)]


def test_individual_types_timeout(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Check that a timeout reading an individual's types is logged without a traceback, unlike other errors."""

    async def failed(*args: object) -> IndividualTypes:
        raise SparqlTimeoutError("timed out after 30 s")

    monkeypatch.setattr(browl.lookup, "fetch_types", failed)
    with loaded(app, individual_stage()) as client, caplog.at_level(logging.ERROR, "browl.lookup"):
        assert column(client, "/ind").trees == []
    [record] = [record for record in caplog.records if record.name == "browl.lookup"]
    assert (
        record.getMessage() == f"Couldn't read the types of {ex('ind')} for its hierarchy column: timed out after 30 s"
    )
    assert record.exc_info is None


@pytest.mark.anyio
@pytest.mark.parametrize(("limit", "expected"), [(300, ("A", "B", "C")), (2, ("A", "B"))])
async def test_fetch_types(
    fuseki_url: str, monkeypatch: pytest.MonkeyPatch, limit: int, expected: tuple[str, ...]
) -> None:
    """Check that an individual's named types are read by IRI, at most `MAX_ANCESTORS`, with how many there are.

    OWL's types and class expressions aren't named types.
    """
    monkeypatch.setattr(browl.hierarchy_column, "MAX_ANCESTORS", limit)
    rdf_type = f"<{RDF}type>"
    triples = [f"<{ex('ind')}> {rdf_type} <{ex(name)}> ." for name in "CAB"]
    triples += [f"<{ex('ind')}> {rdf_type} <{OWL}{name}> ." for name in ("NamedIndividual", "Thing")]
    triples += [f"<{ex('ind')}> {rdf_type} _:b .", f"_:b {rdf_type} <{OWL}Restriction> ."]
    with fuseki_dataset(fuseki_url, {None: "\n".join(triples).encode()}) as dataset:
        async with httpx2.AsyncClient(base_url=fuseki_url) as http:
            types = await fetch_types(FusekiClient(http), dataset, ex("ind"))
    assert types == IndividualTypes(tuple(ex(name) for name in expected), 3)


@pytest.mark.anyio
async def test_fetch_types_union(fuseki_url: str) -> None:
    """Check that with the imports' dataset clause, a type stated in two graphs is read and counted once."""
    a_type, b_type = (f"<{ex('ind')}> <{RDF}type> <{ex(name)}> .\n" for name in "AB")
    graphs = {None: a_type, "http://example.org/import": a_type + b_type, "http://example.org/other": a_type}
    triples: dict[str | None, bytes] = {graph: text.encode() for graph, text in graphs.items()}
    with fuseki_dataset(fuseki_url, triples) as dataset:
        async with httpx2.AsyncClient(base_url=fuseki_url) as http:
            types = await fetch_types(FusekiClient(http), dataset, ex("ind"), UNION_FROM)
    assert types == IndividualTypes((ex("A"), ex("B")), 2)


@pytest.mark.anyio
@pytest.mark.parametrize("found", [None, Literal("A")], ids=["unbound", "literal"])
async def test_fetch_types_malformed(found: Literal | None) -> None:
    """Check that a row of the types query with no type is an error."""

    class Sparql:
        async def select(self, *args: object) -> SelectResult:
            return SelectResult(("type",), [(IRI(ex("A")),), (found,)])

    with pytest.raises(SparqlError, match="no type"):
        await fetch_types(Sparql(), "dataset", ex("ind"))  # type: ignore[arg-type]
