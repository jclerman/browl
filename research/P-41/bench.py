"""Time P-41's search index, with word rows, on a derived database's name values (research/P-41/README.md).

Usage: python research/P-41/bench.py DERIVED_DB SCRATCH_DB

Copies `entity` and `name_value` from DERIVED_DB into SCRATCH_DB (which it overwrites), times finding the word starts
two ways, builds the search tables there with `browl.search`'s own code, and times queries as `browl.search.search`
runs them.
"""

import re
import sqlite3
import statistics
import sys
import time
from pathlib import Path

from browl.derived import INDEXES, SCHEMA, EntityType
from browl.search import SearchQuery, fill_index, search, word_starts

QUERIES = [
    "a",
    "s",
    "e",
    "ba",
    "hom",
    "homo sapiens",
    "sapiens",
    "unclassified",
    "candidatus",
    "bacterium",
    "virus",
    "zzz",
    "x" * 40,
]

# Python's \w is str.isalnum() and "_", so this finds the same starts as `word_starts`, looping in C.
WORD_START = re.compile(r"(?<=[\W_])[^\W_]")


def regex_word_starts(normalized: str) -> list[int]:
    """Return a value's later word starts, by regular expression."""
    return [match.start() for match in WORD_START.finditer(normalized)]


def time_word_starts(connection: sqlite3.Connection) -> None:
    """Time both ways of finding word starts over every normalized value, and check that they agree."""
    values = [row[0] for row in connection.execute("SELECT normalized FROM name_value")]
    for name, function in (("loop", word_starts), ("regex", regex_word_starts)):
        start = time.perf_counter()
        for value in values:
            function(value)
        print(f"word starts, {name}: {time.perf_counter() - start:.1f} s")
    differ = sum(word_starts(value) != regex_word_starts(value) for value in values)
    words = sum(len(word_starts(value)) for value in values)
    print(f"values: {len(values):,}, later word starts: {words:,}, where the two differ: {differ}")


def main() -> None:
    source, scratch = sys.argv[1], Path(sys.argv[2])
    scratch.unlink(missing_ok=True)
    connection = sqlite3.connect(scratch, isolation_level=None)
    connection.executescript(SCHEMA)
    connection.executescript(
        "PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF; PRAGMA cache_size = -262144; PRAGMA threads = 4;"
    )
    connection.execute("ATTACH ? AS source", (f"file:{source}?mode=ro&immutable=1",))
    connection.execute("INSERT INTO entity SELECT * FROM source.entity")
    connection.execute("INSERT INTO name_value SELECT * FROM source.name_value")
    connection.execute("DETACH source")
    for sql in INDEXES["entity"] + INDEXES["name_value"]:
        connection.execute(sql)
    time_word_starts(connection)
    start = time.perf_counter()
    connection.execute("BEGIN")  # as pre-caching does: a statement per row outside one would commit each
    fill_index(connection, ["http://www.w3.org/2000/01/rdf-schema#label"], ["en", "none"])
    connection.execute("COMMIT")
    print(f"build: {time.perf_counter() - start:.1f} s")
    for word, rows in connection.execute("SELECT word, count(*) FROM search_value GROUP BY word"):
        print(f"{'word' if word else 'whole'} rows: {rows:,}")
    size = connection.execute("SELECT sum(pgsize) FROM dbstat WHERE name = 'search_value'").fetchone()[0]
    print(f"search_value: {size / 2**20:.0f} MiB")
    lanes = connection.execute("SELECT type, word, max_length FROM search_lane ORDER BY 1, 2").fetchall()
    print(f"lanes: {lanes}")
    connection.close()
    reader = sqlite3.connect(f"file:{scratch}?mode=ro&immutable=1", uri=True)
    cases = [(q, None) for q in QUERIES] + [(q, frozenset({EntityType.ANNOTATION_PROPERTY})) for q in ("a", "zzz")]
    for text, types in cases:
        times = []
        for _ in range(20):
            start = time.perf_counter()
            results = search(reader, SearchQuery(text, types=types))
            times.append(time.perf_counter() - start)
        label = f"{text[:16]!r} {sorted(types) if types else ''}"
        print(
            f"{label:40} median {statistics.median(times) * 1000:6.2f} ms, max {max(times) * 1000:6.2f} ms,"
            f" {len(results.hits)} hits: {[hit.value for hit in results.hits[:3]]}"
        )


if __name__ == "__main__":
    main()
