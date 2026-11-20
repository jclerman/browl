"""Compare every logical axiom of an ontology, as nodes rendered in Python, with the OWLAPI's Manchester rendering.

Usage: poetry run python research/P-15b/compare.py ONTOLOGY [--heap 8g]

Loads the whole ontology into the OWLAPI, then for each axiom but declarations and annotation assertions, without its
annotations: renders it with the OWLAPI's renderer (full IRIs), converts it into nodes (`browl.axioms.convert_axioms`)
and renders those. Texts are compared as `tests/test_axioms.py` compares them: single spaces, none before a comma or a
closing parenthesis; the kinds it lists as different by design are counted apart. Prints the counts, the first
unexpected differences, and the time to convert and render, per axiom.
"""

import argparse
import asyncio
import collections
import json
import statistics
import time
from pathlib import Path
from typing import Any

import jpype

from browl import axioms, owlapi

ROBOT_JAR = Path(__file__).parents[2] / ".cache" / "robot.jar"
DIFFERENT_BY_DESIGN = {"HasKey", "DatatypeDefinition", "Rule"}


def normalized(text: str) -> str:
    return " ".join(text.split()).replace(" , ", ", ").replace(" )", ")")


def compare(path: str) -> dict[str, Any]:
    manager = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").createOWLOntologyManager()
    start = time.perf_counter()
    ontology = manager.loadOntologyFromOntologyDocument(jpype.JClass("java.io.File")(path))
    loaded = time.perf_counter() - start
    prefixes = jpype.JClass("org.semanticweb.owlapi.util.DefaultPrefixManager")()
    prefixes.clear()
    renderer = jpype.JClass(
        "org.semanticweb.owlapi.manchestersyntax.renderer.ManchesterOWLSyntaxOWLObjectRendererImpl"
    )()
    renderer.setShortFormProvider(prefixes)
    every = [
        axiom.getAxiomWithoutAnnotations()
        for axiom in ontology.getAxioms()
        if str(axiom.getAxiomType().getName()) not in axioms._LEFT_OUT
    ]
    kinds: collections.Counter[str] = collections.Counter()
    by_design: collections.Counter[str] = collections.Counter()
    unexpected: list[tuple[str, str]] = []
    convert_us, owlapi_us = [], []
    for axiom in every:
        start = time.perf_counter()
        [node] = axioms.convert_axioms([axiom])
        text = node.manchester
        middle = time.perf_counter()
        expected = str(renderer.render(axiom))
        end = time.perf_counter()
        convert_us.append((middle - start) * 1e6)
        owlapi_us.append((end - middle) * 1e6)
        kind = str(axiom.getAxiomType().getName())
        kinds[kind] += 1
        if normalized(text) != normalized(expected):
            if kind in DIFFERENT_BY_DESIGN:
                by_design[kind] += 1
            else:
                unexpected.append((normalized(text), normalized(expected)))

    def summary(values: list[float]) -> dict[str, float]:
        ordered = sorted(values)
        return {
            "p50": round(statistics.median(ordered), 1),
            "p95": round(ordered[int(len(ordered) * 0.95)], 1),
            "max": round(ordered[-1], 1),
            "total_s": round(sum(ordered) / 1e6, 1),
        }

    return {
        "ontology": path,
        "loaded_s": round(loaded, 1),
        "axioms": len(every),
        "kinds": dict(kinds.most_common()),
        "identical": len(every) - sum(by_design.values()) - len(unexpected),
        "different_by_design": dict(by_design),
        "unexpected": len(unexpected),
        "first_unexpected": unexpected[:10],
        "convert_and_render_us": summary(convert_us),
        "owlapi_render_us": summary(owlapi_us),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ontology")
    parser.add_argument("--heap", default="8g")
    args = parser.parse_args()
    owlapi.start_jvm(ROBOT_JAR, args.heap)
    print(json.dumps(await owlapi.run_java(compare, args.ontology), indent=1), flush=True)


asyncio.run(main())
