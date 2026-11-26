"""Measure axiom annotations' cost on an entity's Overview: the fragment's extra size and IRIs, the second query's rows.

Usage: poetry run python research/P-49/bench.py URL DATASET [--sample N] [--top N] [--iri IRI ...]

For a random sample of the store's classes, and the `--top` entities that the most reified axioms annotate (those whose
`owl:annotatedSource` they are), reads each entity's Overview data as the page does (`browl.overview`): its annotations
with their axiom annotations (`browl.annotations.fetch_value_annotations`, alongside `fetch_annotations`) and its
axioms from a fragment with their annotations (`browl.axioms.fetch_fragment`, `entity_axioms`). Each is read once to
warm up, then timed; then timed again without the axiom annotations (the second query skipped, and the fragment's
annotations query answering nothing), the page as it was before P-49. Prints the times and the largest sizes against
the caps (`MAX_AXIOM_ANNOTATIONS`, `MAX_ANNOTATIONS_CHARS`, `CONSTRUCT_MAX_BYTES`, `MAX_FRAGMENT_IRIS`).
"""

import argparse
import asyncio
import json
import random
import statistics
import time
from pathlib import Path

import httpx2

from browl import annotations, axioms, owlapi
from browl.fuseki import FusekiClient, SparqlError
from browl.rdf import ntriples_iris
from browl.tasks import task_group

ROBOT_JAR = Path(__file__).parents[2] / ".cache" / "robot.jar"


def summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50": round(statistics.median(ordered), 1),
        "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
        "max": round(ordered[-1], 1),
    }


async def sample(sparql: FusekiClient, dataset: str, size: int, top: int) -> tuple[list[str], list[str]]:
    """Return a random sample of the store's classes, and the entities the most reified axioms annotate."""
    random.seed(1)
    query = "SELECT ?e WHERE { ?e a <http://www.w3.org/2002/07/owl#Class> FILTER isIRI(?e) }"
    async with sparql.select_stream(dataset, query) as stream:
        classes = sorted({str(row[0]) async for row in stream})
    most = (
        "SELECT ?e (COUNT(*) AS ?n) WHERE { ?a <http://www.w3.org/2002/07/owl#annotatedSource> ?e FILTER isIRI(?e) }"
        f" GROUP BY ?e ORDER BY DESC(?n) LIMIT {top}"
    )
    result = await sparql.select(dataset, most)
    return random.sample(classes, min(size, len(classes))), [str(row[0]) for row in result.rows]


async def overview(sparql: FusekiClient, dataset: str, iri: str, annotated: bool) -> dict[str, float]:
    """Read an entity's Overview data as the page does; return the time and the sizes."""
    start = time.perf_counter()
    async with task_group() as group:
        pairs = group.create_task(annotations.fetch_annotations(sparql, dataset, iri))
        values = group.create_task(_values(sparql, dataset, iri)) if annotated else None
        fragment = group.create_task(axioms.fetch_fragment(sparql, dataset, iri))
    found = await owlapi.run_java(axioms.entity_axioms, fragment.result().ntriples, iri)
    elapsed = (time.perf_counter() - start) * 1000
    rows, chars = values.result() if values is not None else (0, 0)
    ntriples = fragment.result().ntriples
    return {
        "ms": elapsed,
        "values": len(pairs.result()),
        "value_annotation_rows": rows,
        "value_annotation_chars": chars,
        "fragment_bytes": len(ntriples),
        "fragment_iris": len(ntriples_iris(ntriples)),
        "axioms": len(found),
        "annotated_axioms": sum(bool(each.annotations) for each in found),
        "left_out": str(fragment.result().annotations_left_out),
    }


async def _values(sparql: FusekiClient, dataset: str, iri: str) -> tuple[int, int]:
    """Fetch the values' axiom annotations as the page does; return the query's rows and characters."""
    query = axioms.filled_query(annotations._VALUE_ANNOTATIONS, "", {"$entity": f"<{iri}>"})
    try:
        result = await sparql.select(dataset, f"{query}LIMIT {annotations.MAX_AXIOM_ANNOTATIONS + 1}\n")
    except SparqlError:
        return -1, -1
    annotations.value_annotations(annotations.IRI(iri), result.rows)
    return len(result.rows), sum(len(repr(cell)) for row in result.rows for cell in row)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("dataset")
    parser.add_argument("--sample", type=int, default=300)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--iri", action="append", default=[])
    parser.add_argument("--heap", default="4g")
    args = parser.parse_args()
    owlapi.start_jvm(ROBOT_JAR, args.heap)
    async with httpx2.AsyncClient(base_url=args.url, timeout=600) as http:
        sparql = FusekiClient(http)
        random_iris, top = await sample(sparql, args.dataset, args.sample, args.top)
        iris = [*random_iris, *top, *args.iri]
        before = axioms._AXIOM_ANNOTATIONS
        results = {}
        for annotated in (True, False):
            # Without them, the fragment's annotations query asks for nothing.
            axioms._AXIOM_ANNOTATIONS = before if annotated else before.replace("DESCRIBE ?node", "DESCRIBE ?none")
            for iri in iris:  # warm up
                await overview(sparql, args.dataset, iri, annotated)
            results[annotated] = {iri: await overview(sparql, args.dataset, iri, annotated) for iri in iris}
        axioms._AXIOM_ANNOTATIONS = before
        with_, without = results[True], results[False]
        report: dict[str, object] = {
            "entities": len(iris),
            "ms (with annotations), random sample": summary([with_[iri]["ms"] for iri in random_iris]),
            "ms (without), random sample": summary([without[iri]["ms"] for iri in random_iris]),
            "ms (with), most annotated": summary([with_[iri]["ms"] for iri in top]),
            "ms (without), most annotated": summary([without[iri]["ms"] for iri in top]),
            "caps": {
                "MAX_AXIOM_ANNOTATIONS": annotations.MAX_AXIOM_ANNOTATIONS,
                "MAX_ANNOTATIONS_CHARS": annotations.MAX_ANNOTATIONS_CHARS,
                "CONSTRUCT_MAX_BYTES": axioms.CONSTRUCT_MAX_BYTES,
                "MAX_FRAGMENT_IRIS": axioms.MAX_FRAGMENT_IRIS,
            },
        }
        for key in ("value_annotation_rows", "value_annotation_chars", "fragment_bytes", "fragment_iris"):
            biggest = max(iris, key=lambda iri, key=key: with_[iri][key])
            report[f"largest {key}"] = {"iri": biggest, **with_[biggest], "without": without[biggest]}
        report["fragment growth (bytes with / without), most annotated"] = summary(
            [with_[iri]["fragment_bytes"] / max(1, without[iri]["fragment_bytes"]) for iri in top]
        )
        report["left out"] = [iri for iri in iris if with_[iri]["left_out"] != "None"]
        report["most annotated"] = {
            iri.rsplit("/", 1)[-1]: {
                key: round(value, 1) if isinstance(value, float) else value for key, value in with_[iri].items()
            }
            for iri in top[:8]
        }
        print(json.dumps(report, indent=1), flush=True)


asyncio.run(main())
