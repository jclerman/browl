"""The search route, `search`: the autocomplete fragment and the full results page (U-10, U-17; DESIGN.md §2.9)."""

import bisect
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import NamedTuple

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import ValidationError

from browl.derived import TYPE_LABELS, EntityType, ready_generation
from browl.links import EntityLink, read_entity_links
from browl.names import name_properties, normalize
from browl.pager import PAGE_PARAM, make_pager, page_count, read_page
from browl.pages import (
    MAX_IRI_CHARS,
    PANEL_HEADERS,
    SEARCH_PATH,
    SUGGEST_PARAM,
    cut_name,
    cut_text,
    href,
    link_root,
    not_found,
    read_derived,
    single_params,
    templates,
)
from browl.search import (
    MAX_OFFSET,
    MAX_QUERY_CHARS,
    SEARCH_LIMIT,
    CommonValue,
    SearchBackend,
    SearchHit,
    SearchProperty,
    SearchQuery,
    SearchResults,
)
from browl.settings import Settings
from browl.tabs import with_query

QUERY_PARAM = "q"
"""The query parameter that gives the search's text."""

TYPE_PARAM = "type"
"""The query parameter, repeated, that names each entity type to find; none for all."""

PROPERTY_PARAM = "property"
"""The query parameter, repeated, that names each property, by IRI, whose values to search; none for the defaults."""

VALUES_PARAM = "values"
"""The query parameter that says which values count: `all`, the default, or `direct`, without entailed ones."""

PAGE_SIZE = 50
"""How many results the full results page shows at once."""

LAST_PAGE = MAX_OFFSET // PAGE_SIZE + 1
"""The deepest page of results the results page shows, as deep as a query may skip."""

COUNT_TO = MAX_OFFSET + PAGE_SIZE + 1
"""How many results the results page counts, at most: those of its deepest page, and one more, to tell whether there
are more than it shows. An exact count would read every match."""

_VALUES = ("all", "direct")
"""What `values=` may give."""

TYPE_PLURALS = {
    EntityType.ONTOLOGY: "ontologies",
    EntityType.CLASS: "classes",
    EntityType.OBJECT_PROPERTY: "object properties",
    EntityType.DATA_PROPERTY: "data properties",
    EntityType.ANNOTATION_PROPERTY: "annotation properties",
    EntityType.NAMED_INDIVIDUAL: "named individuals",
}
"""Each entity type's name for a count of them, as the results page's common values show it."""

router = APIRouter()
"""The search route, under the reserved prefix."""


@dataclass(frozen=True)
class SearchRequest:
    """What a search URL asks for.

    Attributes:
        text: The query's text, as given.
        types: The entity types to find, in `EntityType`'s order; empty for all.
        properties: The properties to search, by IRI, each once, sorted; empty for those searched by default. Once
            checked against the served ontology's (`_choices`), in their walk order
            (`browl.search.SearchBackend.properties`).
        direct: Whether only direct values count.
        page: The page of results it shows, from 1.
        suggest: Whether it asks for the autocomplete fragment.
    """

    text: str
    types: tuple[EntityType, ...]
    properties: tuple[str, ...]
    direct: bool
    page: int
    suggest: bool

    @property
    def offset(self) -> int:
        """Return how many results come before those shown."""
        return (self.page - 1) * PAGE_SIZE


class Marked(NamedTuple):
    """Text cut short (`cut_name`), split around its match of the query.

    Attributes:
        before: What comes before the match: empty unless the match is a later word's (`SearchHit.word_start`).
        marked: The match; empty if none is marked.
        rest: The rest.
    """

    before: str
    marked: str
    rest: str


@dataclass(frozen=True)
class ShownHit:
    """A search hit as the autocomplete and the results page show it.

    Attributes:
        link: The entity's link, with its name.
        term: The matched value; None if it's the name, which then shows the match.
        name: The entity's name, with the match marked if it's the matched value.
        types: Its types' labels, joined.
        id_text: Its CURIE, else its IRI cut short, which the results page shows; None in the autocomplete.
        property: The label of the property the value is from, which the results page shows; None in the
            autocomplete.
    """

    link: EntityLink
    term: Marked | None
    name: Marked
    types: str
    id_text: str | None
    property: str | None = None


@dataclass(frozen=True)
class PropertyChoice:
    """A property the results page lets the visitor search: a name-like one with values (DESIGN.md §2.6).

    Attributes:
        iri: Its IRI.
        label: Its display name, else its name in `NAME_PROPERTIES`, else its CURIE, else its IRI, cut short.
        values: How many values it has.
        sub_values: How many values its sub-properties have, which a search with entailed values reaches too.
        by_default: Whether a search that checks none searches it.
        checked: Whether the query names it.
    """

    iri: str
    label: str
    values: int
    sub_values: int
    by_default: bool
    checked: bool


def read_request(request: Request) -> SearchRequest | None:
    """Return what a search URL asks for; None if a parameter is repeated where it mustn't be, or isn't valid.

    The properties `property=` names are checked once the served ontology's are read (`_choices`).
    """
    params = request.query_params
    single = single_params(request, (QUERY_PARAM, VALUES_PARAM, SUGGEST_PARAM))
    if single is None:
        return None
    text, values, suggest = single[QUERY_PARAM] or "", single[VALUES_PARAM], single[SUGGEST_PARAM]
    if len(text) > MAX_QUERY_CHARS or (values is not None and values not in _VALUES) or suggest not in (None, "1"):
        return None
    page = read_page(request)
    if page is None or page > LAST_PAGE or (PAGE_PARAM in params and suggest):  # the fragment isn't paged
        return None
    try:
        kinds = {EntityType(kind) for kind in params.getlist(TYPE_PARAM)}
    except ValueError:
        return None
    # In a fixed order, each once, so a query's links, and the page cache's key (DESIGN.md §2.10), don't depend on
    # the URL's.
    types = tuple(kind for kind in EntityType if kind in kinds)
    named = tuple(sorted(set(params.getlist(PROPERTY_PARAM))))
    return SearchRequest(text, types, named, values == "direct", page, suggest == "1")


def _normalized_length(value: str, end: int) -> int:
    """Return the length of a value's first characters once normalized (`browl.names.normalize`)."""
    return len(normalize(value[:end]))


def marked_length(value: str, query: str) -> int:
    """Return how many of a value's first characters match a normalized query (`browl.names.normalize`), to mark.

    A value's characters and its normalized ones don't line up one to one (accents, case folding, leading
    whitespace), so this is the shortest start of the value that normalizes to the query, or past it, with any
    combining marks that follow.

    Examples:
        >>> marked_length("Ångström", "angs")
        4
        >>> marked_length("  Heart", "he")
        4
    """
    ends = range(len(value) + 1)
    end = min(bisect.bisect_left(ends, len(query), key=lambda end: _normalized_length(value, end)), len(value))
    while 0 < end < len(value) and unicodedata.combining(value[end]):
        end += 1
    return end


def value_offset(value: str, offset: int) -> int:
    """Return where in a value an offset in its normalized value (`browl.names.normalize`) falls, such as a word start.

    It's the longest start of the value that normalizes to at most that many characters, so a character that
    normalizing expands, holding the offset, is included.

    Examples:
        >>> value_offset("Ångström-unit", 9)
        9
        >>> value_offset("A\u030angstro\u0308m-unit", 9)
        11
        >>> value_offset("½ cup", 2)  # ½ normalizes to three characters
        0
    """
    ends = range(len(value) + 1)
    return bisect.bisect_right(ends, offset, key=lambda end: _normalized_length(value, end)) - 1


_WORD_SCAN = 3 * MAX_QUERY_CHARS
"""How many of a value's characters `_split` reads to find a word past what's shown, so a hit's cost is bounded."""


def _split(text: str, query: str, word_start: int) -> Marked:
    """Return a text cut short (`cut_name`), split around its match of the query, from a normalized offset.

    The match starts at `SearchHit.word_start` in the normalized text. A match in what's shown is marked there, up to
    any "…" the cut added. A match past it is shown from its word on, after "…", unless it's further in than
    `_WORD_SCAN` characters of the text, when nothing is marked.
    """
    shown = cut_name(text)
    head = text[: len(shown) if shown == text else len(shown) - 1]
    if word_start < _normalized_length(head, len(head)) or not word_start:
        start = value_offset(head, word_start)
        end = start + marked_length(head[start:], query)  # only what's shown: the query may be far longer
        return Marked(shown[:start], shown[start:end], shown[end:])
    scanned = text[:_WORD_SCAN]
    start = value_offset(scanned, word_start)
    if start == len(scanned):
        return Marked("", "", shown)
    rest = text[start:]
    tail = cut_name(rest)
    end = marked_length(rest[: len(tail) if tail == rest else len(tail) - 1], query)
    return Marked("…", tail[:end], tail[end:])


def _id_texts(connection: sqlite3.Connection, iris: Sequence[str]) -> dict[str, str]:
    """Return each IRI's CURIE, else the IRI cut short."""
    texts = {iri: cut_text(iri, MAX_IRI_CHARS) for iri in iris}
    if iris:
        placeholders = ", ".join("?" * len(iris))  # at most MAX_LIMIT
        rows = connection.execute(
            f"SELECT iri, curie FROM entity WHERE iri IN ({placeholders}) AND curie IS NOT NULL", list(iris)
        )
        for iri, curie in rows:
            texts[iri] = cut_text(curie, MAX_IRI_CHARS)
    return texts


def shown_hits(
    connection: sqlite3.Connection,
    hits: Sequence[SearchHit],
    query: str,
    prefix: str,
    root: str,
    with_ids: bool = False,
) -> list[ShownHit]:
    """Return how the hits are shown, from the derived database.

    Args:
        connection: The derived database.
        hits: The hits, best first.
        query: The query's text, normalized.
        prefix: The reserved prefix.
        root: The app's root path, for links (`browl.pages.link_root`).
        with_ids: Whether to read their CURIEs, which the results page shows, and the autocomplete doesn't.
    """
    iris = [hit.iri for hit in hits]
    links = read_entity_links(connection, iris, prefix, root)
    ids: dict[str, str] = _id_texts(connection, iris) if with_ids else {}
    shown = []
    for hit in hits:
        link = links[hit.iri]
        if cut_name(hit.value) == link.name:  # the name is the value that matched
            term, name = None, _split(hit.value, query, hit.word_start)
        else:
            term, name = _split(hit.value, query, hit.word_start), Marked("", "", link.name)
        types = ", ".join(TYPE_LABELS[kind] for kind in hit.types)
        shown.append(ShownHit(link, term, name, types, ids.get(hit.iri)))
    return shown


def property_label(found: SearchProperty, names: Mapping[str, str]) -> str:
    """Return how a property is labeled, cut short.

    Its display name, else its name in `NAME_PROPERTIES`, else its CURIE, else its IRI.

    Args:
        found: The property.
        names: The name properties, by IRI, each with its name in `NAME_PROPERTIES` (`name_properties`).
    """
    return cut_name(found.name or names.get(found.iri) or found.label)


def _choices(
    properties: Sequence[SearchProperty], names: Mapping[str, str], chosen: Sequence[str]
) -> list[PropertyChoice]:
    """Return the properties the results page offers: the name-like ones with values, or sub-properties with values.

    Args:
        properties: The properties search knows, in walk order, the name properties first.
        names: The name properties, by IRI, each with its name in `NAME_PROPERTIES` (`name_properties`).
        chosen: The IRIs of those the query names.
    """
    return [
        PropertyChoice(
            found.iri,
            property_label(found, names),
            found.values,
            found.sub_values,
            found.by_default,
            found.iri in chosen,
        )
        for found in properties
        if (found.values or found.sub_values) and not found.prose
    ]


@dataclass(frozen=True)
class ShownCommon:
    """A common value as the results page shows it: counted, as it's on too many entities to list (DESIGN.md §2.6).

    Attributes:
        value: The value, cut short.
        property: The label of the property it's asserted with.
        counts: How many entities of each type have it, and their share of that type, e.g. "2,291,619 classes (76%)".
    """

    value: str
    property: str
    counts: str


def share(part: int, whole: int) -> str:
    """Return a share as a whole percentage, or "<1%" for a share that rounds to none.

    Examples:
        >>> share(2_291_619, 3_025_059), share(3, 1000)
        ('76%', '<1%')
    """
    percent = round(100 * part / whole)
    return f"{percent}%" if percent else "<1%"


def shown_common(common: Sequence[CommonValue], labels: Mapping[str, str]) -> list[ShownCommon]:
    """Return how the results page shows common values, each property by its label (`property_label`)."""
    return [
        ShownCommon(
            cut_name(found.value),
            labels.get(found.property, cut_name(found.property)),
            " and ".join(f"{n:,} {TYPE_PLURALS[kind]} ({share(n, of)})" for kind, n, of in found.counts),
        )
        for found in common
    ]


def _params(asked: SearchRequest) -> list[tuple[str, str]]:
    """Return a search URL's parameters for the same query and filters, without the page; without `q=` if it's empty."""
    params = [(QUERY_PARAM, asked.text)] if asked.text else []
    params += [(TYPE_PARAM, kind.value) for kind in asked.types]
    params += [(PROPERTY_PARAM, iri) for iri in asked.properties]
    if asked.direct:
        params.append((VALUES_PARAM, "direct"))
    return params


def _pages(count: int | None) -> tuple[int, bool]:
    """Return how many pages of results the results page can show, and whether there are more results past them.

    Args:
        count: How many results there are, up to `COUNT_TO`; None if they weren't counted, as for a blank query.

    Examples:
        >>> _pages(0), _pages(51), _pages(COUNT_TO - 1), _pages(COUNT_TO)
        ((1, False), (2, False), (21, False), (21, True))
    """
    if count is None or count < COUNT_TO:
        return page_count(count or 0, PAGE_SIZE), False
    return LAST_PAGE, True


@router.api_route(f"/{SEARCH_PATH}", methods=["GET", "HEAD"], include_in_schema=False)
async def search_page(request: Request) -> Response:
    """Serve the full results page, or with `suggest=1`, the navigation bar's autocomplete fragment (DESIGN.md §2.13).

    A blank query finds nothing: the fragment is then empty, and the page shows only its form.
    """
    settings: Settings = request.app.state.settings
    names = name_properties(settings)
    asked = read_request(request)
    if asked is None:
        return not_found(request)
    try:
        query = SearchQuery(
            asked.text,
            types=frozenset(asked.types) or None,
            properties=frozenset(asked.properties) or None,
            order=tuple(names),
            entailed=not asked.direct,
            limit=SEARCH_LIMIT if asked.suggest else PAGE_SIZE,
            offset=asked.offset,
            count_to=0 if asked.suggest else COUNT_TO,
        )
    except ValidationError:
        return not_found(request)  # e.g. a lone surrogate
    normalized = normalize(asked.text)
    if asked.suggest and not normalized:
        return templates.TemplateResponse(request, "search_suggestions.html", {"blank": True}, headers=PANEL_HEADERS)
    state = await request.app.state.jobs.current()
    generation = ready_generation(state)
    backend: SearchBackend = request.app.state.search
    choices = _choices(await backend.properties(generation, list(names)), names, asked.properties)
    offered = [choice.iri for choice in choices]
    if not set(asked.properties) <= set(offered):
        return not_found(request)
    asked = replace(asked, properties=tuple(iri for iri in offered if iri in asked.properties))
    prefix = settings.reserved_prefix
    page_path = f"/{prefix}/{SEARCH_PATH}"
    if PAGE_PARAM in request.query_params and asked.page == 1:
        # The first page's URL is without `page`, so each page has one URL.
        return Response(status_code=301, headers={"Location": href(request, with_query(page_path, _params(asked)))})
    results = SearchResults((), more=False)  # the page's form alone
    if normalized:
        results = await backend.search(generation, query)
    root = link_root(request.scope)

    labels = {choice.iri: choice.label for choice in choices}

    def read(connection: sqlite3.Connection) -> list[ShownHit]:
        hits = shown_hits(connection, results.hits, normalized, prefix, root, with_ids=not asked.suggest)
        if asked.suggest:
            return hits
        return [
            replace(hit, property=labels.get(found.property)) for hit, found in zip(hits, results.hits, strict=True)
        ]

    hits = await read_derived(request, read, state)
    if asked.suggest:
        context = {"hits": hits, "blank": False, "all_results": href(request, with_query(page_path, _params(asked)))}
        return templates.TemplateResponse(request, "search_suggestions.html", context, headers=PANEL_HEADERS)
    pages, more = _pages(results.count)
    # None past the last page, which has no more results.
    pager = None
    if asked.page <= pages:
        pager = make_pager(request, "Result", asked.page, pages, page_path, _params(asked), more=more, nofollow=True)
    context = {
        "noindex": True,
        "text": asked.text,
        "search_text": asked.text,  # the navigation bar's box
        "blank": not normalized,
        "hits": hits,
        "common": shown_common(results.common, labels),
        "offset": asked.offset,
        "count": results.count,
        "more": more,
        "shown_cap": COUNT_TO - 1,  # with `more`, there are more results than this
        "pager": pager,
        "action": href(request, page_path),
        "types": [(kind.value, TYPE_LABELS[kind], kind in asked.types) for kind in EntityType],
        "properties": choices,
        "direct": asked.direct,
        "max_query_chars": MAX_QUERY_CHARS,
    }
    return templates.TemplateResponse(request, "search.html", context)
