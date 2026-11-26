"""Search (DESIGN.md §2.6): the `SearchBackend` interface, and its v1 implementation over SQLite, `SqliteSearch`.

Only this module knows the search engine (N-5): pre-caching calls `BACKEND.index`, and pages call a backend's `search`.
"""

import heapq
import itertools
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Generator, Iterable, Iterator, Sequence
from contextlib import ExitStack, closing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, NamedTuple

from pydantic import AfterValidator, Field
from pydantic.dataclasses import dataclass as validated_dataclass

from browl.derived import DerivedReader, EntityType
from browl.names import LAST_CHAR, language_ranks, normalize

if TYPE_CHECKING:
    from browl.precache.core import PreCacheContext

SEARCH_LIMIT = 10
"""How many results a query returns by default."""

MAX_LIMIT = 100
"""The most results a query may ask for at once."""

MAX_OFFSET = 1000
"""The most results a query may skip: paging deeper reads more of the index for each page."""

MAX_COUNT = MAX_OFFSET + MAX_LIMIT + 1
"""The most results a query may count (`SearchQuery.count_to`): those of its deepest page, and one more."""

MAX_QUERY_CHARS = 1000
"""The longest query text."""

COMMON_SHARE = 0.1
"""A name-like value on more than this share of the ontology's entities, and on at least `COMMON_MIN`, is _common_: too
common to tell entities apart, so it isn't indexed by entity, only counted (`common_value`)."""

COMMON_MIN = 1000
"""The fewest entities a common value is on, so a small ontology's values are always indexed."""

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
    """A search (U-10): values whose start or a word's matches the text (U-19), ignoring case and accents (U-11).

    A word starts after a character that isn't a letter or digit (`word_starts`),
    within a value's first `MAX_QUERY_CHARS` normalized characters.

    Attributes:
        text: What the values, or their later words, start with; leading whitespace is ignored, and a query of only
            whitespace finds nothing.
        types: The entity types to find; None for all, and an empty set for none.
        properties: The properties (IRIs) whose values to search; None for those searched by default (names, IDs and
            synonyms, `SearchProperty.by_default`), and an empty set for none. A prose property's values aren't in
            this index, so it finds nothing.
        order: The properties to walk first, in this order (§2.6, rule 3); then those searched by default, then the
            rest, each by label (`SearchProperty.label`).
        entailed: Whether values entailed through a sub-property count too, or only direct ones: a chosen property's
            sub-properties' values, transitively, rank after its own (rule 5).
        limit: How many results to return.
        offset: How many of the best results to skip, for paging.
        count_to: How many results to count, at most, for `SearchResults.count`; 0 to count none, so a query reads
            only as many as it returns. Counting reads every result up to it.
    """

    text: Annotated[str, Field(max_length=MAX_QUERY_CHARS), AfterValidator(_encodable)]
    types: frozenset[EntityType] | None = None
    properties: frozenset[str] | None = None
    order: tuple[str, ...] = ()
    entailed: bool = True
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = SEARCH_LIMIT
    offset: Annotated[int, Field(ge=0, le=MAX_OFFSET)] = 0
    count_to: Annotated[int, Field(ge=0, le=MAX_COUNT)] = 0


@dataclass(frozen=True)
class SearchHit:
    """An IRI that matches, with its best-matching value (U-12).

    Attributes:
        iri: The IRI; a punned IRI is one hit.
        types: Its types that the query's filter allows, in `EntityType` order.
        value: Its best-matching value, as written.
        property: The IRI of the property that value is asserted with.
        language: That value's language tag, lowercased; None if it has none.
        exact: Whether the value is the whole query, rather than a prefix match.
        word_start: Where in the normalized value (`browl.names.normalize`) the match starts: 0 if the value starts with
            the query, else the start of the later word that does (`word_starts`).
    """

    iri: str
    types: tuple[EntityType, ...]
    value: str
    property: str
    language: str | None
    exact: bool
    word_start: int


@dataclass(frozen=True)
class SearchProperty:
    """A property with values in the served ontology (§2.6, Every annotation property).

    Attributes:
        iri: Its IRI.
        name: Its display name; None if it isn't a declared entity (e.g. an undeclared built-in, `rdfs:label`).
        curie: Its CURIE, as its entity page gives it (`entity.curie`), or if it isn't declared, by the same rule;
            None if no prefix covers it.
        prose: Whether it's prose, rather than name-like: prose isn't in the lane index.
        by_default: Whether a query searches it when it names no properties.
        values: How many values it has, on declared entities; a property searched by default, or with sub-properties
            that have values, is listed even with none.
        sub_values: How many values its sub-properties have, transitively, which a search of it with entailed values
            also reaches.
    """

    iri: str
    name: str | None
    curie: str | None
    prose: bool
    by_default: bool
    values: int
    sub_values: int

    @property
    def label(self) -> str:
        """Return its display name, else its CURIE, else its IRI."""
        return self.name or self.curie or self.iri


@dataclass(frozen=True)
class CommonValue:
    """A common value that matches a query: on too many entities to list them, so only counted (§2.6, Common values).

    Attributes:
        value: The value, as written.
        property: The IRI of the property it's asserted with.
        counts: For each entity type it's on that the query's filter allows, in `EntityType` order: the type, how many
            entities of that type have it, and how many entities that type has.
        exact: Whether the value is the whole query, rather than a prefix match.
    """

    value: str
    property: str
    counts: tuple[tuple[EntityType, int, int], ...]
    exact: bool


@dataclass(frozen=True)
class SearchResults:
    """A page of a search's results, best first.

    Attributes:
        hits: The results.
        more: Whether there are more after them.
        count: How many results there are, up to the query's `SearchQuery.count_to`, which it is if there are as
            many or more; None if the query counts none.
        common: The common values that match, best first, which no hit lists: given with the first page only.
    """

    hits: tuple[SearchHit, ...]
    more: bool
    common: tuple[CommonValue, ...] = ()
    count: int | None = None


class SearchBackend(ABC):
    """A search engine: indexes the annotation values during pre-caching, and answers queries (N-5).

    Args:
        reader: Reads the derived database.
    """

    def __init__(self, reader: DerivedReader) -> None:
        """Read through the reader."""
        self.reader = reader

    @staticmethod
    @abstractmethod
    async def index(context: "PreCacheContext") -> None:
        """Index the new generation's name-like values, during pre-caching, once `annotation_value` is filled."""

    @abstractmethod
    async def search(self, generation: int, query: SearchQuery) -> SearchResults:
        """Return a page of a query's results in a generation, ranked by §2.6.

        Raises:
            NotCurrentError: The generation's derived database isn't current.
        """

    @abstractmethod
    async def properties(self, generation: int, order: Sequence[str] = ()) -> list[SearchProperty]:
        """Return the properties search knows in a generation, in a query's walk order (`SearchQuery.order`).

        Raises:
            NotCurrentError: The generation's derived database isn't current.
        """


class SqliteSearch(SearchBackend):
    """Search over the derived database's `search_value` table, keyed in ranking order (DESIGN.md §2.6).

    What every query reads alike (`_Fixed`) is read once per generation and kept for the newest: a generation's
    derived database never changes once it's current, and only a current one is read.
    """

    def __init__(self, reader: DerivedReader) -> None:
        """Read through the reader, with nothing kept yet."""
        super().__init__(reader)
        self._fixed: tuple[int, _Fixed] | None = None

    @staticmethod
    async def index(context: "PreCacheContext") -> None:
        """Fill `search_value` and `search_lane` from `annotation_value`'s name-like values (`fill_index`)."""
        ranking = context.job.settings.language_ranking
        await context.db.run(lambda connection: fill_index(connection, ranking))

    async def search(self, generation: int, query: SearchQuery) -> SearchResults:
        """Return a page of a query's results in a generation (`search`)."""
        return await self.reader.read(
            generation, lambda connection: search(connection, query, self._fixed_of(generation, connection))
        )

    async def properties(self, generation: int, order: Sequence[str] = ()) -> list[SearchProperty]:
        """Return the properties search knows in a generation, in walk order (`search_properties`)."""
        fixed = await self.reader.read(generation, lambda connection: self._fixed_of(generation, connection))
        return [found for _, found in _walk(fixed.properties, order)]

    def _fixed_of(self, generation: int, connection: sqlite3.Connection) -> "_Fixed":
        """Return what every query reads alike in a generation, kept from an earlier read if it was of the newest."""
        kept = self._fixed
        if kept is not None and kept[0] == generation:
            return kept[1]
        fixed = _Fixed.read(connection)
        if kept is None or kept[0] < generation:
            self._fixed = (generation, fixed)
        return fixed


BACKEND: type[SearchBackend] = SqliteSearch
"""The search engine in use, whose `index` pre-caching calls."""


def word_starts(normalized: str) -> list[int]:
    """Return where a normalized value's later words start: each alphanumeric character after one that isn't (§2.6).

    Alphanumeric is `str.isalnum`, Unicode's letters and numbers, so a change between letters and digits isn't a word
    start. The value's own start isn't listed, as its row for the whole value covers it.

    Examples:
        >>> word_starts("t-cell")
        [2]
        >>> word_starts("1,3-dimethylbutane")
        [2, 4]
        >>> word_starts("h2o")
        []
    """
    return [
        index
        for index in range(1, len(normalized))
        if normalized[index].isalnum() and not normalized[index - 1].isalnum()
    ]


def fill_index(connection: sqlite3.Connection, ranking: Sequence[str]) -> None:
    """Fill the index from `annotation_value`'s values of the properties that aren't prose, by the language ranking.

    Each value gets a row for the whole value, and a word row for each later word start (`_keys`), in its property's
    lanes. A value that normalizes to nothing (e.g. only whitespace or combining marks) is left out, as no query
    matches it. Of an entity's values that differ only in language tags that rank alike, one is kept. A common value
    (`COMMON_SHARE`) gets no rows, only its counts in `common_value` (`_fill_common`).
    """
    _fill_common(connection)
    # Keys are cut in Python, in one pass: SQLite's length() and substr() stop at a NUL, which a literal may hold, and
    # calling Python functions from SQL for each row took about 10 s longer on NCBITaxon (research/P-41).
    connection.execute(
        "CREATE TEMP TABLE search_key (source INTEGER NOT NULL, word INTEGER NOT NULL, length INTEGER NOT NULL,"
        " key TEXT NOT NULL, value TEXT NOT NULL, key_rest TEXT, value_rest TEXT, word_start INTEGER NOT NULL)"
    )
    try:
        values = connection.execute(
            "SELECT v.rowid, v.value FROM annotation_value AS v JOIN search_property AS p ON p.id = v.property"
            " WHERE NOT p.prose AND NOT EXISTS"
            "  (SELECT 1 FROM common_value AS c WHERE c.property = v.property AND c.value = v.value)"
        )
        connection.executemany("INSERT INTO search_key VALUES (?, ?, ?, ?, ?, ?, ?, ?)", _keys(values))
        tags = connection.execute(
            "SELECT DISTINCT language FROM annotation_value"
            " WHERE property IN (SELECT id FROM search_property WHERE NOT prose)"
        ).fetchall()
        with language_ranks(connection, [tag for (tag,) in tags], ranking):
            # Grouping by the key keeps one row of an entity's value in languages that rank alike, the first tag's.
            connection.execute(
                "INSERT INTO search_value"
                " SELECT entity.type, v.property, l.rank, k.word, k.length, k.key, k.value, v.entity,"
                "  min(k.key_rest), min(k.value_rest), min(v.language), k.word_start"
                " FROM search_key AS k JOIN annotation_value AS v ON v.rowid = k.source"
                " JOIN entity ON entity.id = v.entity JOIN language_rank AS l ON l.language IS v.language"
                " GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 12"
                " ORDER BY 1, 2, 3, 4, 5, 6, 7, 8, 12"
            )
            connection.execute(
                "INSERT INTO search_lane SELECT type, property, language_rank, word, max(length) FROM search_value"
                " GROUP BY type, property, language_rank, word"
            )
    finally:
        connection.execute("DROP TABLE search_key")


def _fill_common(connection: sqlite3.Connection) -> None:
    """Fill `common_value` with the common name-like values: each with, per entity type, its entities and the type's.

    A value is counted by its lexical form, whatever its languages, and its entities are `entity` rows, as are the
    total's: a punned IRI counts once per type, as the counts shown are per type. Found by one pass over the name-like
    values, grouped, then one more over every value, joined to the common ones, and to `entity` for the types.
    """
    total = connection.execute("SELECT count(*) FROM entity").fetchone()[0]
    least = max(COMMON_MIN, int(total * COMMON_SHARE) + 1)
    common = connection.execute(
        "SELECT v.property, v.value FROM annotation_value AS v JOIN search_property AS p ON p.id = v.property"
        " WHERE NOT p.prose GROUP BY v.property, v.value HAVING count(DISTINCT v.entity) >= ?",
        (least,),
    ).fetchall()
    if not common:
        return
    of_type = dict(connection.execute("SELECT type, count(*) FROM entity GROUP BY type").fetchall())
    connection.execute("CREATE TEMP TABLE common_key (property INTEGER NOT NULL, value TEXT NOT NULL)")
    try:
        connection.executemany("INSERT INTO common_key VALUES (?, ?)", common)
        rows = connection.execute(
            "SELECT v.property, v.value, entity.type, count(DISTINCT v.entity) FROM annotation_value AS v"
            " JOIN common_key AS k ON k.property = v.property AND k.value = v.value"
            " JOIN entity ON entity.id = v.entity GROUP BY 1, 2, 3"
        ).fetchall()
    finally:
        connection.execute("DROP TABLE common_key")
    connection.executemany(
        "INSERT INTO common_value VALUES (?, ?, ?, ?, ?, ?)",
        sorted((p, value, kind, normalize(value), n, of_type[kind]) for p, value, kind, n in rows),
    )


type _Key = tuple[int, bool, int, str, str, str | None, str | None, int]


def _keys(values: Iterator[tuple[int, str]]) -> Iterator[_Key]:
    """Yield the index's keys of `annotation_value` rows: the whole value's, then one per later word start.

    A word row's key is the normalized value from its word on, at most `MAX_QUERY_CHARS` characters, as no query
    compares more, and it holds only the value's first `KEY_CHARS` characters; a hit reads the rest from
    `annotation_value`. Only word starts within the first `MAX_QUERY_CHARS` normalized characters get one, so a value's
    rows hold at most about `MAX_QUERY_CHARS` ** 2 / 2 characters, however long it is. A value that normalizes to
    nothing gets none.

    Args:
        values: The rows, as (row id, value).

    Yields:
        Keys as (row id, word, key length, key, value, rest of key, rest of value, word start), with at most
        `KEY_CHARS` characters of the key and of the value, and the rest None if there's none.
    """
    for source, value in values:
        normalized = normalize(value)
        if not normalized:
            continue
        yield source, False, min(len(normalized), KEY_CHARS), *_cut(normalized, value), 0
        for start in word_starts(normalized[:MAX_QUERY_CHARS]):
            key = normalized[start : start + MAX_QUERY_CHARS]
            yield source, True, min(len(key), KEY_CHARS), *_cut(key, value[:KEY_CHARS]), start


def _cut(key: str, value: str) -> tuple[str, str, str | None, str | None]:
    """Return a key's and a value's first `KEY_CHARS` characters, then the rest of each; None if there's none."""
    return key[:KEY_CHARS], value[:KEY_CHARS], key[KEY_CHARS:] or None, value[KEY_CHARS:] or None


class _Lane(NamedTuple):
    """A lane of `search_value`: its rows of one type, property, language rank and word, and its place in a query.

    Its values are stored by length, then normalized value, then value, so a query reads its matches of one length in
    rank order.

    Attributes:
        type: Its entities' type.
        property: Its property's id (`search_property.id`).
        language_rank: Its languages' rank by `LANGUAGE_RANKING`, from 0; languages that rank alike share one.
        word: Whether its rows are word rows, keyed from a later word start, rather than whole values.
        max_length: Its longest normalized value's length, at most `KEY_CHARS`.
        iri: Its property's IRI.
        rank: Its place in the query's walk: the position of the chosen property it's searched for (rule 3).
        entailed: Whether it's searched as a sub-property of that chosen property, rather than as the property itself.
    """

    type: EntityType
    property: int
    language_rank: int
    word: bool
    max_length: int
    iri: str
    rank: int
    entailed: bool

    def tier(self) -> tuple[bool, int, int, bool]:
        """Return its rank among the query's lanes (§2.6, rules 2-5); a tier's lanes tie."""
        return self.word, self.rank, self.language_rank, self.entailed


class _Match(NamedTuple):
    normalized: str
    value: str
    iri: str
    entity: int
    language: str | None
    word_start: int
    lane: _Lane


_MATCHES = (
    "SELECT s.normalized, s.normalized_rest, s.value, s.value_rest, entity.iri, s.entity, s.language, s.word_start"
    " FROM search_value AS s JOIN entity ON entity.id = s.entity"
    " WHERE s.type = ? AND s.property = ? AND s.language_rank = ? AND s.word = ? AND s.length = ?"
    " AND s.normalized >= ? AND s.normalized < ?"
    " ORDER BY s.normalized, s.value, s.entity, s.word_start"
)


def search(connection: sqlite3.Connection, query: SearchQuery, fixed: "_Fixed | None" = None) -> SearchResults:
    """Return a page of a query's results, ranked by §2.6, one per IRI with its best-matching value.

    Values rank by §2.6's rules, a word match as the normalized value from its word on. Values longer than `KEY_CHARS`
    rank among themselves by their first `KEY_CHARS` characters. An IRI ranks by its best value; IRIs with the same
    best value keep an order that's arbitrary but the same for every query.

    Args:
        connection: A connection to the derived database.
        query: The query.
        fixed: What every query reads alike in the database, if it's been read (`_Fixed`); else it's read.
    """
    text = normalize(query.text)
    none = 0 if query.count_to else None
    if not text:
        return SearchResults((), more=False, count=none)
    if fixed is None:
        fixed = _Fixed.read(connection)
    lanes, places, iris = _lanes(fixed, query)
    common = _common(fixed, query, text, places, iris) if query.offset == 0 else ()
    if not lanes:
        return SearchResults((), more=False, common=common, count=none)
    wanted = max(query.offset + query.limit + 1, query.count_to)  # one more, to tell whether there are more
    best: dict[str, tuple[_Match, bool]] = {}

    def take(matches: Generator[_Match], exact: bool) -> bool:
        """Keep each new IRI's first match; return whether there are enough.

        The matches are closed when it returns, so the cursors of a length left part-read are closed then.
        """
        with closing(matches):
            for match in matches:
                if match.iri not in best:
                    best[match.iri] = (match, exact)
                    if len(best) >= wanted:
                        return True
        return False

    tiers = [list(group) for _, group in itertools.groupby(lanes, key=_Lane.tier)]
    key_length = min(len(text), KEY_CHARS)

    def matches(tier: list[_Lane], length: int) -> Iterator[_Match]:
        return _tier_matches(connection, tier, text[:KEY_CHARS], length)

    whole = [tier for tier in tiers if not tier[0].word]
    exact_matches = (match for tier in whole for match in matches(tier, key_length) if match.normalized == text)
    if not take(exact_matches, exact=True):
        for tier in tiers:
            # A word row whose key is the query itself isn't an exact match, as the value is longer.
            word = tier[0].word
            first = len(text) if word else len(text) + 1
            lengths = range(min(first, KEY_CHARS), max(lane.max_length for lane in tier) + 1)
            prefix_matches = (
                match
                for length in lengths
                for match in matches(tier, length)
                if match.normalized.startswith(text) and (word or match.normalized != text)
            )
            if take(prefix_matches, exact=False):
                break
    found = [
        (_with_value(connection, match) if match.lane.word and len(match.value) == KEY_CHARS else match, exact)
        for match, exact in list(best.values())[query.offset : query.offset + query.limit]
    ]
    types = _types(connection, [match.iri for match, _ in found], query.types)
    hits = tuple(
        SearchHit(match.iri, types[match.iri], match.value, match.lane.iri, match.language, exact, match.word_start)
        for match, exact in found
    )
    more = len(best) > query.offset + query.limit
    count = min(len(best), query.count_to) if query.count_to else None
    return SearchResults(hits, more=more, common=common, count=count)


def _with_value(connection: sqlite3.Connection, match: _Match) -> _Match:
    """Return a word row's match with its whole value, of which word rows hold the first `KEY_CHARS` characters.

    It's read from `annotation_value`: of the entity's values with the row's property and language, the first by value
    that starts with the row's and whose normalized value has the key at the word's start.
    """
    rows = connection.execute(
        "SELECT value FROM annotation_value WHERE entity = ? AND property = ? AND language IS ? ORDER BY value",
        (match.entity, match.lane.property, match.language),
    )
    for (value,) in rows:
        if value.startswith(match.value) and normalize(value).startswith(match.normalized, match.word_start):
            return match._replace(value=value)
    return match  # not reached: the row was built from such a value


@dataclass(frozen=True)
class _Fixed:
    """What every query reads alike of a derived database, which never changes once built.

    Attributes:
        properties: The properties search knows, with their ids, in no order.
        subs: Each property's sub-properties' ids, transitively, by id (`sub_property`).
        lanes: Every lane, as (type, property id, language rank, word, longest length) (`search_lane`).
        common: Every common value, as (property id, value, type, normalized value, entities, entities of the type)
            (`common_value`).
    """

    properties: tuple[tuple[int, SearchProperty], ...]
    subs: dict[int, tuple[int, ...]]
    lanes: tuple[tuple[str, int, int, int, int], ...]
    common: tuple[tuple[int, str, str, str, int, int], ...]

    @classmethod
    def read(cls, connection: sqlite3.Connection) -> "_Fixed":
        """Read it from the database.

        A property's count of its sub-properties' values counts only name-like ones', which a search of it reaches.
        """
        sub_values = dict(
            connection.execute(
                "SELECT s.property, sum(p.value_count) FROM sub_property AS s JOIN search_property AS p ON p.id = s.sub"
                " WHERE NOT p.prose GROUP BY s.property"
            ).fetchall()
        )
        rows = connection.execute(
            "SELECT p.id, p.iri, e.display_name, coalesce(e.curie, p.curie), p.prose, p.by_default, p.value_count"
            " FROM search_property AS p"
            " LEFT JOIN entity AS e ON e.id = (SELECT id FROM entity WHERE entity.iri = p.iri ORDER BY type LIMIT 1)"
        )
        properties = tuple(
            (id_, SearchProperty(iri, name, curie, bool(prose), bool(default), count, sub_values.get(id_, 0)))
            for id_, iri, name, curie, prose, default, count in rows
        )
        subs: dict[int, list[int]] = {}
        for property_id, sub in connection.execute("SELECT property, sub FROM sub_property"):
            subs.setdefault(property_id, []).append(sub)
        return cls(
            properties,
            {property_id: tuple(found) for property_id, found in subs.items()},
            tuple(connection.execute("SELECT type, property, language_rank, word, max_length FROM search_lane")),
            tuple(connection.execute("SELECT property, value, type, normalized, entities, of_type FROM common_value")),
        )


def search_properties(connection: sqlite3.Connection, order: Sequence[str] = ()) -> list[SearchProperty]:
    """Return the properties search knows, in a query's walk order (`SearchQuery.order`).

    Those in the order come first, in it; then those searched by default; then the others; each group by label
    (`SearchProperty.label`), case ignored, then IRI.
    """
    return [found for _, found in _walk(_Fixed.read(connection).properties, order)]


def _walk(properties: Iterable[tuple[int, SearchProperty]], order: Sequence[str]) -> list[tuple[int, SearchProperty]]:
    """Return properties, with their ids, in walk order (`search_properties`)."""
    first = {iri: n for n, iri in enumerate(dict.fromkeys(order))}

    def place(item: tuple[int, SearchProperty]) -> tuple[int, bool, str, str]:
        found = item[1]
        return first.get(found.iri, len(first)), not found.by_default, found.label.casefold(), found.iri

    return sorted(properties, key=place)


type _Places = dict[int, tuple[int, bool]]
"""Each property a query searches, by id, with its place in the walk: its rank, and whether it's entailed."""


def _lanes(fixed: _Fixed, query: SearchQuery) -> tuple[list[_Lane], _Places, dict[int, str]]:
    """Return the lanes the query's filters allow, in tier order, and the properties it searches, with their IRIs.

    Each chosen property's lanes take its place in the walk; with entailed values, so do its sub-properties' lanes,
    after its own (rule 5). A lane reached through several chosen properties takes its best place.
    """
    properties = _walk(fixed.properties, query.order)
    ids = {found.iri: id_ for id_, found in properties}
    iris = {id_: found.iri for id_, found in properties}
    rank = {id_: n for n, (id_, _) in enumerate(properties)}
    if query.properties is None:
        chosen = [id_ for id_, found in properties if found.by_default]
    else:
        chosen = [ids[iri] for iri in query.properties if iri in ids]
    places = {id_: (rank[id_], False) for id_ in chosen}
    if query.entailed:
        for chosen_id in chosen:
            for sub in fixed.subs.get(chosen_id, ()):
                places[sub] = min(places.get(sub, (rank[chosen_id], True)), (rank[chosen_id], True))
    lanes = [
        _Lane(EntityType(kind), property_id, language_rank, bool(word), max_length, iris[property_id], *place)
        for kind, property_id, language_rank, word, max_length in fixed.lanes
        if (place := places.get(property_id)) is not None and (query.types is None or kind in query.types)
    ]
    return sorted(lanes, key=lambda lane: (*lane.tier(), lane.type)), places, iris


def _common(
    fixed: _Fixed, query: SearchQuery, text: str, places: _Places, iris: dict[int, str]
) -> tuple[CommonValue, ...]:
    """Return the common values that match a normalized query, of the properties and types it searches.

    They match as indexed values do, from the start of the value or of a later word, and rank exact first, then by the
    property's place, then by normalized value and value.
    """
    found: dict[tuple[int, str], list[tuple[EntityType, int, int]]] = {}
    keys: dict[tuple[int, str], str] = {}
    for property_id, value, kind, normalized, entities, of_type in fixed.common:
        if property_id not in places or (query.types is not None and kind not in query.types):
            continue
        starts = [0, *word_starts(normalized[:MAX_QUERY_CHARS])]
        if any(normalized.startswith(text, start) for start in starts):
            found.setdefault((property_id, value), []).append((EntityType(kind), entities, of_type))
            keys[property_id, value] = normalized
    order = list(EntityType)

    def rank(key: tuple[int, str]) -> tuple[bool, tuple[int, bool], str, str]:
        return keys[key] != text, places[key[0]], keys[key], key[1]

    return tuple(
        CommonValue(
            value,
            iris[property_id],
            tuple(sorted(found[property_id, value], key=lambda count: order.index(count[0]))),
            keys[property_id, value] == text,
        )
        for property_id, value in sorted(found, key=rank)
    )


def _tier_matches(connection: sqlite3.Connection, tier: list[_Lane], key: str, length: int) -> Generator[_Match]:
    """Yield a tier's values of a key length whose key starts with the key, merging its lanes' in rank order.

    Each lane's rows are read as they're needed; its cursor is closed once they're all read, or the generator is
    closed. Equal keys keep their lanes' order, by type, as `heapq.merge` is stable.
    """
    with ExitStack() as cursors:
        lanes = []
        for lane in tier:
            if length <= lane.max_length:
                cursor = connection.execute(
                    _MATCHES,
                    (lane.type, lane.property, lane.language_rank, lane.word, length, key, key + LAST_CHAR),
                )
                cursors.callback(cursor.close)
                lanes.append(_rows(cursor, lane))
        yield from heapq.merge(*lanes, key=lambda match: (match.normalized[:KEY_CHARS], match.value[:KEY_CHARS]))


def _rows(cursor: sqlite3.Cursor, lane: _Lane) -> Iterator[_Match]:
    """Yield a lane's rows as matches, with their whole key (a word row's from its word on) and value."""
    for normalized, normalized_rest, value, value_rest, iri, entity, language, word_start in cursor:
        whole_key, whole_value = normalized + (normalized_rest or ""), value + (value_rest or "")
        yield _Match(whole_key, whole_value, iri, entity, language, word_start, lane)


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
