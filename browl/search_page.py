"""The search route, `search`: the navigation bar's autocomplete fragment, and the full results page (U-10, U-17).

Both show each hit the same way (DESIGN.md §2.9, Search): the value that matched, its query's prefix marked, then
"→" and the entity's name if that's different, and its types in a column of their own.
"""

import sqlite3
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import ValidationError

from browl.derived import TYPE_LABELS, EntityType, ready_generation
from browl.links import EntityLink, read_entity_links
from browl.lookup import PANEL_HEADERS, not_found
from browl.names import name_properties, normalize
from browl.pages import (
    MAX_IRI_CHARS,
    SEARCH_PATH,
    SUGGEST_PARAM,
    cut_name,
    cut_text,
    href,
    link_root,
    read_derived,
    read_offset,
    single_params,
    templates,
)
from browl.search import (
    MAX_OFFSET,
    MAX_QUERY_CHARS,
    SEARCH_LIMIT,
    SearchBackend,
    SearchHit,
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
"""The query parameter, repeated, that names each name property, by IRI, whose values to search; none for all."""

VALUES_PARAM = "values"
"""The query parameter that says which values count: `all`, the default, or `direct`, without entailed ones."""

OFFSET_PARAM = "offset"
"""The query parameter that says how many results come before those shown; absent for none."""

PAGE_SIZE = 50
"""How many results the full results page shows at once."""

_VALUES = ("all", "direct")
"""What `values=` may give."""

router = APIRouter()
"""The search route, under the reserved prefix."""


@dataclass(frozen=True)
class SearchRequest:
    """What a search URL asks for.

    Attributes:
        text: The query's text, as given.
        types: The entity types to find; empty for all.
        properties: The name properties to search, by IRI; empty for all.
        direct: Whether only direct values count.
        offset: How many results come before those shown.
        suggest: Whether it asks for the autocomplete fragment.
    """

    text: str
    types: tuple[EntityType, ...]
    properties: tuple[str, ...]
    direct: bool
    offset: int
    suggest: bool


@dataclass(frozen=True)
class ShownHit:
    """A search hit as the autocomplete and the results page show it.

    Attributes:
        link: The entity's link, with its name.
        marked: The start of the matched value that matches the query, cut short; None if the value isn't shown.
        rest: The rest of the matched value, cut short; None if the value isn't shown.
        name_marked: The start of the entity's name that matches the query, when the name is the matched value;
            else empty.
        name_rest: The rest of the entity's name.
        types: Its types' labels, joined.
        id_text: Its CURIE, else its IRI cut short, which the results page shows; None in the autocomplete.
    """

    link: EntityLink
    marked: str | None
    rest: str | None
    name_marked: str
    name_rest: str
    types: str
    id_text: str | None


@dataclass(frozen=True)
class PropertyChoice:
    """A name property the results page lets the visitor search.

    Attributes:
        iri: Its IRI.
        label: Its display name, else its name in `NAME_PROPERTIES`, cut short.
        checked: Whether the query names it.
    """

    iri: str
    label: str
    checked: bool


def read_request(request: Request, properties: Collection[str]) -> SearchRequest | None:
    """Return what a search URL asks for; None if a parameter is repeated where it mustn't be, or isn't valid.

    Args:
        request: The request.
        properties: The name properties' IRIs (`browl.names.name_properties`), which `property=` may name.
    """
    params = request.query_params
    single = single_params(request, (QUERY_PARAM, VALUES_PARAM, OFFSET_PARAM, SUGGEST_PARAM))
    if single is None:
        return None
    text, values, suggest = single[QUERY_PARAM] or "", single[VALUES_PARAM], single[SUGGEST_PARAM]
    if len(text) > MAX_QUERY_CHARS or (values is not None and values not in _VALUES) or suggest not in (None, "1"):
        return None
    offset = read_offset(single[OFFSET_PARAM])
    if offset is None or offset > MAX_OFFSET or (offset and suggest):  # the fragment isn't paged
        return None
    try:
        types = tuple(dict.fromkeys(EntityType(kind) for kind in params.getlist(TYPE_PARAM)))
    except ValueError:
        return None
    chosen = tuple(dict.fromkeys(params.getlist(PROPERTY_PARAM)))
    if not set(chosen) <= set(properties):
        return None
    return SearchRequest(text, types, chosen, values == "direct", offset, suggest == "1")


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
    if not query:
        return 0
    for end in range(1, len(value) + 1):
        if len(normalize(value[:end])) >= len(query):
            while end < len(value) and unicodedata.combining(value[end]):
                end += 1
            return end
    return len(value)


def _split(text: str, query: str) -> tuple[str, str]:
    """Return a text cut short (`cut_name`), split where its match of the query ends, before any "…" the cut added."""
    shown = cut_name(text)
    kept = len(shown) if shown == text else len(shown) - 1
    end = marked_length(text[:kept], query)  # only what's shown: the query may be far longer
    return shown[:end], shown[end:]


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
            marked = rest = None
            name_marked, name_rest = _split(hit.value, query)
        else:
            marked, rest = _split(hit.value, query)
            name_marked, name_rest = "", link.name
        types = ", ".join(TYPE_LABELS[kind] for kind in hit.types)
        shown.append(ShownHit(link, marked, rest, name_marked, name_rest, types, ids.get(hit.iri)))
    return shown


def property_choices(
    connection: sqlite3.Connection, properties: Mapping[str, str], chosen: Sequence[str]
) -> list[PropertyChoice]:
    """Return the name properties the results page offers, labeled by display name, else by their setting's name.

    Args:
        connection: The derived database.
        properties: The name properties, by IRI, each with its name in `NAME_PROPERTIES` (`name_properties`).
        chosen: The IRIs of those the query names.
    """
    names: dict[str, str] = {}
    if properties:
        placeholders = ", ".join("?" * len(properties))
        rows = connection.execute(
            f"SELECT iri, display_name FROM entity WHERE iri IN ({placeholders}) AND display_name IS NOT NULL"
            " ORDER BY type",
            list(properties),
        )
        for iri, name in rows:
            names.setdefault(iri, name)
    return [
        PropertyChoice(iri, cut_name(names.get(iri) or setting), iri in chosen) for iri, setting in properties.items()
    ]


def _params(asked: SearchRequest, offset: int = 0) -> list[tuple[str, str]]:
    """Return a search URL's parameters for the same query and filters, from an offset."""
    params = [(QUERY_PARAM, asked.text)]
    params += [(TYPE_PARAM, kind.value) for kind in asked.types]
    params += [(PROPERTY_PARAM, iri) for iri in asked.properties]
    if asked.direct:
        params.append((VALUES_PARAM, "direct"))
    if offset:
        params.append((OFFSET_PARAM, str(offset)))
    return params


@router.api_route(f"/{SEARCH_PATH}", methods=["GET", "HEAD"], include_in_schema=False)
async def search_page(request: Request) -> Response:
    """Serve the full results page, or with `suggest=1`, the navigation bar's autocomplete fragment (DESIGN.md §2.13).

    A blank query finds nothing: the fragment is then empty, and the page shows only its form.
    """
    settings: Settings = request.app.state.settings
    properties = name_properties(settings)
    asked = read_request(request, properties)
    if asked is None:
        return not_found(request)
    try:
        query = SearchQuery(
            asked.text,
            types=frozenset(asked.types) or None,
            properties=frozenset(asked.properties) or None,
            entailed=not asked.direct,
            limit=SEARCH_LIMIT if asked.suggest else PAGE_SIZE,
            offset=0 if asked.suggest else asked.offset,
        )
    except ValidationError:
        return not_found(request)  # e.g. a lone surrogate
    normalized = normalize(asked.text)
    if asked.suggest and not normalized:
        return templates.TemplateResponse(request, "search_suggestions.html", {"blank": True}, headers=PANEL_HEADERS)
    state = await request.app.state.jobs.current()
    generation = ready_generation(state)
    results = SearchResults((), more=False)  # the page's form alone
    if normalized:
        backend: SearchBackend = request.app.state.search
        results = await backend.search(generation, query)
    prefix = settings.reserved_prefix
    root = link_root(request.scope)

    def read(connection: sqlite3.Connection) -> tuple[list[ShownHit], list[PropertyChoice]]:
        hits = shown_hits(connection, results.hits, normalized, prefix, root, with_ids=not asked.suggest)
        choices = [] if asked.suggest else property_choices(connection, properties, asked.properties)
        return hits, choices

    hits, choices = await read_derived(request, read, state)
    page_path = f"/{prefix}/{SEARCH_PATH}"
    if asked.suggest:
        context = {"hits": hits, "blank": False, "all_results": href(request, with_query(page_path, _params(asked)))}
        return templates.TemplateResponse(request, "search_suggestions.html", context, headers=PANEL_HEADERS)
    previous = next_ = None
    if asked.offset:
        previous = href(request, with_query(page_path, _params(asked, max(asked.offset - PAGE_SIZE, 0))))
    if results.more and asked.offset + PAGE_SIZE <= MAX_OFFSET:
        next_ = href(request, with_query(page_path, _params(asked, asked.offset + PAGE_SIZE)))
    context = {
        "noindex": True,
        "text": asked.text,
        "blank": not normalized,
        "hits": hits,
        "offset": asked.offset,
        "previous": previous,
        "next": next_,
        "action": href(request, page_path),
        "types": [(kind.value, TYPE_LABELS[kind], kind in asked.types) for kind in EntityType],
        "properties": choices,
        "direct": asked.direct,
        "max_query_chars": MAX_QUERY_CHARS,
    }
    return templates.TemplateResponse(request, "search.html", context)
