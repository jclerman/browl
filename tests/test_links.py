"""Tests for links to entity pages (U-4, U-6): `browl.links` and the `entity_link` macro."""

import sqlite3
from collections.abc import Iterator

import pytest
from pydantic import HttpUrl

from browl.derived import SCHEMA, EntityType, create_indexes
from browl.links import TYPE_LABELS, EntityLink, read_entity_links
from browl.pages import MAX_NAME_CHARS, templates
from browl.precache.entities import Reserved, insert_entities
from browl.urls import URLMapper

ENTITIES = [
    ("http://example.org/a", EntityType.CLASS),
    ("http://example.org/a", EntityType.OBJECT_PROPERTY),  # punned
    ("http://example.org/vocab#B", EntityType.CLASS),  # a hash entity
    ("http://other.org/c", EntityType.CLASS),  # with the next, at /other.org/c
    ("https://other.org/c", EntityType.CLASS),
    ("urn:example:d", EntityType.DATA_PROPERTY),  # at a fallback URL
]
NAMES = {"http://example.org/a": "Alpha", "http://other.org/c": "Gamma"}


@pytest.fixture
def connection() -> Iterator[sqlite3.Connection]:
    """Yield a derived database holding `ENTITIES`, mapped under http://example.org/, with `NAMES`."""
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(SCHEMA)
        insert_entities(ENTITIES, URLMapper(HttpUrl("http://example.org/"), "_browl"), Reserved(), connection)
        create_indexes(connection, "entity")
        for iri, name in NAMES.items():
            connection.execute("UPDATE entity SET display_name = ? WHERE iri = ?", (name, iri))
        yield connection


def test_read_entity_links(connection: sqlite3.Connection) -> None:
    """Check each IRI's link: its own URL, or its fallback URL if shared; none for an IRI with no page."""
    iris = [
        "http://example.org/a",
        "http://example.org/vocab#B",
        "http://other.org/c",
        "https://other.org/c",
        "urn:example:d",
        "http://nowhere.org/e",
        "http://example.org/a",
    ]
    links = read_entity_links(connection, iris, "_browl", "")
    assert list(links.values()) == [
        EntityLink("http://example.org/a", "Alpha", "/a"),
        EntityLink("http://example.org/vocab#B", "http://example.org/vocab#B", "/vocab#B"),
        EntityLink("http://other.org/c", "Gamma", "/_browl/iri?iri=http%3A%2F%2Fother.org%2Fc"),
        EntityLink("https://other.org/c", "https://other.org/c", "/_browl/iri?iri=https%3A%2F%2Fother.org%2Fc"),
        EntityLink("urn:example:d", "urn:example:d", "/_browl/iri?iri=urn%3Aexample%3Ad"),
        EntityLink("http://nowhere.org/e", "http://nowhere.org/e", None),
    ]


def test_read_entity_links_root_and_prefix(connection: sqlite3.Connection) -> None:
    """Check that links include the app's root path and the given reserved prefix."""
    links = read_entity_links(connection, ["http://example.org/a", "http://other.org/c"], "_r", "/root")
    assert [link.href for link in links.values()] == ["/root/a", "/root/_r/iri?iri=http%3A%2F%2Fother.org%2Fc"]


def test_read_entity_links_many(connection: sqlite3.Connection) -> None:
    """Check that more IRIs than one query looks up are all read, in the order given."""
    iris = [f"http://nowhere.org/{number}" for number in range(1200)] + ["http://example.org/a"]
    links = read_entity_links(connection, iris, "_browl", "")
    assert list(links) == iris
    assert links["http://example.org/a"].href == "/a"


def test_read_entity_links_long_name(connection: sqlite3.Connection) -> None:
    """Check that a long display name is cut short."""
    connection.execute("UPDATE entity SET display_name = ? WHERE iri = ?", ("x" * 500, "http://example.org/a"))
    name = read_entity_links(connection, ["http://example.org/a"], "_browl", "")["http://example.org/a"].name
    assert name == "x" * (MAX_NAME_CHARS - 1) + "…"


def render(link: EntityLink) -> str:
    """Return the `entity_link` macro's rendering of a link."""
    macro = templates.env.from_string('{% from "links.html" import entity_link %}{{ entity_link(link) }}')
    return macro.render(link=link)


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (EntityLink("http://x/a", "A <b>", "/a?x=1&y=2"), '<a href="/a?x=1&amp;y=2"><bdi>A &lt;b&gt;</bdi></a>'),
        (EntityLink("http://x/a", 'A "q"', None), "<bdi>A &#34;q&#34;</bdi>"),
    ],
)
def test_entity_link_macro(link: EntityLink, expected: str) -> None:
    """Check that the macro renders a link, or text for an IRI with no page, with the name escaped and isolated."""
    assert render(link) == expected


def test_type_labels() -> None:
    """Check that every entity type has a label."""
    assert set(TYPE_LABELS) == set(EntityType)
