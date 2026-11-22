"""Links to entity pages (U-4), labeled with display names (U-6); see DESIGN.md §2.9, Rendering names and IRIs."""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import batched

from browl.derived import Entity
from browl.pages import cut_name, join_link
from browl.urls import fallback_path

LOOKUP_BATCH = 500
"""The most IRIs or ids one query looks up, here and in `browl.usages` and `browl.usage_tab`: well under SQLite's
limit on a statement's parameters."""


@dataclass(frozen=True)
class EntityLink:
    """How a page shows an entity it mentions.

    Attributes:
        iri: The entity's IRI.
        name: Its display name, else its IRI, cut short (`browl.pages.cut_name`).
        href: Its page's root-relative link; None if the IRI has no page, and it's shown as text.
    """

    iri: str
    name: str
    href: str | None


def _name(display_name: str | None, iri: str) -> str:
    """Return the name a page shows for an entity: its display name, else its IRI, cut short."""
    return cut_name(display_name or iri)


def entity_name(entity: Entity) -> str:
    """Return the name a page shows for an entity (`_name`)."""
    return _name(entity.display_name, entity.iri)


def no_link(iri: str) -> EntityLink:
    """Return how a page shows an IRI that has no page: as text."""
    return EntityLink(iri, cut_name(iri), None)


def read_entity_links(
    connection: sqlite3.Connection, iris: Iterable[str], prefix: str, root: str
) -> dict[str, EntityLink]:
    """Return how a page shows each IRI it mentions, from the derived database.

    An entity links to its page: at its own URL, or at its fallback URL if other IRIs share its URL, so the link doesn't
    lead to the disambiguation page (U-15). An IRI the `entity` table lacks has no page.

    Args:
        connection: The derived database.
        iris: The IRIs; repeats are fine.
        prefix: The reserved prefix.
        root: The app's root path, for links (`browl.pages.link_root`).
    """
    wanted = list(dict.fromkeys(iris))
    links: dict[str, EntityLink] = {}
    for batch in batched(wanted, LOOKUP_BATCH):
        rows = connection.execute(
            "SELECT DISTINCT e.iri, e.path, e.fragment, e.display_name, EXISTS (SELECT 1 FROM entity AS o"
            "  WHERE o.path = e.path AND o.fragment IS e.fragment AND o.iri != e.iri)"
            f" FROM entity AS e WHERE e.iri IN ({', '.join('?' * len(batch))})",
            batch,
        )
        for iri, path, fragment, display_name, shared in rows:
            link = join_link(root, fallback_path(prefix, iri)) if shared else join_link(root, path, fragment)
            links[iri] = EntityLink(iri, _name(display_name, iri), link)
    return {iri: links.get(iri) or no_link(iri) for iri in wanted}
