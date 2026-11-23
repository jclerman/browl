"""Tests for the search route (P-20, U-10, DESIGN.md §2.13): the autocomplete fragment and the full results page."""

import html.parser
import urllib.parse
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from browl.derived import EntityType
from browl.search import MAX_OFFSET, MAX_QUERY_CHARS
from browl.search_page import PAGE_SIZE, marked_length
from tests.helpers import derived_stage, make_app
from tests.test_trees import loaded

pytestmark = pytest.mark.usefixtures("data_dir")

EX = "http://example.org/"
LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
SYNONYM = "http://www.geneontology.org/formats/oboInOwl#hasExactSynonym"
SEARCH = "/_browl/search"
MANY = PAGE_SIZE + 5
"""How many classes named "item …" there are: more than a page."""


def ex(name: str) -> str:
    """Return the IRI of a name in `EX`."""
    return f"{EX}{name}"


def search_stage() -> object:
    """Return a stage with a few named entities, a synonym, an entailed label, a punned IRI, and `MANY` items."""
    entities = [
        (ex("heart"), EntityType.CLASS),
        (ex("myoblast"), EntityType.CLASS),
        (ex("hasPart"), EntityType.OBJECT_PROPERTY),
        (ex("pun"), EntityType.CLASS),
        (ex("pun"), EntityType.ANNOTATION_PROPERTY),
        (ex("other"), EntityType.CLASS),
        (SYNONYM, EntityType.ANNOTATION_PROPERTY),
    ]
    entities += [(ex(f"item{n}"), EntityType.CLASS) for n in range(MANY)]
    values: list[tuple[str, str, str, str | None, bool]] = [
        (ex("heart"), LABEL, "heart muscle", "en", True),
        (ex("heart"), SYNONYM, "myocardium", "en", True),
        (ex("myoblast"), LABEL, "Myoblast", "en", True),
        (ex("hasPart"), LABEL, "has part", "en", True),
        (ex("pun"), LABEL, "punned", "en", True),
        (ex("other"), LABEL, "other thing", "en", False),
        (SYNONYM, LABEL, "has exact synonym", "en", True),
    ]
    values += [(ex(f"item{n}"), LABEL, f"item {n:03}", "en", True) for n in range(MANY)]
    names: dict[str, tuple[str | None, str | None]] = {
        ex("heart"): ("heart muscle", "ex:heart"),
        ex("myoblast"): ("Myoblast", None),
        ex("hasPart"): ("has part", None),
        ex("pun"): ("punned", None),
        ex("other"): ("other thing", None),
        SYNONYM: ("has exact synonym", None),
    }
    names |= {ex(f"item{n}"): (f"item {n:03}", None) for n in range(MANY)}
    return derived_stage(entities, names=names, name_values=values)


@pytest.fixture
def app() -> FastAPI:
    """Return an app that searches labels, then exact synonyms."""
    return make_app(NAME_PROPERTIES=["rdfs:label", SYNONYM])


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Yield a test client for the app, with `search_stage` loaded."""
    with loaded(app, search_stage()) as test_client:
        yield test_client


class Hits(html.parser.HTMLParser):
    """A page's or fragment's hits, each as (term, name, types, link), and its other links by text.

    Attributes:
        hits: The hits, in order: the matched value, None if it isn't shown, the name, the types and the link.
        marks: The text of each `<mark>`.
        links: Every link's text and href.
        options: The listbox options' ids.
    """

    def __init__(self, page: str) -> None:
        """Parse a page, or a fragment."""
        super().__init__()
        self.hits: list[tuple[str | None, str, str, str]] = []
        self.marks: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.options: list[str] = []
        self._field: str | None = None
        self._fields: dict[str, str] = {}
        self._href: str | None = None
        self._link_text: list[str] = []
        self._mark: list[str] | None = None
        self.feed(page)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Start a hit, one of its fields, a mark or a link."""
        at = dict(attrs)
        classes = (at.get("class") or "").split()
        if at.get("role") == "option":
            self.options.append(at.get("id") or "")
        if tag == "a":
            self._href = at.get("href") or ""
            self._link_text = []
            if "hit" in classes:
                self._fields = {"href": self._href}
        for field in ("hit-term", "hit-name", "hit-types"):
            if field in classes:
                self._field = field
                self._fields[field] = ""
        if tag == "mark":
            self._mark = []

    def handle_endtag(self, tag: str) -> None:
        """End a field, a mark, or a link and its hit."""
        if tag in ("bdi", "span") and self._field is not None:
            self._field = None
        elif tag == "mark" and self._mark is not None:
            self.marks.append("".join(self._mark))
            self._mark = None
        elif tag == "a" and self._href is not None:
            self.links.append((" ".join("".join(self._link_text).split()), self._href))
            if self._fields:
                f = self._fields
                self.hits.append((f.get("hit-term"), f["hit-name"], f["hit-types"], f["href"]))
                self._fields = {}
            self._href = None

    def handle_data(self, data: str) -> None:
        """Collect a field's, mark's and link's text."""
        if self._field is not None:
            self._fields[self._field] += data
        if self._mark is not None:
            self._mark.append(data)
        if self._href is not None:
            self._link_text.append(data)


def suggest(client: TestClient, text: str) -> Hits:
    """Return the autocomplete fragment's hits for a query, checking that it's served."""
    response = client.get(SEARCH, params={"q": text, "suggest": "1"})
    assert response.status_code == 200, response.text
    assert response.headers["x-robots-tag"] == "noindex"
    return Hits(response.text)


def results(client: TestClient, params: list[tuple[str, str]]) -> Hits:
    """Return the full results page's hits, checking that it's served."""
    response = client.get(SEARCH, params=params)
    assert response.status_code == 200, response.text
    assert '<meta name="robots" content="noindex">' in response.text
    return Hits(response.text)


def test_suggestions_show_term_then_name(client: TestClient) -> None:
    """Check that a hit shows the matched value, then the entity's name if it's different, and its types."""
    hits = suggest(client, "myo").hits
    assert hits == [
        (None, "Myoblast", "Class", "/myoblast"),
        ("myocardium", "heart muscle", "Class", "/heart"),
    ]


def test_suggestion_marks_match(client: TestClient) -> None:
    """Check that the query's match is marked, in the value shown, whatever its case."""
    page = suggest(client, "myo")
    assert page.marks == ["Myo", "myo"]


def test_punned_one_hit(client: TestClient) -> None:
    """Check that a punned IRI is one hit, listing its types."""
    assert suggest(client, "pun").hits == [(None, "punned", "Class, Annotation property", "/pun")]


def test_suggestions_end_with_all_results(client: TestClient) -> None:
    """Check that the options end with "All results…", which opens the full results page for the query."""
    page = suggest(client, "he")
    assert page.options[-1] == "search-option-all"
    text, link = page.links[-1]
    assert text == "All results…"
    assert urllib.parse.parse_qsl(urllib.parse.urlsplit(link).query) == [("q", "he")]


def test_suggestions_limited(client: TestClient) -> None:
    """Check that the autocomplete shows at most ten hits."""
    assert len(suggest(client, "item").hits) == 10


def test_no_matches(client: TestClient) -> None:
    """Check that a query that matches nothing says so, and still offers the full results page."""
    page = suggest(client, "zzz")
    assert page.hits == []
    assert page.options == ["search-option-all"]


@pytest.mark.parametrize("text", ["", "   "])
def test_blank_fragment_empty(client: TestClient, text: str) -> None:
    """Check that a blank query's fragment is empty, which closes the list."""
    response = client.get(SEARCH, params={"q": text, "suggest": "1"})
    assert response.status_code == 200
    assert response.text.strip() == ""


def test_blank_page_shows_form(client: TestClient) -> None:
    """Check that the results page with no query shows its form, and no results."""
    response = client.get(SEARCH)
    assert response.status_code == 200
    assert 'name="q"' in response.text
    assert "Results" not in response.text


def test_page_shows_hits_with_ids(client: TestClient) -> None:
    """Check that the results page shows the hits as the autocomplete does, each with its CURIE, else IRI."""
    response = client.get(SEARCH, params={"q": "he"})
    page = Hits(response.text)
    assert page.hits == [
        (None, "heart muscle", "Class", "/heart"),
    ]
    assert "ex:heart" in response.text


def test_type_filter(client: TestClient) -> None:
    """Check that the type filter keeps hits of the types checked, and that none checked means all (U-10)."""
    assert [hit[1] for hit in results(client, [("q", "has")]).hits] == ["has part", "has exact synonym"]
    only = results(client, [("q", "has"), ("type", "object_property")]).hits
    assert [hit[1] for hit in only] == ["has part"]


def test_property_filter(client: TestClient) -> None:
    """Check that the property filter searches only the name properties checked (U-10)."""
    assert [hit[1] for hit in results(client, [("q", "myo"), ("property", LABEL)]).hits] == ["Myoblast"]
    assert [hit[1] for hit in results(client, [("q", "myo"), ("property", SYNONYM)]).hits] == ["heart muscle"]


def test_entailed_filter(client: TestClient) -> None:
    """Check that "Direct only" leaves out values entailed through a sub-property (U-10)."""
    assert [hit[1] for hit in results(client, [("q", "oth")]).hits] == ["other thing"]
    assert results(client, [("q", "oth"), ("values", "direct")]).hits == []


def test_filters_shown_checked(client: TestClient) -> None:
    """Check that the form shows the filters the URL asks for, and names name properties by display name, else CURIE."""
    response = client.get(SEARCH, params=[("q", "a"), ("type", "class"), ("property", SYNONYM), ("values", "direct")])
    text = response.text
    assert 'value="class" checked' in text
    assert f'value="{SYNONYM}" checked' in text
    assert 'value="direct" checked' in text
    assert "has exact synonym" in text  # the synonym property's display name
    assert "rdfs:label" in text  # undeclared: its CURIE


def test_paging(client: TestClient) -> None:
    """Check that the results page pages through the hits, keeping the query and filters in its links."""
    first = results(client, [("q", "item"), ("type", "class")])
    assert len(first.hits) == PAGE_SIZE
    links = dict(first.links)
    assert "Previous" not in links
    params = urllib.parse.parse_qsl(urllib.parse.urlsplit(links["Next"]).query)
    assert params == [("q", "item"), ("type", "class"), ("offset", str(PAGE_SIZE))]
    second = results(client, params)
    assert [hit[1] for hit in second.hits] == [f"item {n:03}" for n in range(PAGE_SIZE, MANY)]
    assert "Next" not in dict(second.links)
    previous = urllib.parse.parse_qsl(urllib.parse.urlsplit(dict(second.links)["Previous"]).query)
    assert previous == [("q", "item"), ("type", "class")]


@pytest.mark.parametrize(
    "params",
    [
        [("q", "a"), ("q", "b")],
        [("q", "a"), ("type", "individual")],
        [("q", "a"), ("property", f"{EX}notAName")],
        [("q", "a"), ("values", "some")],
        [("q", "a"), ("offset", "0")],
        [("q", "a"), ("offset", "01")],
        [("q", "a"), ("offset", "-5")],
        [("q", "a"), ("offset", str(MAX_OFFSET + 1))],
        [("q", "a"), ("offset", "1" * 5_000)],  # past int()'s digit limit
        [("q", "a"), ("suggest", "yes")],
        [("q", "a" * (MAX_QUERY_CHARS + 1))],
    ],
)
def test_not_found(client: TestClient, params: list[tuple[str, str]]) -> None:
    """Check that a repeated or invalid parameter gets the "not found" page."""
    assert client.get(SEARCH, params=params).status_code == 404


def test_search_box_in_bar(client: TestClient) -> None:
    """Check that every page's navigation bar has the search box: a combobox that's also a plain form (U-17)."""
    text = client.get("/_browl/tree/classes").text
    assert f'action="{SEARCH}" method="get" role="search"' in text
    assert 'role="combobox"' in text
    assert f'hx-get="{SEARCH}?suggest=1"' in text


def test_under_root_path(app: FastAPI) -> None:
    """Check that links are under the app's root path."""
    with loaded(app, search_stage(), root_path="/root") as client:
        response = client.get(SEARCH, params={"q": "myo", "suggest": "1"})
        page = Hits(response.text)
        assert [hit[3] for hit in page.hits] == ["/root/myoblast", "/root/heart"]
        assert page.links[-1][1].startswith(f"/root{SEARCH}?")


@pytest.mark.parametrize(
    ("value", "query", "length"),
    [
        ("Heart", "he", 2),
        ("  heart", "he", 4),
        ("Ångström", "an", 2),  # Å is one character, NFKD two
        ("Ángstrom", "a", 2),  # a combining accent goes with its letter
        ("Straße", "strass", 5),  # ß folds to ss
        ("abc", "", 0),
    ],
)
def test_marked_length(value: str, query: str, length: int) -> None:
    """Check how much of a value is marked as matching a normalized query."""
    assert marked_length(value, query) == length
