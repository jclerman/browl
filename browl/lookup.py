"""Routing of entity URLs (DESIGN.md §2.3, Lookup): the home page, entity pages, disambiguation, hash IRIs, 404.

Entity URLs come from IRIs, not from a route pattern, so a catch-all route, included after every other, looks each path
up in the derived database. Two routes under the reserved prefix serve what a path can't: `iri?u=…`, an entity named
by its IRI (fallback URLs, and each of the IRIs that share a URL), and `hash?path=…&fragment=…`, which the hash-IRI
script (`static/hash.js`) fetches for the entity named by the URL's fragment, which the server never sees.

The entity and home pages are placeholders until P-16 and P-17.
"""

import sqlite3
import urllib.parse
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.routing import APIRoute
from starlette.routing import Match
from starlette.types import Scope

from browl.derived import Entity, EntityType, Meta, entities_at, entities_with_iri, is_hash_prefix, read_meta
from browl.pages import app_path, is_under, link_root, read_derived, templates
from browl.urls import FALLBACK_PATH, fallback_path

HASH_PATH = "hash"
"""The hash-IRI script's lookup route's path under the reserved prefix."""


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


def href(request: Request, path: str, fragment: str | None = None) -> str:
    """Return a root-relative link to a path from the app's root, with a fragment if given."""
    root = link_root(request.scope)
    return f"{root}{path}" if fragment is None else f"{root}{path}#{fragment}"


def not_found(request: Request) -> Response:
    """Return the "not found" page (U-14), with status 404."""
    return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)


def _page(
    request: Request,
    prefix: str,
    entities: list[Entity],
    meta: Meta | None,
    hash_path: str | None = None,
    noindex: bool = False,
) -> Response:
    """Return the page for the entities at a URL: one IRI's page, or the disambiguation page for several (U-15).

    Args:
        request: The request.
        prefix: The reserved prefix.
        entities: The entities at the URL, by IRI then type; at least one.
        meta: What the derived database was built from; not None if the ontology is among the entities.
        hash_path: The URL's path, when hash entities share it, so the page includes the hash-IRI script.
        noindex: Whether to ask search engines not to index the page, which is also at another URL.
    """
    hash_script = None if hash_path is None else _hash_script(request, prefix, hash_path, prefix_only=False)
    common = {"hash_script": hash_script, "noindex": noindex}
    names: dict[str, str] = {}  # each IRI's display name, in order
    for entity in entities:
        names.setdefault(entity.iri, entity.display_name or entity.iri)
    if len(names) > 1:
        choices = [(iri, href(request, fallback_path(prefix, iri)), name) for iri, name in names.items()]
        return templates.TemplateResponse(request, "choose.html", {"choices": choices} | common)
    if any(entity.type == EntityType.ONTOLOGY for entity in entities):
        return templates.TemplateResponse(request, "home.html", {"meta": meta} | common)
    context = {"entity": entities[0], "types": [entity.type for entity in entities]} | common
    return templates.TemplateResponse(request, "entity.html", context)


def _hash_script(request: Request, prefix: str, path: str, prefix_only: bool) -> dict[str, str]:
    """Return the hash-IRI script's attributes: its lookup route, the URL's path, and whether no entity has it."""
    return {
        "lookup": href(request, f"/{prefix}/{HASH_PATH}"),
        "path": path,
        "prefix_only": "true" if prefix_only else "false",
    }


@reserved_router.api_route(f"/{FALLBACK_PATH}", methods=["GET", "HEAD"], include_in_schema=False)
async def entity_by_iri(request: Request, u: str = "") -> Response:
    """Serve the page of the entity with IRI `u`, if its URL is a fallback URL or shared by several IRIs.

    Any other entity is redirected to its own URL, so its page has one URL.
    """
    prefix: str = request.app.state.settings.reserved_prefix
    found = await read_derived(request, lambda connection: _find_iri(connection, u))
    own = [entity for entity in found.entities if entity.iri == u]
    if not own:
        return not_found(request)
    entity = own[0]
    if entity.path == fallback_path(prefix, u) or len(own) < len(found.entities):
        return _page(request, prefix, own, found.meta)
    # Not a RedirectResponse, which would percent-encode characters such as `|` that stored URLs keep.
    return Response(status_code=302, headers={"Location": href(request, entity.path, entity.fragment)})


@reserved_router.api_route(f"/{HASH_PATH}", methods=["GET", "HEAD"], include_in_schema=False)
async def hash_entity(request: Request, path: str = "", fragment: str = "") -> Response:
    """Serve the page for a hash URL, given its path and fragment in URI form, for the hash-IRI script (U-13).

    The script shows the page's main content in place of its own page's. Other query parameters, such as the tab, are
    the hash URL's own.
    """
    if not path or not fragment:
        return not_found(request)
    found = await read_derived(request, lambda connection: _find(connection, path, fragment))
    if not found.entities:
        return not_found(request)
    return _page(request, request.app.state.settings.reserved_prefix, found.entities, found.meta, noindex=True)


@catch_all_router.api_route("/{_:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def entity_page(request: Request) -> Response:
    """Serve the page an entity URL's path leads to (DESIGN.md §2.3, Lookup); never one under the reserved prefix."""
    prefix: str = request.app.state.settings.reserved_prefix
    path = request_path(request)
    found = await read_derived(request, lambda connection: _find(connection, path, None))
    if found.entities:
        return _page(request, prefix, found.entities, found.meta, path if found.hash_prefix else None)
    if found.hash_prefix:
        context = {"hash_script": _hash_script(request, prefix, path, prefix_only=True)}
        return templates.TemplateResponse(request, "hash.html", context)
    return not_found(request)
