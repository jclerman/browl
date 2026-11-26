"""Time fetching, parsing and rendering entity pages' axioms from a Fuseki store, and compare with the whole ontology.

Usage: poetry run python research/P-15/bench.py URL DATASET [--ontology FILE] [--sample N] [--iri IRI ...] [--union]

For a random sample of the store's classes and properties (and any IRIs given), fetches each entity's fragment
(`browl.axioms.fetch_fragment`), then parses and renders it (`browl.axioms.entity_axioms`): once to warm up, then
again, timed. With `--ontology`, the OWLAPI also loads the whole ontology from that file, the sample is drawn from it,
and each entity's axioms rendered from its fragment are compared with those rendered from the whole ontology.
`--union` adds the dataset clause that pages use when the ontology has imports (`browl.fuseki.UNION_FROM`).
"""

import argparse
import asyncio
import json
import random
import statistics
import time
from pathlib import Path
from typing import Any

import httpx2
import jpype

from browl import axioms, owl, owlapi
from browl.fuseki import UNION_FROM, FusekiClient

ROBOT_JAR = Path(__file__).parents[2] / ".cache" / "robot.jar"
TYPES = ("Class", "ObjectProperty", "DatatypeProperty", "AnnotationProperty")


def summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50": round(statistics.median(ordered), 1),
        "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
        "max": round(ordered[-1], 1),
    }


def load_whole(path: str, sample: int, extra: list[str]) -> tuple[list[str], dict[str, list[owl.Axiom]]]:
    """Load the whole ontology; return a sample of its entities and each one's axioms."""
    manager = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").createOWLOntologyManager()
    start = time.perf_counter()
    ontology = manager.loadOntologyFromOntologyDocument(jpype.JClass("java.io.File")(path))
    print(json.dumps({"loaded_whole_s": round(time.perf_counter() - start, 1)}), flush=True)
    random.seed(1)
    classes = sorted(str(each.getIRI()) for each in ontology.getClassesInSignature())
    properties = sorted(
        str(each.getIRI())
        for each in [*ontology.getObjectPropertiesInSignature(), *ontology.getDataPropertiesInSignature()]
    )
    iris = [*random.sample(classes, min(sample, len(classes))), *properties[:sample], *extra]
    return iris, {iri: axioms.convert_axioms(axioms.entity_closure(ontology, iri)) for iri in iris}


async def sample_store(sparql: FusekiClient, dataset: str, sample: int) -> list[str]:
    """Return a random sample of the store's classes, and of its properties."""
    random.seed(1)
    iris: list[str] = []
    for kind in TYPES:
        query = f"SELECT ?e WHERE {{ ?e a <http://www.w3.org/2002/07/owl#{kind}> FILTER isIRI(?e) }}"
        async with sparql.select_stream(dataset, query) as stream:
            found = sorted({str(row[0]) async for row in stream})
        iris += random.sample(found, min(sample if kind == "Class" else sample // 4, len(found)))
    return iris


async def page(sparql: FusekiClient, dataset: str, iri: str, clause: str) -> tuple[float, float, Any, int]:
    """Fetch, parse and render one entity's axioms; return the times in ms, the axioms and the fragment's size."""
    start = time.perf_counter()
    fragment = await axioms.fetch_fragment(sparql, dataset, iri, clause)
    fetched = time.perf_counter()
    rendered = await owlapi.run_java(axioms.entity_axioms, fragment.ntriples, iri)
    return (fetched - start) * 1000, (time.perf_counter() - fetched) * 1000, rendered, len(fragment.ntriples)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("dataset")
    parser.add_argument("--ontology")
    parser.add_argument("--sample", type=int, default=300)
    parser.add_argument("--iri", action="append", default=[])
    parser.add_argument("--union", action="store_true")
    parser.add_argument("--heap", default="8g")
    args = parser.parse_args()
    owlapi.start_jvm(ROBOT_JAR, args.heap)
    clause = UNION_FROM if args.union else ""
    async with httpx2.AsyncClient(base_url=args.url, timeout=600) as http:
        sparql = FusekiClient(http)
        expected: dict[str, list[owl.Axiom]] = {}
        if args.ontology:
            iris, expected = await owlapi.run_java(load_whole, args.ontology, args.sample, args.iri)
        else:
            iris = [*await sample_store(sparql, args.dataset, args.sample), *args.iri]
        for iri in iris:  # warm up
            await page(sparql, args.dataset, iri, clause)
        fetch, render, totals, slowest, same, differ = [], [], [], [], 0, []
        for iri in iris:
            fetch_ms, render_ms, rendered, size = await page(sparql, args.dataset, iri, clause)
            fetch.append(fetch_ms)
            render.append(render_ms)
            totals.append(fetch_ms + render_ms)
            slowest.append((round(fetch_ms + render_ms, 1), round(fetch_ms, 1), len(rendered), size, iri))
            if iri in expected:
                if rendered == expected[iri]:
                    same += 1
                else:
                    differ.append(iri)
        result: dict[str, object] = {
            "entities": len(iris),
            "fetch_ms": summary(fetch),
            "parse_and_render_ms": summary(render),
            "total_ms": summary(totals),
            "slowest (total ms, fetch ms, axioms, fragment bytes, IRI)": sorted(slowest, reverse=True)[:5],
        }
        if expected:
            result |= {"identical": same, "differ": differ[:10]}
        if args.iri:
            result["given"] = [each for each in slowest if each[-1] in args.iri]
        print(json.dumps(result, indent=1), flush=True)


asyncio.run(main())
