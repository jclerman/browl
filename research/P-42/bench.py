"""Measure P-42's search index of every annotation property's literal values (research/P-42/README.md).

Usage:
    python research/P-42/bench.py fetch DATASET VALUES_DB    # where Fuseki is reachable, e.g. in the app container
    python research/P-42/bench.py build[:names|:all] VALUES_DB SCRATCH_DB [NAME_PROPERTY ...]

`fetch` reads a Fuseki dataset's entities (as `entity-list.rq` lists them, plus the ontology) and every literal value
of each annotation property on them, timing the bulk query, into VALUES_DB: `entity`, `name_value` (one row per
asserted property, all direct) and `value_stats` (counts by property, language and datatype).

`build` copies VALUES_DB into SCRATCH_DB (which it overwrites) and builds the search index there with `browl.search`'s
own code twice: over the name properties' values only (default `rdfs:label`), and over every property's, the name
properties first; `build:names` or `build:all` builds only that one, leaving it in SCRATCH_DB. A property's rank
stands for its id: one lane per (type, property, language rank, word), as P-42 keys them. Then it times queries as
`browl.search.search` runs them, over the name properties and over all.
"""

import asyncio
import os
import sqlite3
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import httpx2

from browl.derived import INDEXES, SCHEMA, EntityType
from browl.fuseki import FusekiClient
from browl.names import normalize
from browl.precache.core import PREFIXES, batched, sparql_file
from browl.precache.entities import ENTITY_TYPES
from browl.rdf import Literal
from browl.search import SearchQuery, fill_index, search

RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
BUILT_IN = [
    f"{RDFS}label",
    f"{RDFS}comment",
    f"{RDFS}seeAlso",
    f"{RDFS}isDefinedBy",
    f"{OWL}deprecated",
    f"{OWL}versionInfo",
    f"{OWL}priorVersion",
    f"{OWL}backwardCompatibleWith",
    f"{OWL}incompatibleWith",
]
"""OWL 2's built-in annotation properties, which needn't be declared."""

ANNOTATION_VALUES = f"""SELECT ?entity ?property ?value
WHERE {{
  {{ VALUES ?property {{ {" ".join(f"<{iri}>" for iri in BUILT_IN)} }} }}
  UNION {{ ?property rdf:type owl:AnnotationProperty }}
  ?entity ?property ?value .
  FILTER (isIRI(?entity) && isLiteral(?value))
}}"""
"""Every literal value of each annotation property, declared or built in, on an IRI. A built-in property that's also
declared comes twice; `store` keeps each value once."""

ONTOLOGY = "SELECT ?entity WHERE { ?entity rdf:type owl:Ontology . FILTER isIRI(?entity) }"

QUERIES = ["a", "s", "e", "ba", "hom", "homo sapiens", "sapiens", "heart", "abnormality", "ncbi", "gc_id", "x" * 40]


async def fetch(dataset: str, path: Path) -> None:
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path, isolation_level=None)
    connection.executescript(SCHEMA)
    connection.execute(
        "CREATE TEMP TABLE staging (iri TEXT NOT NULL, property TEXT NOT NULL, value TEXT NOT NULL,"
        " normalized TEXT NOT NULL, language TEXT, datatype TEXT NOT NULL)"
    )
    connection.execute("BEGIN")
    url = os.environ.get("FUSEKI_URL", "http://fuseki:3030")
    async with httpx2.AsyncClient(base_url=url, timeout=None) as http:
        client = FusekiClient(http)
        start = time.perf_counter()
        async with client.bulk_select(dataset, ONTOLOGY, PREFIXES) as stream:
            rows = [(str(row[0]), EntityType.ONTOLOGY) async for row in stream]
        async with client.bulk_select(dataset, sparql_file("entity-list.rq"), PREFIXES) as stream:
            columns = [stream.variables.index(name) for name in ("entity", "type")]
            async for row in stream:
                rows.append((str(row[columns[0]]), ENTITY_TYPES[str(row[columns[1]])]))
        connection.executemany("INSERT INTO entity (iri, type, path) VALUES (?, ?, '')", rows)
        print(f"entities: {len(rows):,} in {time.perf_counter() - start:.1f} s")
        start, staged = time.perf_counter(), 0
        async with client.bulk_select(dataset, ANNOTATION_VALUES, PREFIXES) as stream:
            columns = [stream.variables.index(name) for name in ("entity", "property", "value")]
            async for batch in batched(stream):
                staged += len(batch)
                values = []
                for row in batch:
                    literal = row[columns[2]]
                    assert isinstance(literal, Literal)
                    text = literal.value
                    values.append(
                        (
                            str(row[columns[0]]),
                            str(row[columns[1]]),
                            text,
                            normalize(text),
                            literal.language,
                            literal.datatype,
                        )
                    )
                connection.executemany("INSERT INTO staging VALUES (?, ?, ?, ?, ?, ?)", values)
        print(f"annotation values query and staging: {staged:,} rows in {time.perf_counter() - start:.1f} s")
    connection.execute("CREATE INDEX entity_iri ON entity (iri)")
    start = time.perf_counter()
    connection.execute(
        "INSERT INTO name_value (entity, property, value, normalized, language, direct)"
        " SELECT entity.id, s.property, s.value, s.normalized, s.language, 1"
        " FROM staging AS s JOIN entity ON entity.iri = s.iri"
        " GROUP BY entity.id, s.property, s.value, s.language ORDER BY entity.id"
    )
    connection.execute(
        "CREATE TABLE value_stats AS SELECT s.property, s.language, s.datatype, count(*) AS n,"
        " sum(entity.id IS NULL) AS undeclared, max(length(s.normalized)) AS max_length"
        " FROM staging AS s LEFT JOIN entity ON entity.iri = s.iri GROUP BY 1, 2, 3"
    )
    connection.execute("COMMIT")
    print(
        f"stored: {connection.execute('SELECT count(*) FROM name_value').fetchone()[0]:,} values"
        f" in {time.perf_counter() - start:.1f} s"
    )
    connection.close()


def build(source: Path, scratch: Path, names: list[str], only: str | None = None) -> None:
    connection = sqlite3.connect(source)
    stats = connection.execute(
        "SELECT property, sum(n), sum(undeclared), count(DISTINCT language), max(max_length),"
        " group_concat(DISTINCT replace(datatype, 'http://www.w3.org/2001/XMLSchema#', 'xsd:'))"
        " FROM value_stats GROUP BY property ORDER BY 2 DESC"
    ).fetchall()
    connection.close()
    print("values by property: (property, values, on undeclared IRIs, languages, longest, datatypes)")
    for row in stats:
        print(f"  {row}")
    others = sorted(row[0] for row in stats if row[0] not in names)
    for label, properties in (("names", names), ("all", names + others)):
        if only is not None and label != only:
            continue
        print(f"\n== index over {label}: {len(properties)} properties")
        scratch.unlink(missing_ok=True)
        connection = sqlite3.connect(scratch, isolation_level=None)
        connection.executescript(SCHEMA)
        connection.executescript(
            "PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF; PRAGMA cache_size = -262144; PRAGMA threads = 4;"
        )
        connection.execute("ATTACH ? AS source", (f"file:{source}?mode=ro&immutable=1",))
        connection.execute("INSERT INTO entity SELECT * FROM source.entity")
        placeholders = ", ".join("?" * len(properties))
        connection.execute(
            f"INSERT INTO name_value SELECT * FROM source.name_value WHERE property IN ({placeholders})", properties
        )
        connection.execute("DETACH source")
        for sql in INDEXES["entity"] + INDEXES["name_value"]:
            connection.execute(sql)
        start = time.perf_counter()
        connection.execute("BEGIN")
        fill_index(connection, properties, ["en", "none"])
        connection.execute("COMMIT")
        print(f"build: {time.perf_counter() - start:.1f} s")
        rows = dict(connection.execute("SELECT word, count(*) FROM search_value GROUP BY word").fetchall())
        print(f"rows: {rows.get(0, 0):,} whole, {rows.get(1, 0):,} word")
        size = connection.execute("SELECT sum(pgsize) FROM dbstat WHERE name = 'search_value'").fetchone()[0]
        print(f"search_value: {size / 2**20:.0f} MiB")
        lanes = connection.execute("SELECT word, count(*), sum(max_length) FROM search_lane GROUP BY word").fetchall()
        print(
            f"lanes (word, count, sum of max lengths = seeks for a one-character query that matches nothing): {lanes}"
        )
        shapes = Counter(connection.execute("SELECT type FROM search_lane").fetchall())
        print(f"lanes by type: { {kind[0]: n for kind, n in shapes.items()} }")
        connection.close()
        reader = sqlite3.connect(f"file:{scratch}?mode=ro&immutable=1", uri=True)
        filters = [("default", None)] if label == "names" else [("names", frozenset(names)), ("all", None)]
        for filter_label, chosen in filters:
            for text in QUERIES:
                times = []
                for _ in range(20):
                    start = time.perf_counter()
                    results = search(reader, SearchQuery(text=text, properties=chosen))
                    times.append(time.perf_counter() - start)
                print(
                    f"  {filter_label:7} {text[:16]!r:18} median {statistics.median(times) * 1000:7.2f} ms,"
                    f" max {max(times) * 1000:7.2f} ms, {len(results.hits)} hits:"
                    f" {[hit.value[:30] for hit in results.hits[:3]]}"
                )
        reader.close()


def main() -> None:
    if sys.argv[1] == "fetch":
        asyncio.run(fetch(sys.argv[2], Path(sys.argv[3])))
    else:
        names = sys.argv[4:] or [f"{RDFS}label"]
        only = sys.argv[1].partition(":")[2] or None
        build(Path(sys.argv[2]), Path(sys.argv[3]), names, only)


if __name__ == "__main__":
    main()
