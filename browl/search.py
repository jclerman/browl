"""Search (DESIGN.md §2.6): the `SearchBackend` interface, and its v1 implementation over SQLite, `SqliteSearch`.

Only this module knows the search engine (N-5): pre-caching calls `BACKEND.index`, and pages call a backend's `search`.
"""

import heapq
import itertools
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic.dataclasses import dataclass as validated_dataclass

from browl.derived import DerivedReader, EntityType
from browl.names import BLANK, language_rank, name_property_iris, normalize

if TYPE_CHECKING:
    from browl.precache.core import PreCacheContext

SEARCH_LIMIT = 10
"""How many results a query returns by default."""

_LAST_CHAR = "\U0010ffff"  # sorts after any other character, as UTF-8 bytes too


@dataclass(frozen=True)
class SearchQuery:
    """A search (U-10): name values that start with the text, ignoring case and accents (U-11).

    Attributes:
        text: What the values start with; leading whitespace is ignored, and a query of only whitespace finds nothing.
        types: The entity types to find; None for all.
        properties: The name properties (IRIs) whose values to search; None for all.
        entailed: Whether values entailed through a sub-property count too, or only direct ones.
        limit: How many results to return.
        offset: How many of the best results to skip, for paging.
    """

    text: str
    types: Collection[EntityType] | None = None
    properties: Collection[str] | None = None
    entailed: bool = True
    limit: int = SEARCH_LIMIT
    offset: int = 0


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

    hits: list[SearchHit]
    more: bool


class SearchBackend(ABC):
    """A search engine: indexes the name values during pre-caching, and answers queries (N-5)."""

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
    """Search over the derived database's `search_value` table (DESIGN.md §2.6).

    The table's key is the ranking's order (type, property rank, language rank, entailed, length, normalized, value,
    entity), so a query needs no sort of its matches, which on a large ontology are hundreds of thousands for a
    one-letter query. Each lane, a (type, property rank, language rank, entailed) group, is ordered by length, then by
    value, so a query seeks each lane's matches of one length at a time, shortest first, and stops once it has enough
    IRIs.

    Args:
        reader: Reads the derived database.
    """

    def __init__(self, reader: DerivedReader) -> None:
        """Read through the reader."""
        self.reader = reader

    @staticmethod
    async def index(context: "PreCacheContext") -> None:
        """Fill `search_value` and `search_lane` from `name_value`, with each value's ranks by the current settings.

        A blank value (`browl.names.BLANK`) is left out, as its hit would have no text.
        """
        settings = context.job.settings
        await context.db.run(
            lambda connection: _fill(connection, name_property_iris(settings), settings.language_ranking)
        )

    async def search(self, generation: int, query: SearchQuery) -> SearchResults:
        """Return a page of a query's results in a generation (`search`)."""
        return await self.reader.read(generation, lambda connection: search(connection, query))


BACKEND: type[SearchBackend] = SqliteSearch
"""The search engine in use, whose `index` pre-caching calls."""


def _fill(connection: sqlite3.Connection, properties: Sequence[str], ranking: Sequence[str]) -> None:
    languages = [row[0] for row in connection.execute("SELECT DISTINCT language FROM name_value")]
    ranks = sorted({language_rank(tag, ranking) for tag in languages})
    connection.execute("CREATE TEMP TABLE search_property (property TEXT PRIMARY KEY, position INTEGER NOT NULL)")
    connection.executemany("INSERT INTO search_property VALUES (?, ?)", [(iri, n) for n, iri in enumerate(properties)])
    connection.execute("CREATE TEMP TABLE search_language (language TEXT, position INTEGER NOT NULL)")
    connection.executemany(
        "INSERT INTO search_language VALUES (?, ?)",
        [(tag, ranks.index(language_rank(tag, ranking))) for tag in languages],
    )
    connection.execute(
        "INSERT INTO search_value"
        " SELECT entity.type, p.position, l.position, 1 - nv.direct, length(nv.normalized), nv.normalized, nv.value,"
        "  nv.entity, nv.language"
        " FROM name_value AS nv JOIN entity ON entity.id = nv.entity"
        " JOIN search_property AS p USING (property) JOIN search_language AS l ON l.language IS nv.language"
        " WHERE trim(nv.value, ?) <> ''"
        " ORDER BY 1, 2, 3, 4, 5, 6, 7, 8",
        (BLANK,),
    )
    connection.execute(
        "INSERT INTO search_lane"
        " SELECT type, property_rank, language_rank, entailed, max(length), p.property"
        " FROM search_value JOIN search_property AS p ON p.position = property_rank"
        " GROUP BY type, property_rank, language_rank, entailed"
    )
    connection.execute("DROP TABLE search_property")
    connection.execute("DROP TABLE search_language")


@validated_dataclass(frozen=True)
class _Lane:
    """A lane of `search_value`: its values of one type, name property rank, language rank, and direct or entailed.

    A lane's values are stored by length, then normalized value, then value, so a query reads its matches of one length
    in rank order. Lanes are read from `search_lane`, and Pydantic checks that each attribute gets its annotated type.

    Attributes:
        type: Its entities' type.
        property_rank: Its name property's position in `NAME_PROPERTIES`, from 0.
        language_rank: Its languages' rank by `LANGUAGE_RANKING`, from 0; languages that rank alike share one.
        entailed: Whether its values are entailed through a sub-property, rather than direct.
        max_length: Its longest normalized value's length.
        name_property: Its name property's IRI.
    """

    type: EntityType
    property_rank: int
    language_rank: int
    entailed: bool
    max_length: int
    name_property: str

    @property
    def tier(self) -> tuple[int, int, bool]:
        """Return its rank among lanes (§2.6, rules 2-4): lanes of one tier rank alike."""
        return self.property_rank, self.language_rank, self.entailed


type _Match = tuple[str, str, str, str | None, _Lane]  # normalized, value, IRI, language tag, lane

_MATCHES = (
    "SELECT s.normalized, s.value, entity.iri, s.language FROM search_value AS s JOIN entity ON entity.id = s.entity"
    " WHERE s.type = ? AND s.property_rank = ? AND s.language_rank = ? AND s.entailed = ? AND s.length = ?"
    " AND s.normalized >= ? AND s.normalized < ?"
    " ORDER BY s.normalized, s.value, s.entity"
)


def search(connection: sqlite3.Connection, query: SearchQuery) -> SearchResults:
    """Return a page of a query's results, ranked by §2.6, one per IRI with its best-matching value.

    Values rank by exact match first, then their name property's position (`NAME_PROPERTIES`), then their language's
    (`LANGUAGE_RANKING`), then direct before entailed, then shorter, then by normalized value, then by value (code
    point). An IRI ranks by its best value.

    Args:
        connection: A connection to the derived database.
        query: The query.
    """
    text = normalize(query.text).lstrip()
    wanted = query.offset + query.limit + 1  # one more, to tell whether there are more
    lanes = _lanes(connection, query)
    if not text or not lanes or query.limit <= 0:
        return SearchResults([], more=False)
    best: dict[str, tuple[_Match, bool]] = {}

    def take(matches: Iterator[_Match], exact: bool) -> bool:
        """Keep each new IRI's first match; return whether there are enough."""
        for match in matches:
            if match[2] not in best:
                best[match[2]] = (match, exact)
                if len(best) >= wanted:
                    return True
        return False

    tiers = [list(group) for _, group in itertools.groupby(lanes, key=lambda lane: lane.tier)]
    exact = (_tier_matches(connection, tier, text, len(text)) for tier in tiers)  # its length: only the text itself
    if not take(itertools.chain.from_iterable(exact), exact=True):
        for tier in tiers:
            if any(
                take(_tier_matches(connection, tier, text, length), exact=False)
                for length in range(len(text) + 1, max(lane.max_length for lane in tier) + 1)
            ):
                break
    found = list(best.values())[query.offset : query.offset + query.limit]
    types = _types(connection, [match[2] for match, _ in found], query.types)
    hits = [
        SearchHit(iri, types[iri], value, lane.name_property, language, exact)
        for (_, value, iri, language, lane), exact in found
    ]
    return SearchResults(hits, more=len(best) >= wanted)


def _lanes(connection: sqlite3.Connection, query: SearchQuery) -> list[_Lane]:
    """Return the lanes the query's filters allow, in tier order."""
    lanes = [
        _Lane(*row)
        for row in connection.execute(
            "SELECT type, property_rank, language_rank, entailed, max_length, property FROM search_lane"
            " ORDER BY property_rank, language_rank, entailed, type"
        )
    ]
    return [
        lane
        for lane in lanes
        if (query.types is None or lane.type in query.types)
        and (query.properties is None or lane.name_property in query.properties)
        and (query.entailed or not lane.entailed)
    ]


def _lane_matches(connection: sqlite3.Connection, lane: _Lane, text: str, length: int) -> Iterator[_Match]:
    """Yield a lane's values of a length that start with the text, in rank order, read as they're needed."""
    rows = connection.execute(
        _MATCHES, (lane.type, lane.property_rank, lane.language_rank, lane.entailed, length, text, text + _LAST_CHAR)
    )
    return ((normalized, value, iri, language, lane) for normalized, value, iri, language in rows)


def _tier_matches(connection: sqlite3.Connection, tier: list[_Lane], text: str, length: int) -> Iterator[_Match]:
    """Yield a tier's values of a length that start with the text, merging its lanes' in rank order.

    Equal values keep their lanes' order, by type, as `heapq.merge` is stable.
    """
    lanes = [_lane_matches(connection, lane, text, length) for lane in tier if length <= lane.max_length]
    return heapq.merge(*lanes, key=lambda match: match[:2])


def _types(
    connection: sqlite3.Connection, iris: Sequence[str], allowed: Collection[EntityType] | None
) -> dict[str, tuple[EntityType, ...]]:
    """Return each IRI's types that the filter allows, in `EntityType` order."""
    order = {kind: n for n, kind in enumerate(EntityType)}
    types: dict[str, list[EntityType]] = {iri: [] for iri in iris}
    for iri in iris:
        for (kind,) in connection.execute("SELECT type FROM entity WHERE iri = ?", (iri,)):
            if allowed is None or kind in allowed:
                types[iri].append(EntityType(kind))
    return {iri: tuple(sorted(kinds, key=order.__getitem__)) for iri, kinds in types.items()}
