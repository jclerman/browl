"""Time an SQLite FTS5 index as an alternative to P-41's word rows (research/P-41/README.md, FTS5).

Usage: python research/P-41/fts5_bench.py SCRATCH_DB

SCRATCH_DB is the file `bench.py` filled, with `entity` and `name_value`. Builds a contentless FTS5 table of the
normalized values there, which it replaces if present, and times queries that rank as §2.6 does: whole-value matches
first, then shorter, then by normalized value, ten IRIs a query.
"""

import sqlite3
import statistics
import sys
import time

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
    "sp.",
    "isolate",
    "clone",
    "strain",
]

# Matches each value whose words include the query's as a phrase, the last word as a prefix; ranked in SQL, as FTS5
# returns matches in row order. A value starts with the query if its first characters are the query.
RANKED = (
    "SELECT nv.entity, nv.value FROM fts JOIN name_value AS nv ON nv.rowid = fts.rowid"
    " WHERE fts MATCH ? AND instr(nv.normalized, ?) > 0"
    " ORDER BY substr(nv.normalized, 1, ?) <> ?, length(nv.normalized), nv.normalized, nv.value"
)


def build(connection: sqlite3.Connection, prefix: str) -> None:
    """Build the FTS5 table, with prefix indexes of the lengths given, and print its time and size."""
    connection.execute("DROP TABLE IF EXISTS fts")
    connection.execute(
        "CREATE VIRTUAL TABLE fts USING fts5(normalized, content='', detail=full,"
        f" tokenize='unicode61 remove_diacritics 0', prefix='{prefix}')"
    )
    start = time.perf_counter()
    connection.execute("BEGIN")
    connection.execute(
        "INSERT INTO fts (rowid, normalized) SELECT rowid, normalized FROM name_value WHERE normalized <> ''"
    )
    connection.execute("COMMIT")
    built = time.perf_counter() - start
    connection.execute("INSERT INTO fts (fts) VALUES ('optimize')")
    optimized = time.perf_counter() - start - built
    size = connection.execute("SELECT sum(pgsize) FROM dbstat WHERE name LIKE 'fts%'").fetchone()[0]
    print(f"prefix={prefix!r}: build {built:.1f} s, optimize {optimized:.1f} s, {size / 2**20:.0f} MiB")


def phrase(text: str) -> str:
    """Return an FTS5 query for a normalized query: its words as a phrase, the last as a prefix."""
    words = "".join(char if char.isalnum() else " " for char in text).split()
    return '"' + " ".join(words) + '" *'


def query(connection: sqlite3.Connection, text: str) -> tuple[list[str], int]:
    """Return the first ten IRIs' best values, and how many values matched."""
    best: dict[int, str] = {}
    rows = connection.execute(RANKED, (phrase(text), text, len(text), text)).fetchall()
    for entity, value in rows:
        best.setdefault(entity, value)
        if len(best) == 10:
            break
    return list(best.values()), len(rows)


def main() -> None:
    connection = sqlite3.connect(sys.argv[1], isolation_level=None)
    connection.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF; PRAGMA cache_size = -262144;")
    for prefix in sys.argv[2:] or ["1 2 3"]:
        build(connection, prefix)
        for text in QUERIES:
            times = []
            for _ in range(5):
                start = time.perf_counter()
                values, matched = query(connection, text)
                times.append(time.perf_counter() - start)
            print(
                f"  {text[:16]!r:20} median {statistics.median(times) * 1000:8.1f} ms, max {max(times) * 1000:8.1f} ms,"
                f" {matched:,} values matched: {values[:3]}"
            )


if __name__ == "__main__":
    main()
