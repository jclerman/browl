"""Time P-19's search index build and queries on a derived database's name values (research/P-19/README.md).

Usage: python research/P-19/bench.py DERIVED_DB SCRATCH_DB

Copies `entity` and `name_value` from DERIVED_DB into SCRATCH_DB (which it overwrites), builds the search tables there
with `browl.search`'s own code, and times queries, as `browl.search.search` runs them.
"""

import sqlite3
import statistics
import sys
import time
from pathlib import Path

from browl.derived import INDEXES, SCHEMA, EntityType
from browl.search import SearchQuery, _fill, search

QUERIES = ["a", "s", "e", "ba", "hom", "homo sapiens", "unclassified", "candidatus", "zzz", "x" * 40]


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
    start = time.perf_counter()
    _fill(connection, ["http://www.w3.org/2000/01/rdf-schema#label"], ["en", "none"])
    print(f"build: {time.perf_counter() - start:.1f} s")
    size = connection.execute("SELECT sum(pgsize) FROM dbstat WHERE name = 'search_value'").fetchone()[0]
    print(f"search_value: {size / 2**20:.0f} MiB")
    connection.close()
    reader = sqlite3.connect(f"file:{scratch}?mode=ro&immutable=1", uri=True)
    cases = [(q, None) for q in QUERIES] + [(q, {EntityType.ANNOTATION_PROPERTY}) for q in ("a", "zzz")]
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
