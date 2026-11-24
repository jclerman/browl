"""Prototype two ways to make a query that matches few values cheap across many lanes (research/P-42/README.md).

Usage: python research/P-42/variants.py SCRATCH_DB SMALL_ROWS TOP_K [VARIANTS]

SCRATCH_DB is `bench.py build`'s scratch file, holding its index over every property. Both variants are timed against
`browl.search.search` as built, and must return the same hits. VARIANTS picks some, e.g. `B`; both by default.

- A, small lanes read whole: the index as built, but a lane of at most SMALL_ROWS rows is read once per query, with
  one seek, and its matches bucketed by length in Python, rather than sought at each length.
- B, ordered by value, with each busy prefix's best values: a copy of `search_value` keyed by (lane, normalized,
  value, entity, word start), so one seek finds a lane's matches of a prefix, in value order; and `search_top`, which
  holds, for each lane and each prefix that more than TOP_K of its keys start with, their best TOP_K in rank order.
  As in a compressed trie, a prefix whose keys all continue with the same character isn't stored, as the longer
  prefix has the same matches. A query reads the lane's `search_top` rows for the first stored prefix that starts
  with its text, if there's one, and after them, if it needs more, every match, sorted; otherwise it reads every
  match, at most TOP_K, and sorts them.
"""

import heapq
import itertools
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import ExitStack

import browl.search as engine
from browl.names import LAST_CHAR, normalize
from browl.search import KEY_CHARS, SearchHit, SearchQuery, SearchResults, _Lane, _Match, _with_value

LANE = "type = ? AND property_rank = ? AND language_rank = ? AND entailed = ? AND word = ?"
COLUMNS = (
    "s.normalized, s.normalized_rest, s.value, s.value_rest, entity.iri, s.entity, s.language, s.word_start, s.length"
)
NAMES = frozenset({"http://www.w3.org/2000/01/rdf-schema#label"})
QUERIES = [
    "a", "e", "s", "x", "ba", "the", "of", "hom", "heart", "lung", "bone of", "cardiac muscle", "homo sapiens",
    "sapiens", "abnormality", "ncbi", "gc_id", "uberon:0000", "zebrafish", "unclassified", "bacterium", "virus",
    "candidatus", "sp.", "zzz", "qq", "x" * 40,
]  # fmt: skip


def lane_key(lane: _Lane) -> tuple:
    return (lane.type, lane.property_rank, lane.language_rank, lane.entailed, lane.word)


def rank(row: tuple) -> tuple:
    """A row's order within a lane, as `search_value`'s key has it: length, then the cut key and value."""
    return row[8], row[0], row[2], row[5], row[7]


def as_match(row: tuple, lane: _Lane) -> _Match:
    normalized, normalized_rest, value, value_rest, iri, entity, language, word_start, _ = row
    return _Match(
        normalized + (normalized_rest or ""), value + (value_rest or ""), iri, entity, language, word_start, lane
    )


# Variant A: small lanes read whole.


def variant_a(connection: sqlite3.Connection, small_rows: int) -> Callable[[SearchQuery], SearchResults]:
    counts = {
        row[:5]: row[5]
        for row in connection.execute(
            "SELECT type, property_rank, language_rank, entailed, word, count(*) FROM search_value"
            " GROUP BY 1, 2, 3, 4, 5"
        )
    }
    cache: dict[tuple, dict[int, list[tuple]]] = {}

    def small_lane(lane: _Lane, key: str) -> dict[int, list[tuple]]:
        found = cache.get((lane_key(lane), key))
        if found is None:
            found = defaultdict(list)
            rows = connection.execute(
                f"SELECT {COLUMNS} FROM search_value AS s JOIN entity ON entity.id = s.entity"
                f" WHERE {LANE.replace('type', 's.type')}",
                lane_key(lane),
            )
            for row in rows:  # in key order, so each length's rows stay in rank order
                if row[0].startswith(key):
                    found[row[8]].append(row)
            cache[(lane_key(lane), key)] = found
        return found

    def tier_matches(connection, cursors: ExitStack, tier: list[_Lane], key: str, length: int) -> Iterator[_Match]:
        lanes = []
        for lane in tier:
            if length > lane.max_length:
                continue
            if counts[lane_key(lane)] <= small_rows:
                lanes.append(as_match(row, lane) for row in small_lane(lane, key).get(length, ()))
            else:
                cursor = connection.execute(engine._MATCHES, (*lane_key(lane), length, key, key + LAST_CHAR))
                cursors.callback(cursor.close)
                lanes.append(engine._rows(cursor, lane))
        return heapq.merge(*lanes, key=lambda match: (match.normalized[:KEY_CHARS], match.value[:KEY_CHARS]))

    def run(query: SearchQuery) -> SearchResults:
        cache.clear()
        original = engine._tier_matches
        engine._tier_matches = tier_matches
        try:
            return engine.search(connection, query)
        finally:
            engine._tier_matches = original

    return run


# Variant B: ordered by value, with each busy prefix's best values.


def build_b(connection: sqlite3.Connection, top_k: int) -> None:
    connection.executescript(
        """
        DROP TABLE IF EXISTS search_b; DROP TABLE IF EXISTS search_top;
        CREATE TABLE search_b (type TEXT NOT NULL, property_rank INTEGER NOT NULL, language_rank INTEGER NOT NULL,
            entailed INTEGER NOT NULL, word INTEGER NOT NULL, length INTEGER NOT NULL, normalized TEXT NOT NULL,
            value TEXT NOT NULL, entity INTEGER NOT NULL, normalized_rest TEXT, value_rest TEXT, language TEXT,
            word_start INTEGER NOT NULL,
            PRIMARY KEY (type, property_rank, language_rank, entailed, word, normalized, value, entity, word_start)
        ) WITHOUT ROWID;
        CREATE TABLE search_top (type TEXT NOT NULL, property_rank INTEGER NOT NULL, language_rank INTEGER NOT NULL,
            entailed INTEGER NOT NULL, word INTEGER NOT NULL, prefix TEXT NOT NULL, place INTEGER NOT NULL,
            length INTEGER NOT NULL, normalized TEXT NOT NULL, value TEXT NOT NULL, entity INTEGER NOT NULL,
            normalized_rest TEXT, value_rest TEXT, language TEXT, word_start INTEGER NOT NULL,
            PRIMARY KEY (type, property_rank, language_rank, entailed, word, prefix, place)
        ) WITHOUT ROWID;
        """
    )
    start = time.perf_counter()
    connection.execute("BEGIN")
    connection.execute(
        "INSERT INTO search_b SELECT * FROM search_value"
        " ORDER BY type, property_rank, language_rank, entailed, word, normalized, value, entity, word_start"
    )
    connection.execute("COMMIT")
    print(f"B: search_b built in {time.perf_counter() - start:.1f} s")
    start = time.perf_counter()
    connection.execute("BEGIN")
    lanes = connection.execute("SELECT type, property_rank, language_rank, entailed, word FROM search_lane").fetchall()
    stored = prefixes = deepest = 0
    for lane in lanes:
        rows = connection.execute(
            "SELECT normalized, normalized_rest, value, value_rest, NULL, entity, language, word_start, length"
            f" FROM search_b WHERE {LANE} ORDER BY normalized, value, entity, word_start",
            lane,
        ).fetchall()
        ranges = [(0, len(rows))] if len(rows) > top_k else []
        length = 0
        while ranges:
            length += 1
            busy = []
            for first, last in ranges:
                # Keys that start with one prefix of this length are contiguous; shorter keys come first.
                position = first
                while position < last and len(rows[position][0]) < length:
                    position += 1
                for prefix, group in itertools.groupby(range(position, last), key=lambda i: rows[i][0][:length]):
                    members = list(group)
                    if len(members) > top_k:
                        busy.append((members[0], members[-1] + 1))
                        keys = [rows[i][0] for i in members]
                        if all(len(k) > length for k in keys) and len({k[: length + 1] for k in keys}) == 1:
                            continue  # one longer prefix has the same matches: stored there (a compressed trie)
                        best = heapq.nsmallest(top_k, (rows[i] for i in members), key=rank)
                        connection.executemany(
                            "INSERT INTO search_top VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [
                                (*lane, prefix, place, row[8], row[0], row[2], row[5], row[1], row[3], row[6], row[7])
                                for place, row in enumerate(best)
                            ],
                        )
                        stored += len(best)
                        prefixes += 1
                        deepest = max(deepest, length)
            ranges = busy
    connection.execute("COMMIT")
    print(
        f"B: search_top built in {time.perf_counter() - start:.1f} s: {prefixes:,} prefixes,"
        f" {stored:,} rows, longest prefix {deepest}"
    )
    for table in ("search_value", "search_b", "search_top"):
        size = connection.execute("SELECT sum(pgsize) FROM dbstat WHERE name = ?", (table,)).fetchone()[0]
        print(f"   {table}: {size / 2**20:.0f} MiB")


def variant_b(connection: sqlite3.Connection, top_k: int) -> Callable[[SearchQuery], SearchResults]:
    def lane_matches(lane: _Lane, key: str) -> Iterator[tuple]:
        """Yield a lane's rows whose key starts with `key`, in rank order."""
        # A prefix that's not stored has the same matches as the first stored one that starts with it, if any.
        stored = connection.execute(
            f"SELECT prefix FROM search_top WHERE {LANE} AND prefix >= ? ORDER BY prefix LIMIT 1",
            (*lane_key(lane), key),
        ).fetchone()
        top = []
        if stored is not None and stored[0].startswith(key):
            top = connection.execute(
                f"SELECT normalized, normalized_rest, value, value_rest, entity, language, word_start, length"
                f" FROM search_top WHERE {LANE} AND prefix = ? ORDER BY place",
                (*lane_key(lane), stored[0]),
            ).fetchall()
        if top:
            for normalized, normalized_rest, value, value_rest, entity, language, word_start, length in top:
                yield normalized, normalized_rest, value, value_rest, None, entity, language, word_start, length
        rows = connection.execute(
            f"SELECT {COLUMNS.replace('entity.iri', 'NULL')} FROM search_b AS s"
            f" WHERE {LANE} AND normalized >= ? AND normalized < ?",
            (*lane_key(lane), key, key + LAST_CHAR),
        )
        if top:  # rare: past the best TOP_K of a busy prefix
            yield from sorted(rows, key=rank)[len(top) :]
        else:
            yield from sorted(rows, key=rank)

    iris: dict[int, str] = {}

    def with_iri(row: tuple, lane: _Lane) -> _Match:
        entity = row[5]
        if entity not in iris:
            iris[entity] = connection.execute("SELECT iri FROM entity WHERE id = ?", (entity,)).fetchone()[0]
        return as_match((*row[:4], iris[entity], *row[5:]), lane)

    def run(query: SearchQuery) -> SearchResults:
        iris.clear()
        text = normalize(query.text)
        lanes = engine._lanes(connection, query)
        if not text or not lanes:
            return SearchResults((), more=False)
        wanted = query.offset + query.limit + 1
        best: dict[str, tuple[_Match, bool]] = {}

        def take(matches: Iterator[_Match], exact: bool) -> bool:
            for match in matches:
                if match.iri not in best:
                    best[match.iri] = (match, exact)
                    if len(best) >= wanted:
                        return True
            return False

        tiers = [list(group) for _, group in itertools.groupby(lanes, key=_Lane.tier)]
        key = text[:KEY_CHARS]

        def exact(tier: list[_Lane]) -> Iterator[_Match]:
            lanes = []
            for lane in tier:
                rows = connection.execute(
                    f"SELECT {COLUMNS.replace('entity.iri', 'NULL')} FROM search_b AS s"
                    f" WHERE {LANE} AND normalized = ? ORDER BY value, entity, word_start",
                    (*lane_key(lane), key),
                )
                lanes.append(with_iri(row, lane) for row in rows)
            return (m for m in heapq.merge(*lanes, key=lambda m: m.value[:KEY_CHARS]) if m.normalized == text)

        def prefix(tier: list[_Lane]) -> Iterator[_Match]:
            word = tier[0].word
            lanes = [((row, lane) for row in lane_matches(lane, key)) for lane in tier]
            merged = heapq.merge(*lanes, key=lambda item: rank(item[0])[:3])
            for row, lane in merged:
                match = with_iri(row, lane)
                if match.normalized.startswith(text) and (word or match.normalized != text):
                    yield match

        whole = [tier for tier in tiers if not tier[0].word]
        if not take((match for tier in whole for match in exact(tier)), exact=True):
            for tier in tiers:
                if take(prefix(tier), exact=False):
                    break
        found = [
            (_with_value(connection, match) if match.lane.word and len(match.value) == KEY_CHARS else match, exact)
            for match, exact in list(best.values())[query.offset : query.offset + query.limit]
        ]
        types = engine._types(connection, [match.iri for match, _ in found], query.types)
        hits = tuple(
            SearchHit(m.iri, types[m.iri], m.value, m.lane.property, m.language, e, m.word_start) for m, e in found
        )
        return SearchResults(hits, more=len(best) >= wanted)

    return run


def main() -> None:
    path, small_rows, top_k = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    writer = sqlite3.connect(path, isolation_level=None)
    writer.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF; PRAGMA cache_size = -262144;")
    build_b(writer, top_k)
    writer.close()
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    sizes = [row[0] for row in connection.execute(
        "SELECT count(*) FROM search_value GROUP BY type, property_rank, language_rank, entailed, word")]  # fmt: skip
    print(f"lanes: {len(sizes)}, of at most {small_rows} rows: {sum(size <= small_rows for size in sizes)}")
    chosen_variants = sys.argv[4] if len(sys.argv) > 4 else "AB"
    variants: dict[str, Callable[[SearchQuery], SearchResults]] = {
        "as built": lambda query: engine.search(connection, query)
    }
    if "A" in chosen_variants:
        variants["A"] = variant_a(connection, small_rows)
    if "B" in chosen_variants:
        variants["B"] = variant_b(connection, top_k)
    worst: dict[str, list[float]] = defaultdict(list)
    for chosen, limit in ((None, 10), (NAMES, 10), (None, 100)):
        print(f"\n== properties: {'all' if chosen is None else 'names'}, limit {limit}")
        for text in QUERIES:
            query = SearchQuery(text=text, properties=chosen, limit=limit)
            line, expected = f"{text[:14]!r:16}", None
            for name, run in variants.items():
                times = []
                for _ in range(10):
                    start = time.perf_counter()
                    results = run(query)
                    times.append(time.perf_counter() - start)
                worst[name].extend(times)
                if expected is None:
                    expected = results
                    line += f" {len(results.hits):3} hits"
                same = "" if results == expected else " DIFFERS"
                line += f" | {name} {statistics.median(times) * 1000:7.2f} ms (max {max(times) * 1000:6.1f}){same}"
            print(line)
    for name, times in worst.items():
        times.sort()
        print(f"{name}: p95 {times[int(len(times) * 0.95)] * 1000:.1f} ms, max {times[-1] * 1000:.1f} ms")


if __name__ == "__main__":
    main()
