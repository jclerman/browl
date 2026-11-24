"""Measure SQLite FTS5 as the search backend for prose properties (research/P-42/README.md).

Usage: python research/P-42/prose.py VALUES_DB SCRATCH_DB

VALUES_DB is `bench.py fetch`'s output. A property is prose if its median value has at least `PROSE_WORDS` words.
This builds, in SCRATCH_DB (which it overwrites), an FTS5 index of the prose properties' values, and for comparison
the lane index (`browl.search.fill_index`) of the same values and of the rest. Then it times FTS5 queries ranked by
BM25: each query word is a prefix term, all of them required.
"""

import re
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

from browl.derived import INDEXES, SCHEMA
from browl.search import fill_index

PROSE_WORDS = 8
QUERIES = [
    "a", "the", "heart", "lung", "sapiens", "epithelium", "neural crest", "muscle cell", "cardiac muscle",
    "part of the", "bone marrow", "abnormality", "increased", "zzz", "x" * 40,
]  # fmt: skip
TOKEN = re.compile(r"\w+")


def classify(connection: sqlite3.Connection) -> tuple[list[str], list[str]]:
    words: dict[str, list[int]] = defaultdict(list)
    for prop, value in connection.execute("SELECT property, value FROM name_value"):
        words[prop].append(len(value.split()))
    prose = sorted(p for p, counts in words.items() if statistics.median(counts) >= PROSE_WORDS)
    return prose, sorted(set(words) - set(prose))


def size(connection: sqlite3.Connection, pattern: str) -> float:
    return connection.execute("SELECT sum(pgsize) FROM dbstat WHERE name LIKE ?", (pattern,)).fetchone()[0] / 2**20


def lane_index(source: Path, scratch: Path, properties: list[str]) -> str:
    """Build the lane index of the properties' values in a scratch file; return its size and build time."""
    scratch.unlink(missing_ok=True)
    connection = sqlite3.connect(scratch, isolation_level=None)
    connection.executescript(SCHEMA)
    connection.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF; PRAGMA cache_size = -262144;")
    connection.execute("ATTACH ? AS source", (f"file:{source}?mode=ro&immutable=1",))
    connection.execute("INSERT INTO entity SELECT * FROM source.entity")
    marks = ", ".join("?" * len(properties))
    connection.execute(
        f"INSERT INTO name_value SELECT * FROM source.name_value WHERE property IN ({marks})", properties
    )
    connection.execute("DETACH source")
    for sql in INDEXES["entity"] + INDEXES["name_value"]:
        connection.execute(sql)
    start = time.perf_counter()
    connection.execute("BEGIN")
    fill_index(connection, properties, ["en", "none"])
    connection.execute("COMMIT")
    result = f"{size(connection, 'search_value'):.0f} MiB, built in {time.perf_counter() - start:.1f} s"
    connection.close()
    scratch.unlink()
    return result


def fts_query(text: str) -> str | None:
    """Return an FTS5 query requiring a prefix match of each of the text's words; None if it has none."""
    tokens = TOKEN.findall(text)
    return " ".join(f'"{token}"*' for token in tokens) or None


def main() -> None:
    source, scratch = Path(sys.argv[1]), Path(sys.argv[2])
    reader = sqlite3.connect(f"file:{source}?mode=ro&immutable=1", uri=True)
    prose, names = classify(reader)
    reader.close()
    print(f"prose properties ({len(prose)}): {prose}")
    lanes_scratch = scratch.with_suffix(".lanes")
    print(f"lane index, all properties: {lane_index(source, lanes_scratch, prose + names)}")
    print(f"lane index, non-prose only: {lane_index(source, lanes_scratch, names)}")
    print(f"lane index, prose only:     {lane_index(source, lanes_scratch, prose)}")

    scratch.unlink(missing_ok=True)
    connection = sqlite3.connect(scratch, isolation_level=None)
    connection.executescript(SCHEMA)
    connection.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF; PRAGMA cache_size = -262144;")
    connection.execute("ATTACH ? AS source", (f"file:{source}?mode=ro&immutable=1",))
    connection.execute("INSERT INTO entity SELECT * FROM source.entity")
    marks = ", ".join("?" * len(prose))
    connection.execute(f"INSERT INTO name_value SELECT * FROM source.name_value WHERE property IN ({marks})", prose)
    connection.execute("DETACH source")
    rows = connection.execute("SELECT count(*), sum(length(value)) FROM name_value").fetchone()
    print(f"prose values: {rows[0]:,}, {rows[1] / 2**20:.1f} MiB of text")
    start = time.perf_counter()
    connection.execute("BEGIN")
    # External content: the text stays in name_value, so FTS5 holds only its index, and highlight() reads it there.
    connection.execute(
        "CREATE VIRTUAL TABLE prose USING fts5(value, content='name_value', content_rowid='rowid',"
        " tokenize='unicode61 remove_diacritics 2', prefix='1 2 3')"
    )
    connection.execute("INSERT INTO prose (rowid, value) SELECT rowid, value FROM name_value")
    connection.execute("INSERT INTO prose (prose) VALUES ('optimize')")
    connection.execute("COMMIT")
    print(f"FTS5: built in {time.perf_counter() - start:.1f} s, {size(connection, 'prose%'):.1f} MiB")
    connection.close()

    reader = sqlite3.connect(f"file:{scratch}?mode=ro&immutable=1", uri=True)
    definition = "http://purl.obolibrary.org/obo/IAO_0000115"
    for label, extra, parameters in (("all prose", "", ()), ("definitions", " AND nv.property = ?", (definition,))):
        print(f"\n== {label}: query, matching values, median and max of 10 runs, first IRIs' values")
        for text in QUERIES:
            match = fts_query(text)
            if match is None:
                continue
            matched = reader.execute("SELECT count(*) FROM prose WHERE prose MATCH ?", (match,)).fetchone()[0]
            times = []
            for _ in range(10):
                start = time.perf_counter()
                rows = reader.execute(
                    "SELECT nv.entity, highlight(prose, 0, '[', ']') FROM prose JOIN name_value AS nv"
                    f" ON nv.rowid = prose.rowid WHERE prose MATCH ?{extra} ORDER BY rank LIMIT 100",
                    (match, *parameters),
                ).fetchall()
                seen: dict[int, str] = {}
                for entity, value in rows:  # one hit per entity, its best value
                    seen.setdefault(entity, value)
                    if len(seen) == 11:
                        break
                times.append(time.perf_counter() - start)
            first = [value[:70] for value in list(seen.values())[:2]]
            print(
                f"  {text[:16]!r:18} {matched:7,} | {statistics.median(times) * 1000:7.2f} ms"
                f" (max {max(times) * 1000:6.1f}) | {first}"
            )


if __name__ == "__main__":
    main()
