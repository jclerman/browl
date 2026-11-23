"""Search (DESIGN.md §2.6): the `SearchBackend` interface, and its v1 implementation over SQLite, `SqliteSearch`.

Only this module knows the search engine (N-5): pre-caching calls `BACKEND.index`, and pages call a backend's `search`.
"""

import heapq
import itertools
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, NamedTuple

from pydantic import AfterValidator, Field
from pydantic.dataclasses import dataclass as validated_dataclass

from browl.derived import DerivedReader, EntityType
from browl.names import LAST_CHAR, name_property_iris, normalize, rank_tables

if TYPE_CHECKING:
    from browl.precache.core import PreCacheContext

SEARCH_LIMIT = 10
"""How many results a query returns by default."""

MAX_LIMIT = 100
"""The most results a query may ask for at once."""

MAX_OFFSET = 1000
"""The most results a query may skip: paging deeper reads more of the index for each page."""

MAX_QUERY_CHARS = 1000
"""The longest query text."""

KEY_CHARS = 256
"""How many characters of a normalized value, and of the value, the index's key holds. A query seeks each lane at each
length up to this, at most, however long its values are; longer values rank among themselves by these characters."""


def _encodable(text: str) -> str:
    """Return the text if it encodes as UTF-8; it doesn't if it has a lone surrogate, which a JSON string can carry.

    Raises:
        ValueError: It doesn't.
    """
    try:
        text.encode()
    except UnicodeEncodeError as error:
        raise ValueError("the text isn't valid Unicode") from error
    return text


@validated_dataclass(frozen=True)
class SearchQuery:
    """A search (U-10): name values that start with the text, ignoring case and accents (U-11).

    Attributes:
        text: What the values start with; leading whitespace is ignored, and a query of only whitespace finds nothing.
        types: The entity types to find; None for all, and an empty set for none.
        properties: The name properties (IRIs) whose values to search; None for all, and an empty set for none.
        entailed: Whether values entailed through a sub-property count too, or only direct ones.
        limit: How many results to return.
        offset: How many of the best results to skip, for paging.
    """

    text: Annotated[str, Field(max_length=MAX_QUERY_CHARS), AfterValidator(_encodable)]
    types: frozenset[EntityType] | None = None
    properties: frozenset[str] | None = None
    entailed: bool = True
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = SEARCH_LIMIT
    offset: Annotated[int, Field(ge=0, le=MAX_OFFSET)] = 0


@dataclass(frozen=True)
class SearchHit:
    """An IRI that matches, with its best-matching value (U-12).

    Attributes:
        iri: The IRI; a punned IRI is one hit.
        types: Its types that the query's filter allows, in `EntityType` order.
        value: Its best-matching name value, as written.
        property: That value's name property's IRI.
        language: That value's language tag, lowercased; None if it has none.
        exact: Whether the value is the whole query, rather than starting with it.
    """

    iri: str
    types: tuple[EntityType, ...]
    value: str
    property: str
    language: str | None
    exact: bool


@dataclass(frozen=True)
class SearchResults:
    """A page of a search's results, best first.

    Attributes:
        hits: The results.
        more: Whether there are more after them.
    """

    hits: tuple[SearchHit, ...]
    more: bool


class SearchBackend(ABC):
    """A search engine: indexes the name values during pre-caching, and answers queries (N-5).

    Args:
        reader: Reads the derived database.
    """

    def __init__(self, reader: DerivedReader) -> None:
        """Read through the reader."""
        self.reader = reader

    @staticmethod
    @abstractmethod
    async def index(context: "PreCacheContext") -> None:
        """Index the new generation's name values, during pre-caching, once `name_value` is filled."""

    @abstractmethod
    async def search(self, generation: int, query: SearchQuery) -> SearchResults:
        """Return a page of a query's results in a generation, ranked by §2.6.

        Raises:
            NotCurrentError: The generation's derived database isn't current.
        """


class SqliteSearch(SearchBackend):
    """Search over the derived database's `search_value` table, keyed in ranking order (DESIGN.md §2.6)."""

    @staticmethod
    async def index(context: "PreCacheContext") -> None:
        """Fill `search_value` and `search_lane` from `name_value`, with each value's ranks by the current settings.

        A value that normalizes to nothing (e.g. only whitespace or combining marks) is left out, as no query matches
        it. Of an entity's values that differ only in language tags that rank alike, one is kept.
        """
        settings = context.job.settings
        await context.db.run(
            lambda connection: fill_index(connection, name_property_iris(settings), settings.language_ranking)
        )

    async def search(self, generation: int, query: SearchQuery) -> SearchResults:
        """Return a page of a query's results in a generation (`search`)."""
        return await self.reader.read(generation, lambda connection: search(connection, query))


BACKEND: type[SearchBackend] = SqliteSearch
"""The search engine in use, whose `index` pre-caching calls."""


def fill_index(connection: sqlite3.Connection, properties: Sequence[str], ranking: Sequence[str]) -> None:
    """Fill the index from `name_value`, ranking values by the name properties' IRIs and the language ranking."""
    # Lengths and cuts are Python's: SQLite's length() and substr() stop at a NUL, which a literal may hold.
    connection.create_function("key_length", 1, lambda text: min(len(text), KEY_CHARS), deterministic=True)
    connection.create_function("key_head", 1, lambda text: text[:KEY_CHARS], deterministic=True)
    connection.create_function("key_rest", 1, lambda text: text[KEY_CHARS:] or None, deterministic=True)
    with rank_tables(connection, properties, ranking):
        # Grouping by the key keeps one row of an entity's value in languages that rank alike, the first tag's.
        connection.execute(
            "INSERT INTO search_value"
            " SELECT entity.type, p.rank, l.rank, 1 - nv.direct, key_length(nv.normalized), key_head(nv.normalized),"
            "  key_head(nv.value), nv.entity, key_rest(nv.normalized), key_rest(nv.value), min(nv.language)"
            " FROM name_value AS nv JOIN entity ON entity.id = nv.entity"
            " JOIN property_rank AS p USING (property) JOIN language_rank AS l ON l.language IS nv.language"
            " WHERE nv.normalized <> ''"
            " GROUP BY 1, 2, 3, 4, 5, 6, 7, 8"
            " ORDER BY 1, 2, 3, 4, 5, 6, 7, 8"
        )
        connection.execute(
            "INSERT INTO search_lane"
            " SELECT type, property_rank, language_rank, entailed, max(length), p.property"
            " FROM search_value JOIN property_rank AS p ON p.rank = property_rank"
            " GROUP BY type, property_rank, language_rank, entailed"
        )


@validated_dataclass(frozen=True)
class _Lane:
    """A lane of `search_value`: its values of one type, name property rank, language rank, and direct or entailed.

    Its values are stored by length, then normalized value, then value, so a query reads its matches of one length in
    rank order.

    Attributes:
        type: Its entities' type.
        property_rank: Its name property's position in `NAME_PROPERTIES`, from 0.
        language_rank: Its languages' rank by `LANGUAGE_RANKING`, from 0; languages that rank alike share one.
        entailed: Whether its values are entailed through a sub-property, rather than direct.
        max_length: Its longest normalized value's length, at most `KEY_CHARS`.
        property: Its name property's IRI.
    """

    type: EntityType
    property_rank: Annotated[int, Field(ge=0)]
    language_rank: Annotated[int, Field(ge=0)]
    entailed: bool
    max_length: Annotated[int, Field(ge=1, le=KEY_CHARS)]
    property: str

    def tier(self) -> tuple[int, int, bool]:
        """Return its rank among lanes (§2.6, rules 2-4): lanes of one tier rank alike."""
        return self.property_rank, self.language_rank, self.entailed


class _Match(NamedTuple):
    normalized: str
    value: str
    iri: str
    language: str | None
    lane: _Lane


_MATCHES = (
    "SELECT s.normalized, s.normalized_rest, s.value, s.value_rest, entity.iri, s.language"
    " FROM search_value AS s JOIN entity ON entity.id = s.entity"
    " WHERE s.type = ? AND s.property_rank = ? AND s.language_rank = ? AND s.entailed = ? AND s.length = ?"
    " AND s.normalized >= ? AND s.normalized < ?"
    " ORDER BY s.normalized, s.value, s.entity"
)


def search(connection: sqlite3.Connection, query: SearchQuery) -> SearchResults:
    """Return a page of a query's results, ranked by §2.6, one per IRI with its best-matching value.

    Values rank by exact match first, then their name property's position (`NAME_PROPERTIES`), then their language's
    (`LANGUAGE_RANKING`), then direct before entailed, then shorter, then by normalized value, then by value (code
    point). Values longer than `KEY_CHARS` rank among themselves by their first `KEY_CHARS` characters. An IRI ranks by
    its best value; IRIs with the same best value keep an order that's arbitrary but the same for every query.

    Args:
        connection: A connection to the derived database.
        query: The query.
    """
    text = normalize(query.text)
    lanes = _lanes(connection, query)
    if not text or not lanes:
        return SearchResults((), more=False)
    wanted = query.offset + query.limit + 1  # one more, to tell whether there are more
    best: dict[str, tuple[_Match, bool]] = {}

    def take(matches: Iterator[_Match], exact: bool) -> bool:
        """Keep each new IRI's first match; return whether there are enough."""
        for match in matches:
            if match.iri not in best:
                best[match.iri] = (match, exact)
                if len(best) >= wanted:
                    return True
        return False

    tiers = [list(group) for _, group in itertools.groupby(lanes, key=_Lane.tier)]
    key_length = min(len(text), KEY_CHARS)
    with ExitStack() as cursors:

        def matches(tier: list[_Lane], length: int) -> Iterator[_Match]:
            return _tier_matches(connection, cursors, tier, text[:KEY_CHARS], length)

        exact_matches = (match for tier in tiers for match in matches(tier, key_length) if match.normalized == text)
        if not take(exact_matches, exact=True):
            for tier in tiers:
                lengths = range(min(len(text) + 1, KEY_CHARS), max(lane.max_length for lane in tier) + 1)
                prefix_matches = (
                    match
                    for length in lengths
                    for match in matches(tier, length)
                    if match.normalized.startswith(text) and match.normalized != text
                )
                if take(prefix_matches, exact=False):
                    break
    found = list(best.values())[query.offset : query.offset + query.limit]
    types = _types(connection, [match.iri for match, _ in found], query.types)
    hits = tuple(
        SearchHit(match.iri, types[match.iri], match.value, match.lane.property, match.language, exact)
        for match, exact in found
    )
    return SearchResults(hits, more=len(best) >= wanted)


def _lanes(connection: sqlite3.Connection, query: SearchQuery) -> list[_Lane]:
    """Return the lanes the query's filters allow, in tier order."""
    rows = connection.execute(
        "SELECT type, property_rank, language_rank, entailed, max_length, property FROM search_lane"
        " ORDER BY property_rank, language_rank, entailed, type"
    )
    lanes = [_Lane(*row) for row in rows]
    return [
        lane
        for lane in lanes
        if (query.types is None or lane.type in query.types)
        and (query.properties is None or lane.property in query.properties)
        and (query.entailed or not lane.entailed)
    ]


def _tier_matches(
    connection: sqlite3.Connection, cursors: ExitStack, tier: list[_Lane], key: str, length: int
) -> Iterator[_Match]:
    """Yield a tier's values of a key length whose key starts with the key, merging its lanes' in rank order.

    Each lane's rows are read as they're needed; its cursor is closed when `cursors` is. Equal keys keep their lanes'
    order, by type, as `heapq.merge` is stable.
    """
    lanes = []
    for lane in tier:
        if length <= lane.max_length:
            cursor = connection.execute(
                _MATCHES,
                (lane.type, lane.property_rank, lane.language_rank, lane.entailed, length, key, key + LAST_CHAR),
            )
            cursors.callback(cursor.close)
            lanes.append(_rows(cursor, lane))
    return heapq.merge(*lanes, key=lambda match: (match.normalized[:KEY_CHARS], match.value[:KEY_CHARS]))


def _rows(cursor: sqlite3.Cursor, lane: _Lane) -> Iterator[_Match]:
    """Yield a lane's rows as matches, with their whole normalized value and value."""
    for normalized, normalized_rest, value, value_rest, iri, language in cursor:
        yield _Match(normalized + (normalized_rest or ""), value + (value_rest or ""), iri, language, lane)


def _types(
    connection: sqlite3.Connection, iris: Sequence[str], allowed: frozenset[EntityType] | None
) -> dict[str, tuple[EntityType, ...]]:
    """Return each IRI's types that the filter allows, in `EntityType` order."""
    found: dict[str, set[EntityType]] = {iri: set() for iri in iris}
    if iris:
        placeholders = ", ".join("?" * len(iris))  # at most MAX_LIMIT
        for iri, kind in connection.execute(f"SELECT iri, type FROM entity WHERE iri IN ({placeholders})", iris):
            found[iri].add(EntityType(kind))
    return {
        iri: tuple(kind for kind in EntityType if kind in kinds and (allowed is None or kind in allowed))
        for iri, kinds in found.items()
    }
