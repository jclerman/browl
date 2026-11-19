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
from fastapi.responses import RedirectResponse, Response

from browl.derived import Entity, EntityType, Meta, entities_at, entities_with_iri, is_hash_prefix, read_meta
from browl.pages import read_derived, templates
from browl.urls import FALLBACK_PATH, fallback_path

HASH_PATH = "hash"
"""The hash-IRI script's lookup route's path under the reserved prefix."""

reserved_router = APIRouter()
"""The lookup routes under the reserved prefix."""

catch_all_router = APIRouter()
"""The catch-all route for entity URLs; included after every other route."""


@dataclass(frozen=True)
class _Found:
    """What a URL's path and fragment lead to.

    Attributes:
        entities: The entities with that URL, by IRI then type.
        hash_prefix: Whether hash entities have URLs with that path; always False when a fragment was looked up.
        meta: What the derived database was built from.
    """

    entities: list[Entity]
    hash_prefix: bool
    meta: Meta


def _find(path: str, fragment: str | None, connection: sqlite3.Connection) -> _Found:
    hash_prefix = fragment is None and is_hash_prefix(connection, path)
    return _Found(entities_at(connection, path, fragment), hash_prefix, read_meta(connection))


def _find_iri(iri: str, connection: sqlite3.Connection) -> tuple[list[Entity], list[Entity], Meta]:
    """Return the entities with an IRI, all the entities that share its URL, and the meta."""
    entities = entities_with_iri(connection, iri)
    sharing = entities_at(connection, entities[0].path, entities[0].fragment) if entities else []
    return entities, sharing, read_meta(connection)


def request_path(request: Request) -> str:
    """Return a request's path from the app's root, in URI form, as the `entity` table stores paths.

    That's the raw path, not Starlette's percent-decoded one, less the root path when the raw path includes it (as it
    does when a client sends it, but not when a proxy strips it before Uvicorn adds it back to the decoded path).
    """
    scope = request.scope
    root: str = scope.get("root_path", "")
    path: str = scope["path"]
    raw: bytes | None = scope.get("raw_path")
    if raw is None:
        return urllib.parse.quote(path.removeprefix(root))
    text = raw.decode("latin-1")
    if root and text.startswith(root) and urllib.parse.unquote(text) == path:
        text = text[len(root) :]
    return text


def href(request: Request, path: str, fragment: str | None = None) -> str:
    """Return a root-relative link to a path from the app's root, with a fragment if given."""
    root = request.scope.get("root_path", "").rstrip("/")
    return f"{root}{path}" if fragment is None else f"{root}{path}#{fragment}"


def _reserved_prefix(request: Request) -> str:
    prefix: str = request.app.state.settings.reserved_prefix
    return prefix


def not_found(request: Request) -> Response:
    """Return the "not found" page (U-14), with status 404."""
    return templates.TemplateResponse(request, "not_found.html", {"hash_script": None}, status_code=404)


def _page(
    request: Request, entities: list[Entity], meta: Meta, hash_path: str | None = None, noindex: bool = False
) -> Response:
    """Return the page for the entities at a URL: one IRI's page, or the disambiguation page for several (U-15).

    Args:
        request: The request.
        entities: The entities at the URL, by IRI then type; at least one.
        meta: What the derived database was built from.
        hash_path: The URL's path, when hash entities share it, so the page includes the hash-IRI script.
        noindex: Whether to ask search engines not to index the page, which is also at another URL.
    """
    hash_script = None if hash_path is None else _hash_script(request, hash_path, prefix_only=False)
    common = {"hash_script": hash_script, "noindex": noindex}
    iris = list(dict.fromkeys(entity.iri for entity in entities))
    if len(iris) > 1:
        prefix = _reserved_prefix(request)
        choices = [
            (iri, href(request, fallback_path(prefix, iri)), [e for e in entities if e.iri == iri]) for iri in iris
        ]
        return templates.TemplateResponse(request, "choose.html", {"choices": choices} | common)
    if any(entity.type == EntityType.ONTOLOGY for entity in entities):
        return templates.TemplateResponse(request, "home.html", {"meta": meta} | common)
    context = {"entity": entities[0], "types": [entity.type for entity in entities]} | common
    return templates.TemplateResponse(request, "entity.html", context)


def _hash_script(request: Request, path: str, prefix_only: bool) -> dict[str, str]:
    """Return the hash-IRI script's attributes: its lookup route, the URL's path, and whether no entity has it."""
    return {
        "lookup": href(request, f"/{_reserved_prefix(request)}/{HASH_PATH}"),
        "path": path,
        "prefix_only": "true" if prefix_only else "false",
    }


@reserved_router.get(f"/{FALLBACK_PATH}", include_in_schema=False)
async def entity_by_iri(request: Request, u: str = "") -> Response:
    """Serve the page of the entity with IRI `u`, if its URL is a fallback URL or shared by several IRIs.

    Any other entity is redirected to its own URL, so its page has one URL.
    """
    entities, sharing, meta = await read_derived(request, lambda connection: _find_iri(u, connection))
    if not entities:
        return not_found(request)
    entity = entities[0]
    if entity.path == fallback_path(_reserved_prefix(request), entity.iri) or any(e.iri != u for e in sharing):
        return _page(request, entities, meta)
    return RedirectResponse(href(request, entity.path, entity.fragment), status_code=302)


@reserved_router.get(f"/{HASH_PATH}", include_in_schema=False)
async def hash_entity(request: Request, path: str = "", fragment: str = "") -> Response:
    """Serve the page for a hash URL, given its path and fragment in URI form, for the hash-IRI script (U-13).

    The script shows the page's main content in place of its own page's. Other query parameters, such as the tab, are
    the hash URL's own.
    """
    if not path or not fragment:
        return not_found(request)
    found = await read_derived(request, lambda connection: _find(path, fragment, connection))
    if not found.entities:
        return not_found(request)
    return _page(request, found.entities, found.meta, noindex=True)


@catch_all_router.api_route("/{_:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def entity_page(request: Request) -> Response:
    """Serve the page an entity URL's path leads to (DESIGN.md §2.3, Lookup).

    Paths under the reserved prefix, percent-decoded as the router decodes them, are never entity URLs (O-3): one no
    other route matched is "not found".
    """
    relative = request.scope["path"].removeprefix(request.scope.get("root_path", ""))
    if relative.split("/", 2)[1:2] == [_reserved_prefix(request)]:
        return not_found(request)
    path = request_path(request)
    found = await read_derived(request, lambda connection: _find(path, None, connection))
    if found.entities:
        return _page(request, found.entities, found.meta, path if found.hash_prefix else None)
    if found.hash_prefix:
        context = {"hash_script": _hash_script(request, path, prefix_only=True)}
        return templates.TemplateResponse(request, "hash.html", context)
    return not_found(request)
