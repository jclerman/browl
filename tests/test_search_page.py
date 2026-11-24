"""Tests for the search route (P-20, U-10, DESIGN.md §2.13): the autocomplete fragment and the full results page."""

import html.parser
import re
import urllib.parse
from collections.abc import Iterator
from typing import NamedTuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from browl import search, search_page
from browl.derived import EntityType, NotCurrentError
from browl.pages import MAX_NAME_CHARS
from browl.search import MAX_OFFSET, MAX_QUERY_CHARS
from browl.search_page import PAGE_SIZE, Marked, _split, marked_length, value_offset
from tests.helpers import derived_stage, make_app
from tests.test_trees import loaded

pytestmark = pytest.mark.usefixtures("data_dir")

EX = "http://example.org/"
LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
SYNONYM = "http://www.geneontology.org/formats/oboInOwl#hasExactSynonym"
XREF = "http://www.geneontology.org/formats/oboInOwl#hasDbXref"
DEFINITION = "http://purl.obolibrary.org/obo/IAO_0000115"
SUB_LABEL = f"{EX}subLabel"
SOURCE = f"{EX}source"
SEARCH = "/_browl/search"
MANY = PAGE_SIZE + 5
"""How many classes named "item …" there are: more than a page."""
LONG = "zebra " + "z" * MAX_NAME_CHARS
"""A label longer than pages show."""


def ex(name: str) -> str:
    """Return the IRI of a name in `EX`."""
    return f"{EX}{name}"


def search_stage() -> object:
    """Return a stage with a few named entities, synonyms, an entailed label, a punned IRI, `MANY` items, and prose."""
    entities = [
        (ex("heart"), EntityType.CLASS),
        (ex("myoblast"), EntityType.CLASS),
        (ex("hasPart"), EntityType.OBJECT_PROPERTY),
        (ex("pun"), EntityType.CLASS),
        (ex("pun"), EntityType.ANNOTATION_PROPERTY),
        (ex("other"), EntityType.CLASS),
        (ex("long"), EntityType.CLASS),
        (SYNONYM, EntityType.ANNOTATION_PROPERTY),
        (SUB_LABEL, EntityType.ANNOTATION_PROPERTY),
        (XREF, EntityType.ANNOTATION_PROPERTY),
        (SOURCE, EntityType.ANNOTATION_PROPERTY),
    ]
    entities += [(ex(f"item{n}"), EntityType.CLASS) for n in range(MANY)]
    values: list[tuple[str, str, str, str | None]] = [
        (ex("heart"), LABEL, "heart muscle", "en"),
        (ex("heart"), SYNONYM, "myocardium", "en"),
        (ex("heart"), SYNONYM, "cardiac muscle tissue", "en"),
        (ex("heart"), XREF, "FMA:7088", None),
        (ex("heart"), DEFINITION, "A hollow organ that pumps blood through the circulatory system.", "en"),
        (ex("myoblast"), LABEL, "Myoblast", "en"),
        (ex("hasPart"), LABEL, "has part", "en"),
        (ex("pun"), LABEL, "punned", "en"),
        (ex("other"), SUB_LABEL, "other thing", "en"),
        (ex("long"), LABEL, LONG, "en"),
        (SYNONYM, LABEL, "has exact synonym", "en"),
        (SUB_LABEL, LABEL, "sub-label", "en"),
        (XREF, LABEL, "database cross reference", "en"),
    ]
    values += [(ex(f"item{n}"), LABEL, f"item {n:03}", "en") for n in range(MANY)]
    values += [(ex(f"item{n}"), SOURCE, "RadLex", None) for n in range(3)]  # repeated, so not searched by default
    values.append((SOURCE, LABEL, "source", "en"))
    names: dict[str, tuple[str | None, str | None]] = {
        ex("heart"): ("heart muscle", "ex:heart"),
        ex("myoblast"): ("Myoblast", None),
        ex("hasPart"): ("has part", None),
        ex("pun"): ("punned", None),
        ex("other"): ("other thing", None),
        ex("long"): (LONG, None),
        SYNONYM: ("has exact synonym", None),
        SUB_LABEL: ("sub-label", None),
        XREF: ("database cross reference", None),
        SOURCE: ("source", None),
    }
    names |= {ex(f"item{n}"): (f"item {n:03}", None) for n in range(MANY)}
    return derived_stage(entities, names=names, annotation_values=values, sub_properties=[(SUB_LABEL, LABEL)])


@pytest.fixture
def app() -> FastAPI:
    """Return an app that searches labels, then exact synonyms."""
    return make_app(NAME_PROPERTIES=["rdfs:label", SYNONYM])


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Yield a test client for the app, with `search_stage` loaded."""
    with loaded(app, search_stage()) as test_client:
        yield test_client


class Hit(NamedTuple):
    """A hit as shown.

    Attributes:
        term: The matched value; None if it isn't shown, as it's the name.
        name: The entity's name.
        types: Its types.
        href: Its link.
        id: Its CURIE, else IRI; None if it isn't shown.
        property: The label of the property the value is from; None if it isn't shown.
    """

    term: str | None
    name: str
    types: str
    href: str
    id: str | None = None
    property: str | None = None


class Hits(html.parser.HTMLParser):
    """A page's or fragment's hits, and its other links by text.

    Attributes:
        hits: The hits, in order.
        marks: The text of each `<mark>`.
        links: Every link's text and href.
        options: The listbox options' ids.
    """

    def __init__(self, page: str) -> None:
        """Parse a page, or a fragment."""
        super().__init__()
        self.hits: list[Hit] = []
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
        for field in ("hit-term", "hit-name", "hit-types", "hit-id", "hit-property"):
            if field in classes:
                self._field = field
                self._fields[field] = ""
        if tag == "mark":
            self._mark = []

    def handle_endtag(self, tag: str) -> None:
        """End a field, a mark, a link, or a hit: an option (`li`), or a row of the results' table (`tr`)."""
        if tag in ("bdi", "span", "code", "td") and self._field is not None:
            self._field = None
        elif tag == "mark" and self._mark is not None:
            self.marks.append("".join(self._mark))
            self._mark = None
        elif tag == "a" and self._href is not None:
            self.links.append((" ".join("".join(self._link_text).split()), self._href))
            self._href = None
        elif tag in ("li", "tr") and self._fields:
            f = self._fields
            hit = Hit(
                f.get("hit-term"), f["hit-name"], f["hit-types"], f["href"], f.get("hit-id"), f.get("hit-property")
            )
            self.hits.append(hit)
            self._fields = {}

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
    """Check that a hit shows the matched value, then the entity's name if it's different, and its types, but no id."""
    hits = suggest(client, "myo").hits
    assert hits == [
        Hit(None, "Myoblast", "Class", "/myoblast"),
        Hit("myocardium", "heart muscle", "Class", "/heart"),
    ]


def test_suggestion_marks_match(client: TestClient) -> None:
    """Check that the query's match is marked, in the value shown, whatever its case."""
    page = suggest(client, "myo")
    assert page.marks == ["Myo", "myo"]


def test_suggestion_marks_word(client: TestClient) -> None:
    """Check that a match from a later word marks that word, in the whole value shown (U-19)."""
    page = suggest(client, "MUS")
    assert page.hits == [Hit(None, "heart muscle", "Class", "/heart")]
    assert page.marks == ["mus"]


def test_suggestion_word_after_whole(client: TestClient) -> None:
    """Check that matches from a value's start come before matches from a later word."""
    page = suggest(client, "p")
    assert [hit.name for hit in page.hits] == ["punned", "has part"]
    assert page.marks == ["p", "p"]


def test_suggestion_marks_word_in_term(client: TestClient) -> None:
    """Check that a word match in a value that isn't the name marks that word in the value."""
    page = suggest(client, "tiss")
    assert page.hits == [Hit("cardiac muscle tissue", "heart muscle", "Class", "/heart")]
    assert page.marks == ["tiss"]


def test_results_mark_word(client: TestClient) -> None:
    """Check that the results page marks a word match as the autocomplete does."""
    page = results(client, [("q", "mus")])
    assert [hit.name for hit in page.hits] == ["heart muscle"]
    assert page.marks == ["mus"]


def test_long_value_cut_short(client: TestClient) -> None:
    """Check that a long value is cut short, and that its mark stops before the "…", however long the query."""
    page = suggest(client, LONG)
    shown = LONG[: MAX_NAME_CHARS - 1]
    assert page.hits == [Hit(None, f"{shown}…", "Class", "/long")]
    assert page.marks == [shown]


def test_punned_one_hit(client: TestClient) -> None:
    """Check that a punned IRI is one hit, listing its types."""
    assert suggest(client, "pun").hits == [Hit(None, "punned", "Class, Annotation property", "/pun")]


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
    """Check that a query that matches nothing says so, in a disabled option, and still offers the full results page."""
    response = client.get(SEARCH, params={"q": "qqq", "suggest": "1"})
    page = Hits(response.text)
    assert page.hits == []
    assert page.options == ["search-option-none", "search-option-all"]
    assert 'id="search-option-none" class="search-none" aria-disabled="true"' in response.text


@pytest.mark.parametrize("text", ["", "   "])
def test_blank_fragment_empty(client: TestClient, text: str) -> None:
    """Check that a blank query's fragment is empty, which closes the list."""
    response = client.get(SEARCH, params={"q": text, "suggest": "1"})
    assert response.status_code == 200
    assert response.text.strip() == ""


def test_blank_query_not_searched(app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a blank query isn't searched for, and that its fragment reads nothing."""

    async def fail(*args: object) -> None:
        raise AssertionError("searched")

    monkeypatch.setattr(app.state.search, "search", fail)
    assert client.get(SEARCH, params={"q": " "}).status_code == 200
    monkeypatch.setattr(search_page, "read_derived", fail)
    assert client.get(SEARCH, params={"q": " ", "suggest": "1"}).text.strip() == ""


def test_fragment_reads_no_ids(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the autocomplete doesn't read the hits' CURIEs, which it doesn't show."""
    monkeypatch.setattr(search_page, "_id_texts", lambda *args: pytest.fail("read the ids"))
    assert len(suggest(client, "myo").hits) == 2


def test_blank_page_shows_form(client: TestClient) -> None:
    """Check that the results page with no query shows its filters, and the results' panel with a prompt, no hits."""
    response = client.get(SEARCH)
    assert response.status_code == 200
    assert 'name="property"' in response.text
    assert '<h2 id="results-heading">Results</h2>' in response.text
    assert "Type what to search for in the search box" in response.text
    assert Hits(response.text).hits == []
    assert "No matches." not in response.text


def test_page_shows_hits_with_ids(client: TestClient) -> None:
    """Check that the results page shows the hits as the autocomplete does, each with its CURIE, else IRI, and property.

    A property without a display name is labelled as `NAME_PROPERTIES` names it.
    """
    assert results(client, [("q", "myo")]).hits == [
        Hit(None, "Myoblast", "Class", "/myoblast", f"{EX}myoblast", "rdfs:label"),
        Hit("myocardium", "heart muscle", "Class", "/heart", "ex:heart", "has exact synonym"),
    ]


def test_type_filter(client: TestClient) -> None:
    """Check that the type filter keeps hits of the types checked, and that none checked means all (U-10)."""
    assert [hit.name for hit in results(client, [("q", "has")]).hits] == ["has part", "has exact synonym"]
    only = results(client, [("q", "has"), ("type", "object_property")]).hits
    assert [hit.name for hit in only] == ["has part"]


def test_property_filter(client: TestClient) -> None:
    """Check that the property filter searches only the name properties checked (U-10)."""
    assert [hit.name for hit in results(client, [("q", "myo"), ("property", LABEL)]).hits] == ["Myoblast"]
    assert [hit.name for hit in results(client, [("q", "myo"), ("property", SYNONYM)]).hits] == ["heart muscle"]


def test_entailed_filter(client: TestClient) -> None:
    """Check that "Direct only" leaves out values entailed through a sub-property (U-10)."""
    assert [hit.name for hit in results(client, [("q", "oth"), ("property", LABEL)]).hits] == ["other thing"]
    assert results(client, [("q", "oth"), ("property", LABEL), ("values", "direct")]).hits == []


def test_property_choices(client: TestClient) -> None:
    """Check that the page offers the name-like properties with values, name properties first, with their counts.

    Then those searched by default, marked, then the others, each by display name; prose isn't offered (U-47). A
    property's count says how many values its sub-properties add.
    """
    text = client.get(SEARCH, params={"q": "a"}).text
    offered = re.findall(r'name="property" value="([^"]+)"', text)
    assert offered == [LABEL, SYNONYM, XREF, SUB_LABEL, SOURCE]
    shown = re.findall(r"<bdi>([^<]+)</bdi>( \*)?\s*<small class=\"note\">\(([^)]*)\)</small>", text)
    assert shown == [
        ("rdfs:label", " *", f"{MANY + 10:,}, and 1 in sub-properties"),  # the punned IRI's label counts per type
        ("has exact synonym", " *", "2"),
        ("database cross reference", " *", "1"),  # its values are on one entity each
        ("sub-label", " *", "1"),
        ("source", "", "3"),  # its value is on 3
    ]


def test_default_properties(client: TestClient) -> None:
    """Check that a search with no property checked covers the names, IDs and synonyms, not repeated values (U-47)."""
    assert [hit.property for hit in results(client, [("q", "fma")]).hits] == ["database cross reference"]
    assert results(client, [("q", "radlex")]).hits == []
    assert suggest(client, "radlex").hits == []
    assert [hit.property for hit in results(client, [("q", "radlex"), ("property", SOURCE)]).hits] == ["source"] * 3


def test_prose_not_searchable(client: TestClient) -> None:
    """Check that a prose property's values aren't searched, and it can't be chosen (until P-46)."""
    assert results(client, [("q", "hollow")]).hits == []
    assert client.get(SEARCH, params=[("q", "hollow"), ("property", DEFINITION)]).status_code == 404


class Checked(html.parser.HTMLParser):
    """A form's checked inputs, as (name, value).

    Attributes:
        checked: The checked inputs, in order.
    """

    def __init__(self, page: str) -> None:
        """Parse a page."""
        super().__init__()
        self.checked: list[tuple[str | None, str | None]] = []
        self.feed(page)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Note a checked input."""
        at = dict(attrs)
        if tag == "input" and "checked" in at:
            self.checked.append((at.get("name"), at.get("value")))


def test_filters_shown_checked(client: TestClient) -> None:
    """Check that the form shows the filters the URL asks for, each once, and labels name properties as it should."""
    params = [("q", "a"), ("type", "class"), ("type", "object_property"), ("type", "class")]
    params += [("property", SYNONYM), ("property", LABEL), ("values", "direct")]
    text = client.get(SEARCH, params=params).text
    assert Checked(text).checked == [
        ("type", "class"),
        ("type", "object_property"),
        ("property", LABEL),
        ("property", SYNONYM),
        ("values", "direct"),
    ]
    assert "<bdi>has exact synonym</bdi>" in text  # the synonym property's display name
    assert "<bdi>rdfs:label</bdi>" in text  # undeclared: as set


def test_several_filters(client: TestClient) -> None:
    """Check that several types, or several name properties, checked find hits of any of them (U-10)."""
    both = [("type", "class"), ("type", "object_property")]
    assert [hit.name for hit in results(client, [("q", "has"), *both]).hits] == ["has part"]
    properties = [("property", LABEL), ("property", SYNONYM), ("property", LABEL)]
    assert [hit.name for hit in results(client, [("q", "myo"), *properties]).hits] == ["Myoblast", "heart muscle"]


def test_property_labelled_as_set() -> None:
    """Check that a name property without a display name is labelled as `NAME_PROPERTIES` names it.

    Its IRI is in two prefixes' namespaces, so a CURIE made from them could differ from the setting's.
    """
    app = make_app(NAME_PROPERTIES=["rdfs:label", "exa:name"], EXTRA_PREFIXES={"ex": EX, "exa": f"{EX}a/"})
    stage = derived_stage([(ex("a"), EntityType.CLASS)], annotation_values=[(ex("a"), f"{EX}a/name", "a", None)])
    with loaded(app, stage) as client:
        text = client.get(SEARCH, params={"q": "a"}).text
    assert "<bdi>exa:name</bdi>" in text
    assert "ex:a/name" not in text


def test_paging(client: TestClient) -> None:
    """Check that the results page pages through the hits, keeping the query and filters in its links."""
    first = results(client, [("q", "item"), ("type", "class"), ("values", "direct")])
    assert len(first.hits) == PAGE_SIZE
    links = dict(first.links)
    assert "Previous" not in links
    params = urllib.parse.parse_qsl(urllib.parse.urlsplit(links["Next"]).query)
    assert params == [("q", "item"), ("type", "class"), ("values", "direct"), ("offset", str(PAGE_SIZE))]
    response = client.get(SEARCH, params=params)
    assert f"Showing {PAGE_SIZE + 1}–{MANY}" in response.text
    second = Hits(response.text)
    assert [hit.name for hit in second.hits] == [f"item {n:03}" for n in range(PAGE_SIZE, MANY)]
    assert "Next" not in dict(second.links)
    previous = urllib.parse.parse_qsl(urllib.parse.urlsplit(dict(second.links)["Previous"]).query)
    assert previous == [("q", "item"), ("type", "class"), ("values", "direct")]


@pytest.mark.parametrize(
    ("text", "offset", "note"), [("qqq", None, "No matches."), ("item", MAX_OFFSET, "No more results.")]
)
def test_no_results_note(client: TestClient, text: str, offset: int | None, note: str) -> None:
    """Check that a page with no results says there are none, or past the first page, no more."""
    params = [("q", text)] + ([("offset", str(offset))] if offset else [])
    response = client.get(SEARCH, params=params)
    assert Hits(response.text).hits == []
    assert f'<p class="note">{note}</p>' in response.text


def test_filters_in_fixed_order(client: TestClient) -> None:
    """Check that the paging links give the filters in a fixed order, whatever the URL's, so a query has one URL."""
    params = [("q", "item"), ("property", SYNONYM), ("property", LABEL), ("type", "object_property"), ("type", "class")]
    links = dict(results(client, params).links)
    assert urllib.parse.parse_qsl(urllib.parse.urlsplit(links["Next"]).query) == [
        ("q", "item"),
        ("type", "class"),
        ("type", "object_property"),
        ("property", LABEL),
        ("property", SYNONYM),
        ("offset", str(PAGE_SIZE)),
    ]


def test_previous_from_between_pages(client: TestClient) -> None:
    """Check that Previous from an offset under a page goes to the first page, which has no offset."""
    links = dict(results(client, [("q", "item"), ("offset", "10")]).links)
    assert urllib.parse.parse_qsl(urllib.parse.urlsplit(links["Previous"]).query) == [("q", "item")]


def test_paging_stops_at_max_offset(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that `MAX_OFFSET` is a valid offset, and that no Next link goes past it, though there are more results."""
    monkeypatch.setattr(search_page, "MAX_OFFSET", PAGE_SIZE - 1)
    assert "Next" not in dict(results(client, [("q", "item")]).links)
    assert len(results(client, [("q", "item"), ("offset", str(PAGE_SIZE - 1))]).hits) == MANY - PAGE_SIZE + 1
    assert client.get(SEARCH, params=[("q", "item"), ("offset", str(PAGE_SIZE))]).status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        [("q", "a"), ("q", "b")],
        [("q", "a"), ("values", "all"), ("values", "all")],
        [("q", "a"), ("offset", "50"), ("offset", "50")],
        [("q", "a"), ("suggest", "1"), ("suggest", "1")],
        [("q", "a"), ("type", "individual")],
        [("q", "a"), ("property", f"{EX}notAName")],
        [("q", "a"), ("values", "some")],
        [("q", "a"), ("offset", "0")],
        [("q", "a"), ("offset", "01")],
        [("q", "a"), ("offset", "-5")],
        [("q", "a"), ("offset", "+5")],
        [("q", "a"), ("offset", "\u0665\u0660")],  # 50 in Arabic-Indic digits
        [("q", "a"), ("offset", " 5")],
        [("q", "a"), ("offset", str(MAX_OFFSET + 1))],
        [("q", "a"), ("offset", "1" * 5_000)],  # past int()'s digit limit
        [("q", "a"), ("suggest", "yes")],
        [("q", "a"), ("suggest", "1"), ("offset", "50")],
        [("q", "a" * (MAX_QUERY_CHARS + 1))],
    ],
)
def test_not_found(client: TestClient, params: list[tuple[str, str]]) -> None:
    """Check that a repeated or invalid parameter gets the "not found" page."""
    assert client.get(SEARCH, params=params).status_code == 404


def test_not_current(app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a search while the derived database isn't current gets the "loading" page."""

    async def not_current(*args: object) -> None:
        raise NotCurrentError("rebuilding")

    monkeypatch.setattr(app.state.search, "search", not_current)
    assert client.get(SEARCH, params={"q": "myo"}).status_code == 503


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
        assert [hit.href for hit in page.hits] == ["/root/myoblast", "/root/heart"]
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


@pytest.mark.parametrize(
    ("value", "offset", "start"),
    [
        ("T-cell", 2, 2),
        ("  T-cell", 2, 4),  # normalizing drops leading whitespace
        ("Ångström unit", 9, 9),
        ("A\u030angstro\u0308m unit", 9, 11),  # combining accents, which normalizing drops
        ("Straße unit", 7, 6),  # ß folds to ss
        ("x", 0, 0),
        ("  T-cell", 0, 2),
        ("½ cup", 2, 0),  # in a character normalizing expands: it starts there
    ],
)
def test_value_offset(value: str, offset: int, start: int) -> None:
    """Check where an offset in the normalized value falls in the value."""
    assert value_offset(value, offset) == start


@pytest.mark.parametrize(
    ("text", "query", "word_start", "split"),
    [
        ("T-cell", "cell", 2, ("T-", "cell", "")),
        ("  Heart", "he", 0, ("  ", "He", "art")),
        ("½ cup", "2", 2, ("", "½", " cup")),  # the character holding the word is marked
        ("Dose ⑴ low", "1", 6, ("Dose ", "⑴", " low")),
        ("x " * 60 + "target", "targ", 120, ("…", "targ", "et")),  # past the cut: shown from the word on
        ("x " * 45 + "target thing", "target", 90, ("x " * 45, "target", " th…")),  # shown, then cut
        ("x " * 2_000 + "target", "targ", 4_000, ("", "", f"{'x ' * 49}x…")),  # past `_WORD_SCAN`: unmarked
    ],
)
def test_split(text: str, query: str, word_start: int, split: tuple[str, str, str]) -> None:
    """Check how a hit's text is split around its match, from the match's normalized offset."""
    assert _split(text, query, word_start) == Marked(*split)


def test_common_value_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the results page counts a common value in a note above the hits, rather than listing its entities."""
    monkeypatch.setattr(search, "COMMON_MIN", 5)
    entities = [(ex(f"c{n}"), EntityType.CLASS) for n in range(20)]
    values = [(ex(f"c{n}"), LABEL, f"class {n}", None) for n in range(20)]
    values += [(ex(f"c{n}"), XREF, "GC_ID:1", None) for n in range(15)]
    values.append((ex("c19"), XREF, "GC_ID:11", None))
    with loaded(make_app(), derived_stage(entities, annotation_values=values)) as client:
        response = client.get(SEARCH, params=[("q", "gc_id"), ("property", XREF)])
        default = client.get(SEARCH, params=[("q", "gc_id")])
    assert response.status_code == 200
    notes = re.findall(r'<ul class="common-values">(.*?)</ul>', response.text, re.DOTALL)
    assert [" ".join(re.sub("<[^>]+>", "", note).split()) for note in notes] == [
        f"GC_ID:1 ({XREF}) is on 15 classes (75%): too many to list."  # undeclared, so labelled by IRI
    ]
    assert [hit.term or hit.name for hit in Hits(response.text).hits] == ["GC_ID:11"]
    assert "common-values" not in default.text
    assert "No matches." in default.text


def test_page_filters_belong_to_the_bar(client: TestClient) -> None:
    """Check that the search page's one box is the bar's, holding its query, and that its filters belong to its form."""
    text = client.get(SEARCH, params=[("q", "myo"), ("property", LABEL)]).text
    assert 'id="search-text"' not in text
    assert re.search(r'<input id="site-search-box"[^>]*value="myo"', text)
    inputs = re.findall(r'<input type="(?:checkbox|radio)" name="(\w+)"[^>]*>', text)
    assert inputs and all(
        'form="site-search"' in tag for tag in re.findall(r'<input type="(?:checkbox|radio)"[^>]*>', text)
    )
    assert '<button type="submit" form="site-search">Search</button>' in text
