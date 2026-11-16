"""Entity-page load on browl's Fuseki or on OLS4's API, from the same generator.

A browl "page" is P-3's four queries (DESCRIBE, children, reference count, first 50 references), without rendering.
An OLS4 "page" is the API calls its class page makes (frontend/src/pages/ontologies/ontologiesSlice.ts): the
ontology, the entity, its individuals, every page of "Related from", and the tree's hierarchical ancestors,
hierarchical children and direct children. Both run their requests one after another.

Usage, in a container on the stack's network:
    python pageload.py browl <dataset> <concurrency> <seconds> <iris-file>
    python pageload.py ols4 <ontology-id> <concurrency> <seconds> <iris-file>
    python pageload.py ols4 <ontology-id> once <iri>      # one page's requests, with each request's time and size
"""

import asyncio
import json
import random
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import quote

import httpx2

FUSEKI = "http://fuseki:3030"
OLS4 = "http://ols4-backend:8080/api/v2"
PREFIXES = "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\nPREFIX owl: <http://www.w3.org/2002/07/owl#>\n"
TSV = "text/tab-separated-values"


async def browl_page(client: httpx2.AsyncClient, dataset: str, iri: str) -> list[tuple[str, float, int]]:
    url = f"{FUSEKI}/{dataset}/sparql"
    x = f"<{iri}>"
    queries = [
        ("describe", f"DESCRIBE {x}", "application/n-triples"),
        ("children", f"SELECT ?c WHERE {{ ?c rdfs:subClassOf {x} FILTER isIRI(?c) }} LIMIT 200", TSV),
        ("ref-count", f"SELECT (COUNT(*) AS ?n) WHERE {{ ?s ?p {x} }}", TSV),
        ("refs", f"SELECT ?s ?p WHERE {{ ?s ?p {x} }} LIMIT 50", TSV),
    ]
    out = []
    for name, q, accept in queries:
        t = time.perf_counter()
        r = await client.post(url, data={"query": PREFIXES + q}, headers={"Accept": accept})
        r.raise_for_status()
        out.append((name, (time.perf_counter() - t) * 1000, len(r.content)))
    return out


async def ols4_page(client: httpx2.AsyncClient, onto: str, iri: str) -> list[tuple[str, float, int]]:
    base = f"{OLS4}/ontologies/{onto}"
    cls = f"{base}/classes/{quote(quote(iri, safe=''), safe='')}"
    tree = "?size=1000&lang=en&includeObsoleteEntities=false"
    calls = [
        ("ontology", f"{base}?lang=en"),
        ("entity", f"{cls}?lang=en"),
        ("individuals", f"{cls}/individuals?lang=en"),
        ("related-from", f"{cls}/relatedFrom?lang=en"),
        ("hier-ancestors", f"{cls}/hierarchicalAncestors{tree}"),
        ("hier-children", f"{cls}/hierarchicalChildren{tree}"),
        ("children", f"{cls}/children{tree}"),
    ]
    out = []
    for name, url in calls:
        t = time.perf_counter()
        r = await client.get(url)
        r.raise_for_status()
        size = len(r.content)
        if name == "related-from":  # the frontend then fetches the remaining pages
            for page in range(1, r.json().get("totalPages", 1)):
                r2 = await client.get(f"{url}&page={page}")
                r2.raise_for_status()
                size += len(r2.content)
        out.append((name, (time.perf_counter() - t) * 1000, size))
    return out


PAGE = {"browl": browl_page, "ols4": ols4_page}


async def load(target: str, name: str, concurrency: int, seconds: float, iris: list[str]) -> None:
    page = PAGE[target]
    latencies: list[float] = []
    per_call: dict[str, list[float]] = {}
    errors = 0
    deadline = time.monotonic() + seconds

    async def worker(client: httpx2.AsyncClient, rng: random.Random) -> None:
        nonlocal errors
        while time.monotonic() < deadline:
            t = time.perf_counter()
            try:
                calls = await page(client, name, rng.choice(iris))
            except httpx2.HTTPError:
                errors += 1
                continue
            latencies.append((time.perf_counter() - t) * 1000)
            for call, ms, _ in calls:
                per_call.setdefault(call, []).append(ms)

    limits = httpx2.Limits(max_connections=concurrency)
    async with httpx2.AsyncClient(timeout=120, limits=limits) as client:
        await asyncio.gather(*(worker(client, random.Random(i)) for i in range(concurrency)))
    q = statistics.quantiles(latencies, n=100)
    print(
        json.dumps(
            {
                "target": target,
                "name": name,
                "concurrency": concurrency,
                "pages": len(latencies),
                "errors": errors,
                "pages_per_s": round(len(latencies) / seconds, 1),
                "page_ms_p50": round(q[49], 1),
                "page_ms_p95": round(q[94], 1),
                "page_ms_max": round(max(latencies), 1),
                "call_ms_p50": {k: round(statistics.median(v), 1) for k, v in per_call.items()},
            }
        ),
        flush=True,
    )


async def once(target: str, name: str, iri: str) -> None:
    async with httpx2.AsyncClient(timeout=120) as client:
        for call, ms, size in await PAGE[target](client, name, iri):
            print(json.dumps({"call": call, "ms": round(ms, 1), "bytes": size}))


if __name__ == "__main__":
    target, name = sys.argv[1], sys.argv[2]
    if sys.argv[3] == "once":
        asyncio.run(once(target, name, sys.argv[4]))
    else:
        iris = Path(sys.argv[5]).read_text().split()
        asyncio.run(load(target, name, int(sys.argv[3]), float(sys.argv[4]), iris))
