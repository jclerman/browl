"""Routing of entity URLs (DESIGN.md §2.3, Lookup): the home page, entity pages, disambiguation, hash IRIs, 404.

Entity URLs come from IRIs, not from a route pattern, so a catch-all route, included after every other, looks each path
up in the derived database. Two routes under the reserved prefix serve what a path can't: `iri?iri=…`, an entity named
by its IRI (fallback URLs, and each of the IRIs that share a URL), and `hash?path=…&fragment=…`, which the hash-IRI
script (`static/hash.js`) fetches for the entity named by the URL's fragment, which the server never sees.

Entity pages have tabs (`browl.tabs`): the Overview tab (`browl.overview`), and the Usage tab (`browl.usage_tab`), paged
by `page=…`, whose label's count a third route serves, `usage-count?iri=…`, once the page has rendered. The ontology's
own page is the home page (`browl.home`), without tabs.
"""

import dataclasses
import html
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.routing import APIRoute
from starlette.routing import Match
from starlette.types import Scope

from browl.admin import REASONER_LABELS
from browl.derived import (
    TYPE_LABELS,
    Entity,
    EntityType,
    Meta,
    entities_at,
    entities_with_iri,
    is_hash_prefix,
    read_meta,
)
from browl.hierarchy_column import read_column
from browl.home import read_home
from browl.jobs import JobState
from browl.links import EntityLink, entity_name
from browl.overview import read_overview
from browl.pages import (
    PANEL_HEADERS,
    TREES,
    app_path,
    cut_name,
    href,
    is_under,
    link_root,
    not_found,
    read_derived,
    templates,
    tree_href,
)
from browl.tabs import (
    PANEL_PARAM,
    TAB_PARAM,
    Tab,
    TabLink,
    TabNotFoundError,
    TabQuery,
    choose_tab,
    read_tab_query,
    tab_links,
    with_query,
)
from browl.urls import FALLBACK_PATH, fallback_path
from browl.usage_tab import (
    POLL_S,
    USAGE_LABEL,
    PageNotFoundError,
    UsageTab,
    read_count,
    read_usage_tab,
    remaining_text,
    usage_label,
)

HASH_PATH = "hash"
"""The hash-IRI script's lookup route's path under the reserved prefix."""

USAGE_COUNT_PATH = "usage-count"
"""The route's path under the reserved prefix that serves an entity's Usage tab label, with its count."""

OVERVIEW = "overview"
"""The Overview tab's slug."""

USAGE = "usage"
"""The Usage tab's slug."""

PAGE_PARAM = "page"
"""The query parameter that names a page of the Usage tab, from 1; absent for the first."""

ENTITY_TABS = (Tab(OVERVIEW, "Overview"), Tab(USAGE, USAGE_LABEL))
"""An entity page's tabs (U-28); the first is shown by default."""

_PAGE_NUMBER = re.compile(r"[1-9][0-9]{0,8}")
"""A page number `page=…` may give: a positive integer, without a sign or leading zeros, so each page has one URL."""

POLL_ID = "usage-preparing"
"""The id of the Usage tab's "Preparing…", which polls (`usage.html`), and which htmx sends as `HX-Trigger`."""


class _EntityRoute(APIRoute):
    """The catch-all route, which matches no path under the reserved prefix, percent-decoded as the router decodes it.

    Such a path is never an entity URL (O-3), so one no other route matched gets the router's 404, or 405 for a method
    another route lacks, and never reaches a lookup.
    """

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        """Return no match for a path under the reserved prefix, else what `APIRoute` returns."""
        if scope["type"] == "http" and is_under(app_path(scope), f"/{scope['app'].state.settings.reserved_prefix}"):
            return Match.NONE, {}
        return super().matches(scope)


reserved_router = APIRouter()
"""The lookup routes under the reserved prefix."""

catch_all_router = APIRouter(route_class=_EntityRoute)
"""The catch-all route for entity URLs; included after every other route."""


@dataclass(frozen=True)
class _Found:
    """What a URL leads to.

    Attributes:
        entities: The entities with that URL, by IRI then type.
        hash_prefix: Whether hash entities have URLs with that path; always False when a fragment was looked up.
        meta: What the derived database was built from, if the ontology is among the entities; else None.
    """

    entities: list[Entity]
    hash_prefix: bool
    meta: Meta | None


def _found(connection: sqlite3.Connection, entities: list[Entity], hash_prefix: bool) -> _Found:
    """Return what a URL leads to, reading the meta only for the home page."""
    meta = read_meta(connection) if any(entity.type == EntityType.ONTOLOGY for entity in entities) else None
    return _Found(entities, hash_prefix, meta)


def _find(connection: sqlite3.Connection, path: str, fragment: str | None) -> _Found:
    """Return what a URL's path and fragment lead to."""
    hash_prefix = fragment is None and is_hash_prefix(connection, path)
    return _found(connection, entities_at(connection, path, fragment), hash_prefix)


def _find_iri(connection: sqlite3.Connection, iri: str) -> _Found:
    """Return what the URL of the entity with an IRI leads to: its own entities and any others that share its URL."""
    own = entities_with_iri(connection, iri)
    return _found(connection, entities_at(connection, own[0].path, own[0].fragment) if own else [], hash_prefix=False)


def request_path(request: Request) -> str:
    """Return a request's path from the app's root, in URI form, as the `entity` table stores paths.

    That's the raw path (`raw_path`, which Uvicorn and Starlette's test client always send), not Starlette's
    percent-decoded one, less the root path. Uvicorn prepends the root path to the raw path, as to the decoded one, and
    a client may send it too; it's removed only when the raw path decodes to the whole decoded path, so a raw path a
    server didn't prepend it to is kept whole.
    """
    scope = request.scope
    root = link_root(scope)
    text = scope["raw_path"].decode("latin-1")
    if root and text.startswith(root) and urllib.parse.unquote(text) == scope["path"]:
        text = text[len(root) :]
    return text


@dataclass(frozen=True)
class _TabUrls:
    """Where a page is served, for its tabs' links.

    Attributes:
        page: The page's URL, from the app's root, without `tab` or a fragment.
        fragment: The page's fragment, in URI form, if it has one.
        panel: The URL its tab panels are fetched from, from the app's root, without `tab` or `panel`; None if the
            page's own.
    """

    page: str
    fragment: str | None = None
    panel: str | None = None


@dataclass(frozen=True)
class _Poll:
    """How the Usage tab's "Preparing…" asks for its panel again, until the usage table is ready.

    Attributes:
        url: The panel's URL, root-relative.
        every_s: How often, in seconds.
        remaining: About how long the table's build has left, as the page says it; None if unknown.
    """

    url: str
    every_s: float
    remaining: str | None


@dataclass(frozen=True)
class _PageLink:
    """A link of the Usage tab's to another page.

    Attributes:
        href: The page's URL, root-relative.
        panel: The URL its panel is fetched from, root-relative.
    """

    href: str
    panel: str


@dataclass(frozen=True)
class _Pager:
    """The Usage tab's links to other pages, and its form to go to a page.

    Attributes:
        panel: This page's panel URL, root-relative, which `tabs.js` keeps in the history.
        previous: The link to the previous page; None on the first.
        next: The link to the next page; None on the last.
        action: The form's URL, root-relative, without its query, which the form's fields replace.
        action_panel: The URL htmx sends the form's fields to for a page's panel, root-relative.
        fields: The form's hidden fields: the page URL's own query, and the tab.
    """

    panel: str
    previous: _PageLink | None
    next: _PageLink | None
    action: str
    action_panel: str
    fields: list[tuple[str, str]]


def _pager(request: Request, at: _TabUrls, usage: UsageTab) -> _Pager | None:
    """Return the Usage tab's paging, if it has more than one page."""
    pages = usage.pages
    if pages is None or pages < 2:
        return None

    def params(page: int) -> list[tuple[str, str]]:
        return [(TAB_PARAM, USAGE), *([(PAGE_PARAM, str(page))] if page > 1 else [])]

    def panel_href(page: int) -> str:
        return href(request, with_query(at.panel or at.page, [*params(page), (PANEL_PARAM, "1")]))

    def link(page: int) -> _PageLink | None:
        if not 1 <= page <= pages:
            return None
        return _PageLink(href(request, with_query(at.page, params(page)), at.fragment), panel_href(page))

    path, _, query = at.page.partition("?")
    fields = [*urllib.parse.parse_qsl(query, keep_blank_values=True), (TAB_PARAM, USAGE)]
    return _Pager(
        panel=panel_href(usage.page),
        previous=link(usage.page - 1),
        next=link(usage.page + 1),
        action=href(request, path, at.fragment),
        # The page's own query is in the fields, a hash route's isn't.
        action_panel=href(request, with_query(at.panel or path, [(PANEL_PARAM, "1")])),
        fields=fields,
    )


def _page_number(request: Request) -> int | None:
    """Return the Usage tab's page a request asks for, 1 if none; None if it repeats `page` or isn't a page number."""
    values = request.query_params.getlist(PAGE_PARAM)
    if not values:
        return 1
    if len(values) > 1 or not _PAGE_NUMBER.fullmatch(values[0]):
        return None
    return int(values[0])


def _usage_label(request: Request, links: list[TabLink], iri: str, count: int | None) -> list[TabLink]:
    """Return tab links with the Usage tab's count: in its label if it's known, else fetched once the page renders."""
    changes: dict[str, Any]  # TabLink's fields
    if count is not None:
        changes = {"tab": Tab(USAGE, usage_label(count))}
    else:
        prefix: str = request.app.state.settings.reserved_prefix
        changes = {"label_url": href(request, with_query(f"/{prefix}/{USAGE_COUNT_PATH}", [("iri", iri)]))}
    return [dataclasses.replace(link, **changes) if link.tab.slug == USAGE else link for link in links]


async def _page(
    request: Request,
    state: JobState,
    prefix: str,
    entities: list[Entity],
    meta: Meta | None,
    query: TabQuery,
    at: _TabUrls,
    hash_path: str | None = None,
    noindex: bool = False,
    lenient: bool = False,
) -> Response:
    """Return the page for the entities at a URL: one IRI's page, or the disambiguation page for several (U-15).

    An entity page shows the tab the query asks for, or only its panel (`browl.tabs`).

    Args:
        request: The request.
        state: The job state the entities were read with, whose ready generation the page shows.
        prefix: The reserved prefix.
        entities: The entities at the URL, by IRI then type; at least one.
        meta: What the derived database was built from; not None if the ontology is among the entities.
        query: What the request's query asks of the page's tabs.
        at: Where the page is served.
        hash_path: The URL's path, when hash entities share it, so the page includes the hash-IRI script.
        noindex: Whether to ask search engines not to index the page, which is also at another URL.
        lenient: Whether to show the first tab rather than 404 a tab the page lacks (`choose_tab`); always so for a
            page with the hash-IRI script, which also shows the Usage tab's first page rather than 404 a page it
            lacks, as its fragment's entity may have it.
    """
    hash_script = None if hash_path is None else _hash_script(request, prefix, hash_path, prefix_only=False)
    common = {"hash_script": hash_script, "noindex": noindex}
    names: dict[str, str] = {}  # each IRI's name, in order
    for entity in entities:
        names.setdefault(entity.iri, entity_name(entity))
    is_home = any(entity.type == EntityType.ONTOLOGY for entity in entities)
    tabs = ENTITY_TABS if len(names) == 1 and not is_home else ()
    try:
        tab = choose_tab(tabs, query, lenient=lenient or hash_path is not None)
    except TabNotFoundError:
        return not_found(request)
    if len(names) > 1:
        choices = [EntityLink(iri, name, href(request, fallback_path(prefix, iri))) for iri, name in names.items()]
        return templates.TemplateResponse(request, "choose.html", {"choices": choices} | common)
    if is_home:
        assert meta is not None  # read for the home page (`_found`)
        name = cut_name(meta.title or meta.ontology_iri or "")
        home = await read_home(request, state, meta)
        trees = [(tree_href(request, tree), tree) for tree in TREES]
        home_context = {"home": home, "name": name, "trees": trees, "reasoners": REASONER_LABELS}
        return templates.TemplateResponse(request, "home.html", home_context | common)
    assert tab is not None  # an entity page has tabs
    links = tab_links(tabs, tab, href(request, at.page), at.fragment, href(request, at.panel or at.page))
    entity = entities[0]
    types = [TYPE_LABELS[each.type] for each in entities]
    context: dict[str, object] = {"entity": entity, "name": names[entity.iri], "types": types, "tab": tab}
    count = None
    if tab.slug == OVERVIEW:
        context["overview"] = await read_overview(request, state, entity.iri)
    elif tab.slug == USAGE:
        page = _page_number(request)
        if page == 1 and PAGE_PARAM in request.query_params and not query.panel and at.panel is None:
            # The first page's URL is without `page`, so each page has one URL; a fragment is kept by the browser.
            location = href(request, with_query(at.page, [(TAB_PARAM, USAGE)]), at.fragment)
            return Response(status_code=301, headers={"Location": location})
        if page is None and hash_path is None:
            return not_found(request)
        try:
            usage = await read_usage_tab(request, state, entity.iri, page or 1)
        except PageNotFoundError:
            # A poll's page can be past those listed once the table is ready: htmx doesn't swap a 404, so it'd poll on.
            if hash_path is None and request.headers.get("HX-Trigger") != POLL_ID:
                return not_found(request)
            usage = await read_usage_tab(request, state, entity.iri, 1)
        context["usage"] = usage
        context["pager"] = _pager(request, at, usage)
        context["poll"] = None
        if usage.preparing:
            params = [(TAB_PARAM, USAGE), *([(PAGE_PARAM, str(usage.page))] if usage.page > 1 else [])]
            url = href(request, with_query(at.panel or at.page, [*params, (PANEL_PARAM, "1")]))
            context["poll"] = _Poll(url, POLL_S, remaining_text(usage.remaining_s))
        count = usage.count
    context["tabs"] = _usage_label(request, links, entity.iri, count)
    if query.panel:
        return templates.TemplateResponse(request, "entity_panel.html", context, headers=PANEL_HEADERS)
    # Not in a panel alone, as the tabs keep the page's column.
    context["column"] = await read_derived(
        request, lambda connection: read_column(request, connection, entities), state
    )
    return templates.TemplateResponse(request, "entity.html", context | common)


def _hash_script(request: Request, prefix: str, path: str, prefix_only: bool) -> dict[str, str]:
    """Return the hash-IRI script's attributes: its lookup route, the URL's path, and whether no entity has it."""
    return {
        "lookup": href(request, f"/{prefix}/{HASH_PATH}"),
        "path": path,
        "prefix_only": "true" if prefix_only else "false",
    }


@reserved_router.api_route(f"/{FALLBACK_PATH}", methods=["GET", "HEAD"], include_in_schema=False)
async def entity_by_iri(request: Request, iri: str = "") -> Response:
    """Serve the page of the entity with IRI `iri`, if its URL is a fallback URL or shared by several IRIs.

    Any other entity is redirected to its own URL, so its page has one URL.
    """
    prefix: str = request.app.state.settings.reserved_prefix
    query = read_tab_query(request)
    if query is None:
        return not_found(request)
    state = await request.app.state.jobs.current()
    found = await read_derived(request, lambda connection: _find_iri(connection, iri), state)
    own = [entity for entity in found.entities if entity.iri == iri]
    if not own:
        return not_found(request)
    entity = own[0]
    if entity.path == fallback_path(prefix, iri) or len(own) < len(found.entities):
        return await _page(request, state, prefix, own, found.meta, query, _TabUrls(fallback_path(prefix, iri)))
    # Not a RedirectResponse, which would percent-encode characters such as `|` that stored URLs keep. A known tab is
    # kept.
    known = query.tab in {tab.slug for tab in ENTITY_TABS}
    params = [(TAB_PARAM, query.tab)] if known and query.tab is not None else []
    if query.tab == USAGE:
        params += [(PAGE_PARAM, page) for page in request.query_params.getlist(PAGE_PARAM)]
    location = with_query(entity.path, params)
    return Response(status_code=302, headers={"Location": href(request, location, entity.fragment)})


@reserved_router.api_route(f"/{USAGE_COUNT_PATH}", methods=["GET", "HEAD"], include_in_schema=False)
async def usage_count(request: Request, iri: str = "") -> Response:
    """Serve the Usage tab's label of the entity with IRI `iri`, with its count ("Usage (128)"), which its page fetches.

    It's "Usage" if the count fails (`browl.usage_tab.read_count`).
    """
    state = await request.app.state.jobs.current()
    if not await read_derived(request, lambda connection: entities_with_iri(connection, iri), state):
        return not_found(request)
    count = await read_count(request, state, iri)
    return HTMLResponse(html.escape(usage_label(count)), headers=PANEL_HEADERS)


@reserved_router.api_route(f"/{HASH_PATH}", methods=["GET", "HEAD"], include_in_schema=False)
async def hash_entity(request: Request, path: str = "", fragment: str = "") -> Response:
    """Serve the page for a hash URL, given its path and fragment in URI form, for the hash-IRI script (U-13).

    The script shows the page's main content in place of its own page's. Other query parameters, such as the tab, are
    the hash URL's own.
    """
    query = read_tab_query(request)
    if not path or not fragment or query is None:
        return not_found(request)
    state = await request.app.state.jobs.current()
    found = await read_derived(request, lambda connection: _find(connection, path, fragment), state)
    if not found.entities:
        return not_found(request)
    prefix: str = request.app.state.settings.reserved_prefix
    panel = with_query(f"/{prefix}/{HASH_PATH}", [("path", path), ("fragment", fragment)])
    # Lenient, as the page with the script is: one query serves every fragment on the path.
    at = _TabUrls(path, fragment, panel)
    return await _page(request, state, prefix, found.entities, found.meta, query, at, noindex=True, lenient=True)


@catch_all_router.api_route("/{_:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def entity_page(request: Request) -> Response:
    """Serve the page an entity URL's path leads to (DESIGN.md §2.3, Lookup); never one under the reserved prefix."""
    prefix: str = request.app.state.settings.reserved_prefix
    path = request_path(request)
    query = read_tab_query(request)
    if query is None:
        return not_found(request)
    state = await request.app.state.jobs.current()
    found = await read_derived(request, lambda connection: _find(connection, path, None), state)
    if found.entities:
        hash_path = path if found.hash_prefix else None
        return await _page(request, state, prefix, found.entities, found.meta, query, _TabUrls(path), hash_path)
    if found.hash_prefix and not query.panel:
        context = {"hash_script": _hash_script(request, prefix, path, prefix_only=True)}
        return templates.TemplateResponse(request, "hash.html", context)
    return not_found(request)
