"""Time P-45's capped count of search results on NCBITaxon (research/P-45/README.md).

Usage: python research/P-45/bench.py OLD_DB SCRATCH_DB

OLD_DB is a derived database from before P-42 (P-41's scratch file), with `entity` and `name_value`. Its `rdfs:label`
values are copied into SCRATCH_DB (which it overwrites), in today's schema, as `annotation_value`, and the index is
built with `browl.search.fill_index`. Then each query is timed as the autocomplete runs it (10 results, no count), and
as the results page runs it (50 results, counted to `COUNT_TO`), on its first page and its last.
"""

import sqlite3
import statistics
import sys
import time
from pathlib import Path

from browl.derived import INDEXES, SCHEMA
from browl.search import SEARCH_LIMIT, SearchQuery, fill_index, search
from browl.search_page import COUNT_TO, PAGE_SIZE

LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
QUERIES = ["a", "s", "e", "ba", "hom", "homo sapiens", "sapiens", "candidatus", "bacterium", "virus", "zzz", "x" * 40]
RUNS = 20


def build(old: str, scratch: Path) -> None:
    """Copy the old database's entities and labels into a scratch database in today's schema, and index them."""
    scratch.unlink(missing_ok=True)
    connection = sqlite3.connect(scratch, isolation_level=None)
    connection.executescript(SCHEMA)
    connection.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF; PRAGMA cache_size = -262144;")
    connection.execute("ATTACH ? AS old", (f"file:{old}?mode=ro&immutable=1",))
    connection.execute("INSERT INTO entity SELECT * FROM old.entity")
    count = connection.execute(
        "INSERT INTO annotation_value SELECT entity, 1, value, language FROM old.name_value WHERE direct"
    ).rowcount
    connection.execute("DETACH old")
    connection.execute(
        "INSERT INTO search_property (id, iri, prose, by_default, value_count) VALUES (1, ?, 0, 1, ?)", (LABEL, count)
    )
    for sql in INDEXES["entity"] + INDEXES["annotation_value"]:
        connection.execute(sql)
    start = time.perf_counter()
    connection.execute("BEGIN")
    fill_index(connection, ["en", "none"])
    connection.execute("COMMIT")
    print(f"values: {count:,}, build: {time.perf_counter() - start:.1f} s")
    connection.close()


def timed(reader: sqlite3.Connection, query: SearchQuery) -> tuple[list[float], int | None]:
    """Return a query's times in ms, over `RUNS` runs, and its count."""
    times, count = [], None
    for _ in range(RUNS):
        start = time.perf_counter()
        count = search(reader, query).count
        times.append((time.perf_counter() - start) * 1000)
    return times, count


def main() -> None:
    old, scratch = sys.argv[1], Path(sys.argv[2])
    build(old, scratch)
    reader = sqlite3.connect(f"file:{scratch}?mode=ro&immutable=1", uri=True)
    print("| Query | Count | Autocomplete p50 / p95 | Page 1 p50 / p95 / max | Last page p50 / p95 |")
    print("| --- | --- | --- | --- | --- |")
    for text in QUERIES:
        suggest, _ = timed(reader, SearchQuery(text, limit=SEARCH_LIMIT))
        first, count = timed(reader, SearchQuery(text, limit=PAGE_SIZE, count_to=COUNT_TO))
        assert count is not None
        last_offset = min(max(count - 1, 0) // PAGE_SIZE, (COUNT_TO - 1) // PAGE_SIZE - 1) * PAGE_SIZE
        last, _ = timed(reader, SearchQuery(text, limit=PAGE_SIZE, offset=last_offset, count_to=COUNT_TO))
        shown = f"more than {COUNT_TO - 1:,}" if count >= COUNT_TO else f"{count:,}"

        def stats(times: list[float], top: bool = False) -> str:
            p95 = statistics.quantiles(times, n=20)[-1]
            return f"{statistics.median(times):.2f} / {p95:.2f}" + (f" / {max(times):.2f}" if top else "") + " ms"

        print(f"| `{text[:16]}` | {shown} | {stats(suggest)} | {stats(first, top=True)} | {stats(last)} |")


if __name__ == "__main__":
    main()
