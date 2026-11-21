"""Measure the Usage tab's queries against a Fuseki serving a store as /gen-1, with data/NAME.sqlite from precache.py
and ownermap.py.

Usage: poetry run python research/S-7/bench.py URL NAME COMMAND [options]

Commands (each prints JSON, and writes it to data/NAME-COMMAND.json):

- census: every IRI's usage count by source (as object, as predicate, through the owner map), into `usage_size`, from
  two bulk queries; and the usages whose subject isn't in `entity` (question 8).
- counts: time the per-entity count queries for the largest entities and a random sample (question 2).
- sweep: stream and sort every usage of each entity with more than --min usages (question 3), recording where the
  page-1 and deep-page walks would stop (questions 4 and 5), and build the usage cache for the largest (question 6).
- walk: time the walk in name order for chosen entities, pages and batch sizes (question 4).
- pages: time fetching, parsing and rendering pages of 50 usages through the fragment path (question 7).
"""

import argparse
import asyncio
import json
import random
import sqlite3
import statistics
import time
from collections import Counter
from pathlib import Path

import httpx2

from browl import axioms as browl_axioms
from browl import owlapi
from browl.fuseki import FusekiClient
from browl.rdf import ntriples_iris

DATA = Path(__file__).parent / "data"
ROBOT_JAR = Path(__file__).parents[2] / ".cache" / "robot.jar"
DATASET = "gen-1"
PAGE = 50
OWL = "http://www.w3.org/2002/07/owl#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
PREFIXES = f"PREFIX owl: <{OWL}>\nPREFIX rdf: <{RDF}>\nPREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
DECLARATION_TYPES = (
    " ".join(
        f"owl:{name}"
        for name in ("Class", "ObjectProperty", "DatatypeProperty", "AnnotationProperty", "NamedIndividual")
    )
    + " rdfs:Datatype <http://www.w3.org/2003/11/swrl#Variable>"
)
PROPERTY_TYPES = ("object_property", "data_property", "annotation_property")

BY_OBJECT = "SELECT ?o (COUNT(*) AS ?n) WHERE { ?s ?p ?o FILTER (isIRI(?s) && isIRI(?o) && ?s != ?o) } GROUP BY ?o"
BY_PREDICATE = "SELECT ?p (COUNT(*) AS ?n) WHERE { ?s ?p ?o FILTER (isIRI(?s) && ?s != ?o) } GROUP BY ?p"
BY_SUBJECT = "SELECT ?s (COUNT(*) AS ?n) WHERE { ?s ?p ?o FILTER isIRI(?s) } GROUP BY ?s"
COUNT_OBJECT = "SELECT (COUNT(*) AS ?n) WHERE {{ ?s ?p <{x}> }}"
COUNT_PREDICATE = "SELECT (COUNT(*) AS ?n) WHERE {{ ?s <{x}> ?o }}"
COUNT_SELF = "SELECT (COUNT(*) AS ?n) WHERE {{ {{ <{x}> ?p <{x}> }} {union} }}"
COUNT_SELF_PREDICATE = "UNION {{ <{x}> <{x}> ?o }}"
FILTERED_COUNT_OBJECT = "SELECT (COUNT(*) AS ?n) WHERE {{ ?s ?p <{x}> FILTER (isIRI(?s) && ?s != <{x}>) }}"
FILTERED_COUNT_PREDICATE = "SELECT (COUNT(*) AS ?n) WHERE {{ ?s <{x}> ?o FILTER (isIRI(?s) && ?s != <{x}>) }}"
STREAM_OBJECT = "SELECT ?s ?p WHERE {{ ?s ?p <{x}> FILTER (isIRI(?s) && ?s != <{x}>) }}"
STREAM_PREDICATE = "SELECT ?s WHERE {{ ?s <{x}> ?o FILTER (isIRI(?s) && ?s != <{x}>) }}"
WALK = "SELECT ?s ?p WHERE {{ VALUES ?s {{ {values} }} {{ ?s ?p <{x}> }} {union} FILTER (?s != <{x}>) }}"
WALK_PREDICATE = "UNION {{ ?s <{x}> ?o BIND (<{x}> AS ?p) }}"
ROOT_COUNT = (
    "SELECT count(*) FROM bnode_ref f JOIN bnode_root r ON r.id = f.root"
    " WHERE f.term = ? AND (r.owner IS NULL OR r.owner <> f.term)"
)
ROOTS = (
    "SELECT r.owner, r.id FROM bnode_ref f JOIN bnode_root r ON r.id = f.root"
    " WHERE f.term = ? AND (r.owner IS NULL OR r.owner <> f.term)"
)
ROOTS_IN_RANGE = (
    "SELECT r.owner, r.id FROM bnode_root r JOIN bnode_ref f ON f.root = r.id AND f.term = ?"
    " WHERE r.owner >= ? AND r.owner < ? AND r.owner <> f.term"
)


def types_query(values: str) -> str:
    """Return the query for the declaration types of IRIs (`VALUES ?iri { … }`), so the OWLAPI parses each as the
    kind of entity it is. Unlike `entity-types.rq` for a page's own axioms, only declaration types: a usage page
    mustn't gain the class assertions or property characteristics of every IRI it mentions."""
    return (
        f"CONSTRUCT {{ ?iri rdf:type ?type }} WHERE {{ {values} ?iri rdf:type ?type"
        f" VALUES ?type {{ {DECLARATION_TYPES} }} }}"
    )


def summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50": round(statistics.median(ordered), 1),
        "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
        "max": round(ordered[-1], 1),
    }


class Bench:
    def __init__(self, sparql: FusekiClient, name: str) -> None:
        self.sparql = sparql
        self.name = name
        self.db = sqlite3.connect(DATA / f"{name}.sqlite")
        self.db.execute("PRAGMA cache_size = -262144")
        self.entities = self.db.execute("SELECT count(*) FROM name_order").fetchone()[0]
        self.properties = {row[0] for row in self.db.execute(f"SELECT iri FROM entity WHERE type IN {PROPERTY_TYPES}")}

    def term(self, iri: str) -> int | None:
        row = self.db.execute("SELECT id FROM term WHERE iri = ?", (iri,)).fetchone()
        return None if row is None else row[0]

    async def count(self, iri: str, filtered: bool = False) -> tuple[int, dict[str, float]]:
        """Return an entity's usage count and the time of each part.

        Fuseki counts `?s ?p X` (and `?s X ?o` for a property) unfiltered, as a filter on `?s` makes TDB2 decode every
        subject (`filtered` times that way instead). The owner map's per-IRI counts of triples with a blank subject,
        and a lookup of X's triples about itself, are subtracted; its roots that name X and aren't X's own are added.
        """
        times = {}

        async def fuseki(query: str) -> int:
            result = await self.sparql.select(DATASET, query, timeout_s=600)
            return int(result.rows[0][0].value)

        is_property = iri in self.properties
        start = time.perf_counter()
        if filtered:
            total = await fuseki(FILTERED_COUNT_OBJECT.format(x=iri))
        else:
            total = await fuseki(COUNT_OBJECT.format(x=iri))
        times["object_ms"] = (time.perf_counter() - start) * 1000
        if is_property:
            start = time.perf_counter()
            total += await fuseki((FILTERED_COUNT_PREDICATE if filtered else COUNT_PREDICATE).format(x=iri))
            times["predicate_ms"] = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        term = self.term(iri)
        if not filtered:
            union = COUNT_SELF_PREDICATE.format(x=iri) if is_property else ""
            total -= await fuseki(COUNT_SELF.format(x=iri, union=union))
            if term is not None:
                row = self.db.execute("SELECT object, predicate FROM bnode_count WHERE term = ?", (term,)).fetchone()
                if row is not None:
                    total -= row[0] + (row[1] if is_property else 0)
        if term is not None:
            total += self.db.execute(ROOT_COUNT, (term,)).fetchone()[0]
        times["self_and_owner_map_ms"] = (time.perf_counter() - start) * 1000
        times["total_ms"] = sum(times.values())
        return total, times

    async def stream(self, iri: str) -> tuple[list[tuple], dict[str, float]]:
        """Return every usage of an entity, sorted, as (rank, kind, detail, subject) tuples; and the phases' times.

        `rank` is the subject's place in name order; general axioms (no subject) rank `entities`, subjects not in
        `entity` after them. `kind` is 0 for `?s ?p <X>`, 1 for `?s <X> ?o`, 2 for a blank-node root.
        """
        times = {}
        start = time.perf_counter()
        direct: list[tuple[str, int, str]] = []
        async with self.sparql.select_stream(DATASET, STREAM_OBJECT.format(x=iri)) as rows:
            async for s, p in rows:
                direct.append((str(s), 0, str(p)))
        if iri in self.properties:
            async with self.sparql.select_stream(DATASET, STREAM_PREDICATE.format(x=iri)) as rows:
                async for (s,) in rows:
                    direct.append((str(s), 1, ""))
        times["fuseki_ms"] = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        self.db.execute("CREATE TEMP TABLE IF NOT EXISTS streamed (iri TEXT, kind INTEGER, detail TEXT)")
        self.db.execute("DELETE FROM streamed")
        self.db.executemany("INSERT INTO streamed VALUES (?, ?, ?)", direct)
        undeclared = self.entities + 1
        usages = self.db.execute(
            "SELECT coalesce(n.rank, ?), s.kind, s.detail, s.iri"
            " FROM streamed s LEFT JOIN name_order n ON n.iri = s.iri",
            (undeclared,),
        ).fetchall()
        term = self.term(iri)
        if term is not None:
            for owner, root in self.db.execute(ROOTS, (term,)):
                rank = self.entities if owner is None else owner if owner < self.entities else undeclared
                usages.append((rank, 2, root, owner))
        times["names_ms"] = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        usages.sort(key=lambda usage: (usage[0], usage[1], str(usage[2]), str(usage[3])))
        times["sort_ms"] = (time.perf_counter() - start) * 1000
        times["total_ms"] = sum(times.values())
        return usages, times

    async def walk(self, iri: str, page: int, batch: int, limit_s: float = 60.0) -> dict:
        """Walk `name_order` in batches until page `page` is filled; return what it cost."""
        start = time.perf_counter()
        term = self.term(iri)
        union = WALK_PREDICATE.format(x=iri) if iri in self.properties else ""
        found = 0
        candidates = 0
        batches = 0
        fuseki_s = 0.0
        low = 0
        while found < page * PAGE and low < self.entities:
            if time.perf_counter() - start > limit_s:
                return {"timed_out": True, "candidates": candidates, "ms": round(limit_s * 1000)}
            iris = [
                row[0]
                for row in self.db.execute(
                    "SELECT iri FROM name_order WHERE rank >= ? AND rank < ? ORDER BY rank", (low, low + batch)
                )
            ]
            values = " ".join(f"<{each}>" for each in iris)
            began = time.perf_counter()
            result = await self.sparql.select(DATASET, WALK.format(values=values, x=iri, union=union), timeout_s=600)
            fuseki_s += time.perf_counter() - began
            found += len(result.rows)
            if term is not None:
                found += len(self.db.execute(ROOTS_IN_RANGE, (term, low, low + batch)).fetchall())
            candidates += len(iris)
            batches += 1
            low += batch
        elapsed = time.perf_counter() - start
        return {
            "ms": round(elapsed * 1000, 1),
            "fuseki_ms": round(fuseki_s * 1000, 1),
            "candidates": candidates,
            "batches": batches,
            "us_per_candidate": round(elapsed * 1e6 / max(candidates, 1), 2),
            "found": found,
        }


def write(name: str, command: str, result: object) -> None:
    text = json.dumps(result, indent=1)
    (DATA / f"{name}-{command}.json").write_text(text)
    print(text, flush=True)


async def census(bench: Bench) -> dict:
    result: dict[str, object] = {}
    sizes: dict[str, list[int]] = {}
    for column, query in ((0, BY_OBJECT), (1, BY_PREDICATE)):
        start = time.perf_counter()
        async with bench.sparql.select_stream(DATASET, query) as rows:
            async for iri, n in rows:
                sizes.setdefault(str(iri), [0, 0, 0])[column] = int(n.value)
        result[f"{'object' if column == 0 else 'predicate'}_query_s"] = round(time.perf_counter() - start, 1)
    start = time.perf_counter()
    for iri, n in bench.db.execute(
        "SELECT t.iri, count(*) FROM bnode_ref f JOIN bnode_root r ON r.id = f.root JOIN term t ON t.id = f.term"
        " WHERE r.owner IS NULL OR r.owner <> f.term GROUP BY f.term"
    ):
        sizes.setdefault(iri, [0, 0, 0])[2] = n
    result["owner_map_counts_s"] = round(time.perf_counter() - start, 1)
    db = bench.db
    db.execute("DROP TABLE IF EXISTS usage_size")
    db.execute(
        "CREATE TABLE usage_size (iri TEXT PRIMARY KEY, object INTEGER, predicate INTEGER, blank INTEGER,"
        " total INTEGER, entity INTEGER)"
    )
    declared = {row[0] for row in db.execute("SELECT iri FROM entity")}
    db.executemany(
        "INSERT INTO usage_size VALUES (?, ?, ?, ?, ?, ?)",
        (
            (iri, o, p, b, o + (p if iri in bench.properties else 0) + b, iri in declared)
            for iri, (o, p, b) in sizes.items()
        ),
    )
    db.commit()
    totals = sorted((row[0] for row in db.execute("SELECT total FROM usage_size WHERE entity")), reverse=True)
    result["entities_with_usages"] = len(totals)
    result["usages_of_entities"] = sum(totals)
    for limit in (1_000, 5_000, 10_000, 15_000, 20_000, 50_000, 100_000, 1_000_000):
        above = [n for n in totals if n > limit]
        result[f"above_{limit}"] = {"entities": len(above), "usages": sum(above)}
    result["largest"] = db.execute(
        "SELECT u.iri, e.display_name, u.object, u.predicate, u.blank, u.total FROM usage_size u"
        " JOIN entity e ON e.iri = u.iri WHERE u.entity GROUP BY u.iri ORDER BY u.total DESC LIMIT 25"
    ).fetchall()
    result["subjects_not_in_entity"] = await undeclared_subjects(bench, declared)
    return result


async def undeclared_subjects(bench: Bench, declared: set[str]) -> dict:
    start = time.perf_counter()
    ontology = bench.db.execute("SELECT ontology_iri FROM meta").fetchone()[0]
    triples = Counter()
    examples: list[tuple[str, int]] = []
    async with bench.sparql.select_stream(DATASET, BY_SUBJECT) as rows:
        async for s, n in rows:
            iri, count = str(s), int(n.value)
            kind = "ontology" if iri == ontology else "entity" if iri in declared else "undeclared"
            triples[kind] += count
            triples[f"{kind} subjects"] += 1
            if kind == "undeclared":
                examples.append((iri, count))
    owned = Counter(
        dict(
            bench.db.execute(
                "SELECT CASE WHEN r.owner IS NULL THEN 'none' WHEN r.owner < ? THEN 'entity'"
                " WHEN t.iri = ? THEN 'ontology' ELSE 'undeclared' END, count(*)"
                " FROM bnode_root r LEFT JOIN term t ON t.id = r.owner GROUP BY 1",
                (bench.entities, ontology),
            )
        )
    )
    examples.sort(key=lambda item: -item[1])
    return {
        "s": round(time.perf_counter() - start, 1),
        "triples_by_subject": dict(triples),
        "owner_map_roots_by_owner": dict(owned),
        "largest_undeclared": examples[:10],
    }


def chosen(bench: Bench, top: int, sample: int, minimum: int = 0) -> list[tuple[str, int]]:
    largest = bench.db.execute(
        "SELECT iri, total FROM usage_size WHERE entity ORDER BY total DESC LIMIT ?", (top,)
    ).fetchall()
    random.seed(7)
    everyone = [row[0] for row in bench.db.execute("SELECT iri FROM name_order")]
    picked = random.sample(everyone, min(sample, len(everyone)))
    sizes = dict(bench.db.execute("SELECT iri, total FROM usage_size"))
    return [*largest, *((iri, sizes.get(iri, 0)) for iri in picked if sizes.get(iri, 0) >= minimum)]


async def counts(bench: Bench, args) -> dict:
    entities = chosen(bench, args.top, args.sample)
    first = {}
    for iri, _ in entities:  # the first query of each after Fuseki's start, if run then: "cold"
        _, times = await bench.count(iri)
        first[iri] = times["total_ms"]
    warm: dict[str, list[float]] = {iri: [] for iri, _ in entities}
    for _ in range(args.runs):
        for iri, _ in entities:
            total, times = await bench.count(iri)
            warm[iri].append(times["total_ms"])
    rows = []
    for iri, expected in entities:
        total, times = await bench.count(iri)
        rows.append(
            {
                "iri": iri,
                "count": total,
                "census": expected,
                "first_ms": round(first[iri], 1),
                "warm_p50_ms": round(statistics.median(warm[iri]), 1),
                "warm_max_ms": round(max(warm[iri]), 1),
                **{key: round(value, 1) for key, value in times.items()},
            }
        )
    for row in rows[: args.top]:
        filtered, times = await bench.count(row["iri"], filtered=True)
        row["filtered_ms"] = round(times["total_ms"], 1)
        row["filtered_count_matches"] = filtered == row["count"]
    sampled = rows[args.top :]
    return {
        "largest": rows[: args.top],
        "sample": {
            "entities": len(sampled),
            "warm_p50_ms": summary([row["warm_p50_ms"] for row in sampled]),
            "first_ms": summary([row["first_ms"] for row in sampled]),
            "count_mismatches": [row for row in sampled if row["count"] != row["census"]][:5],
        },
        "over_300_ms": [row["iri"] for row in rows if row["warm_p50_ms"] > 300],
    }


def walk_depth(usages: list[tuple], page: int) -> int | None:
    """Return how many candidates a walk reads before page `page` is filled: the rank after its last usage's subject."""
    if len(usages) < page * PAGE:
        return None
    return usages[page * PAGE - 1][0] + 1


async def sweep(bench: Bench, args) -> dict:
    entities = bench.db.execute(
        "SELECT iri, total FROM usage_size WHERE entity AND total > ? ORDER BY total DESC", (args.min,)
    ).fetchall()
    cache = sqlite3.connect(DATA / f"{bench.name}-usage-cache.sqlite")
    cache.execute("DROP TABLE IF EXISTS usage")
    cache.execute(
        "CREATE TABLE usage (entity INTEGER NOT NULL, position INTEGER NOT NULL, subject INTEGER,"
        " kind INTEGER NOT NULL, detail TEXT, PRIMARY KEY (entity, position)) WITHOUT ROWID"
    )
    rows = []
    for number, (iri, total) in enumerate(entities):
        await bench.stream(iri)  # warm
        usages, times = await bench.stream(iri)
        row = {
            "iri": iri,
            "count": len(usages),
            "census": total,
            **{key: round(value, 1) for key, value in times.items()},
            "us_per_usage": round(times["total_ms"] * 1000 / max(len(usages), 1), 2),
            "uniform_page1_candidates": round(PAGE * bench.entities / max(len(usages), 1)),
            "page1_candidates": walk_depth(usages, 1),
            "page10_candidates": walk_depth(usages, 10),
            "page100_candidates": walk_depth(usages, 100),
            "last_page_candidates": usages[-1][0] + 1 if usages else None,
        }
        if number < args.cache:
            start = time.perf_counter()
            term = bench.term(iri) or -1
            cache.executemany(
                "INSERT INTO usage VALUES (?, ?, ?, ?, ?)",
                (
                    (term, position, rank if rank < bench.entities else None, kind, str(detail))
                    for position, (rank, kind, detail, _) in enumerate(usages)
                ),
            )
            cache.commit()
            row["cache_insert_ms"] = round((time.perf_counter() - start) * 1000, 1)
        rows.append(row)
        print(json.dumps(row), flush=True)
    size = cache.execute("SELECT sum(pgsize) FROM dbstat WHERE name = 'usage'").fetchone()[0] or 0
    cached = cache.execute("SELECT count(*) FROM usage").fetchone()[0]
    cache.close()
    return {
        "entities_name_order": bench.entities,
        "rows": rows,
        "cache": {"rows": cached, "MiB": round(size / 2**20, 1), "bytes_per_row": round(size / max(cached, 1), 1)},
    }


async def walk(bench: Bench, args) -> dict:
    iris = args.iri or [row[0] for row in chosen(bench, args.top, 0)]
    rows = []
    for iri in iris:
        for batch in args.batch:
            for page in args.page:
                await bench.walk(iri, page, batch, args.limit)  # warm
                runs = [await bench.walk(iri, page, batch, args.limit) for _ in range(args.runs)]
                result = min(runs, key=lambda run: run["ms"])
                row = {
                    "iri": iri,
                    "batch": batch,
                    "page": page,
                    "best": result,
                    "median_ms": statistics.median(run["ms"] for run in runs),
                }
                rows.append(row)
                print(json.dumps(row), flush=True)
    return {"rows": rows}


def root_triples_query(roots: list[tuple[str, str | None, str | None, bool]]) -> list[str]:
    """Return the queries that fetch the triples of blank-node roots (label, owner, predicate, reified)."""
    queries = []
    owned = [(label, owner, kind) for label, owner, kind, reified in roots if owner and not reified and kind]
    if owned:
        values = " ".join(f"(<{owner}> <{kind}> <_:{label}>)" for label, owner, kind in owned)
        queries.append(f"CONSTRUCT {{ ?s ?p ?o }} WHERE {{ VALUES (?s ?p ?o) {{ {values} }} ?s ?p ?o }}")
    labels = " ".join(f"<_:{label}>" for label, *_ in roots)
    queries.append(f"DESCRIBE {labels}")
    reified = [label for label, _, _, is_reified in roots if is_reified]
    if reified:
        values = " ".join(f"<_:{label}>" for label in reified)
        queries.append(
            "CONSTRUCT { ?s ?p ?t } WHERE { VALUES ?a { "
            + values
            + " } ?a owl:annotatedSource ?s ; owl:annotatedProperty ?p ; owl:annotatedTarget ?t . ?s ?p ?t }"
        )
        queries.append(
            "DESCRIBE ?t WHERE { VALUES ?a { " + values + " } ?a owl:annotatedTarget ?t FILTER isBlank(?t) }"
        )
    return queries


async def fetch_page(bench: Bench, iri: str, page_usages: list[tuple]) -> bytes:
    direct = [(subject, kind, detail) for _, kind, detail, subject in page_usages if kind != 2]
    roots = [detail for _, kind, detail, _ in page_usages if kind == 2]
    queries = []
    objects = [(s, p) for s, kind, p in direct if kind == 0]
    if objects:
        values = " ".join(f"(<{s}> <{p}>)" for s, p in objects)
        queries.append(f"CONSTRUCT {{ ?s ?p <{iri}> }} WHERE {{ VALUES (?s ?p) {{ {values} }} ?s ?p <{iri}> }}")
    subjects = sorted({s for s, kind, _ in direct if kind == 1})
    if subjects:
        values = " ".join(f"<{s}>" for s in subjects)
        queries.append(
            f"CONSTRUCT {{ ?s <{iri}> ?o . ?o ?q ?r }} WHERE {{ VALUES ?s {{ {values} }} ?s <{iri}> ?o"
            " OPTIONAL { ?o ?q ?r FILTER isBlank(?o) } }"
        )
    if roots:
        marks = ",".join("?" * len(roots))
        rows = bench.db.execute(
            f"SELECT r.label, o.iri, k.iri, r.reified FROM bnode_root r LEFT JOIN term o ON o.id = r.owner"
            f" LEFT JOIN term k ON k.id = r.kind WHERE r.id IN ({marks})",
            roots,
        ).fetchall()
        queries += root_triples_query(rows)
    results = await asyncio.gather(*(bench.sparql.construct(DATASET, PREFIXES + query) for query in queries))
    triples = b"".join(results)
    mentioned = ntriples_iris(triples)
    if mentioned:
        values = "VALUES ?iri { " + " ".join(f"<{each}>" for each in sorted(mentioned)) + " }"
        triples += await bench.sparql.construct(DATASET, PREFIXES + types_query(values))
    return triples


def parse_page(ntriples: bytes) -> list[str]:
    with owlapi.ntriples_ontology(ntriples) as ontology:
        found = [
            axiom.getAxiomWithoutAnnotations()
            for axiom in ontology.getAxioms()
            if str(axiom.getAxiomType().getName()) != "Declaration"
        ]
        nodes = browl_axioms.convert_axioms(dict.fromkeys(found))
        return [node.manchester for node in nodes]


async def pages(bench: Bench, args) -> dict:
    owlapi.start_jvm(ROBOT_JAR, "2g")
    iris = [*args.iri, *(row[0] for row in chosen(bench, args.top, args.sample, minimum=1))]
    fetch_ms, parse_ms, rows = [], [], []
    for iri in iris:
        usages, _ = await bench.stream(iri)
        for page in args.page:
            chunk = usages[(page - 1) * PAGE : page * PAGE]
            if not chunk:
                continue
            for _ in range(2):  # warm, then timed
                start = time.perf_counter()
                triples = await fetch_page(bench, iri, chunk)
                fetched = time.perf_counter()
                rendered = await owlapi.run_java(parse_page, triples)
                done = time.perf_counter()
            fetch_ms.append((fetched - start) * 1000)
            parse_ms.append((done - fetched) * 1000)
            rows.append(
                {
                    "iri": iri,
                    "page": page,
                    "usages": len(chunk),
                    "axioms": len(rendered),
                    "bytes": len(triples),
                    "fetch_ms": round(fetch_ms[-1], 1),
                    "parse_render_ms": round(parse_ms[-1], 1),
                }
            )
    return {
        "pages": len(rows),
        "fetch_ms": summary(fetch_ms),
        "parse_render_ms": summary(parse_ms),
        "total_ms": summary([a + b for a, b in zip(fetch_ms, parse_ms, strict=True)]),
        "slowest": sorted(rows, key=lambda row: -(row["fetch_ms"] + row["parse_render_ms"]))[:8],
        "fewer_axioms_than_usages": [row for row in rows if row["axioms"] < row["usages"]][:10],
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("name")
    parser.add_argument("command", choices=["census", "counts", "sweep", "walk", "pages"])
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--sample", type=int, default=200)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--min", type=int, default=1_000)
    parser.add_argument("--cache", type=int, default=10)
    parser.add_argument("--iri", action="append", default=[])
    parser.add_argument("--batch", type=int, action="append", default=[])
    parser.add_argument("--page", type=int, action="append", default=[])
    parser.add_argument("--limit", type=float, default=60.0)
    parser.add_argument("--tag", default="")
    args = parser.parse_args()
    args.batch = args.batch or [1000]
    args.page = args.page or [1]
    async with httpx2.AsyncClient(base_url=args.url, timeout=600) as http:
        bench = Bench(FusekiClient(http), args.name)
        if args.command == "census":
            result = await census(bench)
        else:
            result = await globals()[args.command](bench, args)
    write(args.name, args.command + (f"-{args.tag}" if args.tag else ""), result)


if __name__ == "__main__":
    asyncio.run(main())
