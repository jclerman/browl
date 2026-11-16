"""Page-query load on Fuseki: concurrent "pages", each the queries an entity page needs. Runs in the app container.

Usage, in the app container: [MODE=plain|from|union] python pageload.py <dataset> <concurrency> <seconds> <iris-file>
                              python pageload.py <dataset> bulk
MODE says how queries reach imports in named graphs: not at all (plain), through a FROM merge of every graph (from), or
with { ... } UNION { GRAPH ?g { ... } } (union).
"""

import asyncio
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

import httpx2

FUSEKI = "http://fuseki:3030"
PREFIXES = "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\nPREFIX owl: <http://www.w3.org/2002/07/owl#>\n"
OIO = "PREFIX oio: <http://www.geneontology.org/formats/oboInOwl#>\n"


MODE = os.environ.get("MODE", "plain")  # plain | from | union: how queries reach the imports' named graphs
FROM = "FROM <urn:x-arq:DefaultGraph> FROM <urn:x-arq:UnionGraph>" if MODE != "plain" else ""


def where(pattern: str) -> str:
    """Return a WHERE clause over the main ontology, plus the imports in union mode."""
    if MODE == "union":
        return f"WHERE {{ {{ {pattern} }} UNION {{ GRAPH ?g {{ {pattern} }} }} }}"
    return f"{FROM if MODE == 'from' else ''} WHERE {{ {pattern} }}"


def page_queries(iri: str) -> list[tuple[str, str]]:
    x = f"<{iri}>"
    tsv = "text/tab-separated-values"
    return [
        (f"DESCRIBE {x} {FROM}", "application/n-triples"),
        (f"SELECT ?c {where(f'?c rdfs:subClassOf {x} FILTER isIRI(?c)')} LIMIT 200", tsv),
        (f"SELECT (COUNT(*) AS ?n) {where(f'?s ?p {x}')}", tsv),
        (f"SELECT ?s ?p {where(f'?s ?p {x}')} LIMIT 50", tsv),
    ]


async def run_query(client: httpx2.AsyncClient, url: str, q: str, accept: str) -> int:
    r = await client.post(url, data={"query": PREFIXES + q}, headers={"Accept": accept})
    r.raise_for_status()
    return len(r.content)


async def load(dataset: str, concurrency: int, seconds: float, iris: list[str]) -> None:
    url = f"{FUSEKI}/{dataset}/sparql"
    latencies: list[float] = []
    errors = 0
    deadline = time.monotonic() + seconds

    async def worker(client: httpx2.AsyncClient, rng: random.Random) -> None:
        nonlocal errors
        while time.monotonic() < deadline:
            t = time.perf_counter()
            try:
                for q, accept in page_queries(rng.choice(iris)):
                    await run_query(client, url, q, accept)
            except httpx2.HTTPError:
                errors += 1
                continue
            latencies.append((time.perf_counter() - t) * 1000)

    limits = httpx2.Limits(max_connections=concurrency)
    async with httpx2.AsyncClient(timeout=60, limits=limits) as client:
        await asyncio.gather(*(worker(client, random.Random(i)) for i in range(concurrency)))
    q = statistics.quantiles(latencies, n=100)
    print(
        json.dumps(
            {
                "mode": MODE,
                "concurrency": concurrency,
                "pages": len(latencies),
                "errors": errors,
                "pages_per_s": round(len(latencies) / seconds, 1),
                "page_ms_p50": round(q[49], 1),
                "page_ms_p95": round(q[94], 1),
                "page_ms_max": round(max(latencies), 1),
            }
        ),
        flush=True,
    )


async def bulk(dataset: str) -> None:
    """The up-front pre-caching queries (§2.5), as in S-4: the name dictionary and all named subclass edges."""
    url = f"{FUSEKI}/{dataset}/sparql"
    names = (
        OIO
        + """SELECT ?s ?np ?v WHERE {
        VALUES ?np { rdfs:label oio:hasExactSynonym oio:hasRelatedSynonym oio:hasBroadSynonym oio:hasNarrowSynonym }
        ?p rdfs:subPropertyOf* ?np . ?s ?p ?v FILTER isLiteral(?v) }"""
    )
    edges = "SELECT ?c ?p WHERE { ?c rdfs:subClassOf ?p FILTER (isIRI(?c) && isIRI(?p)) }"
    async with httpx2.AsyncClient(timeout=3600) as client:
        for name, q in (("dictionary", names), ("edges", edges)):
            t = time.perf_counter()
            size = await run_query(client, url, q, "text/tab-separated-values")
            print(
                json.dumps({"query": name, "s": round(time.perf_counter() - t, 1), "mb": round(size / 1e6)}), flush=True
            )


if __name__ == "__main__":
    if sys.argv[2] == "bulk":
        asyncio.run(bulk(sys.argv[1]))
    else:
        iris = Path(sys.argv[4]).read_text().split()
        asyncio.run(load(sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), iris))
