"""Check the usage sets that Fuseki and the owner map give against the OWLAPI's, for a sample of entities and the hubs.

Usage: poetry run python research/S-7/check.py URL NAME ONTOLOGY_FILE [--sample N] [--top N] [--heap 8g] [--iri IRI]

For each entity X, the OWLAPI's set is `getReferencingAxioms` of each entity with X's IRI, plus the annotation
assertions with X as subject or value and the axioms with an annotation whose value is X, all without annotations and
declarations; minus X's own axioms, those the OWLAPI parses from `DESCRIBE X`. browl's set is what the OWLAPI parses
from the usages' triples: `?s ?p X` and `?s X ?o` (IRI subjects but X, with blank objects' closure), and the owner-map
roots that name X and aren't X's own, with the declaration types of the IRIs they mention. The script also checks that
the OWLAPI's set is covered by X's own axioms and its usages, and that the count (`bench.Bench.count`) is the number of
usage axioms.
"""

import argparse
import asyncio
import json
import random
import time
from pathlib import Path

import httpx2
import jpype
from bench import DATA, DATASET, PREFIXES, ROBOT_JAR, Bench, root_triples_query, types_query

from browl import owlapi
from browl.fuseki import FusekiClient
from browl.rdf import ntriples_iris

CHUNK = 2_000
BIG = 2**31


def load_whole(path: str):
    manager = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").createOWLOntologyManager()
    start = time.perf_counter()
    ontology = manager.loadOntologyFromOntologyDocument(jpype.JClass("java.io.File")(path))
    loaded = time.perf_counter() - start
    by_value: dict[str, set[str]] = {}

    def annotation_values(annotations):
        for annotation in annotations:
            value = annotation.getValue()
            if value.isIRI():
                yield str(value)
            yield from annotation_values(annotation.getAnnotations())

    for axiom in ontology.getAxioms():
        bare = None
        if str(axiom.getAxiomType().getName()) == "AnnotationAssertion" and axiom.getValue().isIRI():
            bare = str(axiom.getAxiomWithoutAnnotations())
            by_value.setdefault(str(axiom.getValue()), set()).add(bare)
        if axiom.isAnnotated():
            for iri in annotation_values(axiom.getAnnotations()):
                by_value.setdefault(iri, set()).add(bare or str(axiom.getAxiomWithoutAnnotations()))
    return ontology, by_value, loaded


def owlapi_set(ontology, by_value, iri: str) -> set[str]:
    """Return the OWLAPI's axioms that reference an IRI, without annotations or declarations."""
    excluded = jpype.JClass("org.semanticweb.owlapi.model.parameters.Imports").EXCLUDED
    java_iri = jpype.JClass("org.semanticweb.owlapi.model.IRI").create(iri)
    found = set(by_value.get(iri, ()))
    for entity in ontology.getEntitiesInSignature(java_iri):
        for axiom in ontology.getReferencingAxioms(entity, excluded):
            if str(axiom.getAxiomType().getName()) != "Declaration":
                found.add(str(axiom.getAxiomWithoutAnnotations()))
    found.update(str(axiom.getAxiomWithoutAnnotations()) for axiom in ontology.getAnnotationAssertionAxioms(java_iri))
    return {normal(axiom) for axiom in found}


def normal(axiom: str) -> str:
    """Return an axiom's text with `xsd:string` literals as plain ones: the RDF/XML and N-Triples parses differ."""
    return axiom.replace('"^^xsd:string', '"')


def fragment_set(ntriples: bytes) -> set[str]:
    with owlapi.ntriples_ontology(ntriples) as ontology:
        return {
            normal(str(axiom.getAxiomWithoutAnnotations()))
            for axiom in ontology.getAxioms()
            if str(axiom.getAxiomType().getName()) != "Declaration"
        }


async def construct_all(bench: Bench, queries: list[str]) -> bytes:
    triples = b"".join(
        [await bench.sparql.construct(DATASET, PREFIXES + query, timeout_s=600, max_bytes=BIG) for query in queries]
    )
    mentioned = sorted(ntriples_iris(triples))
    for start in range(0, len(mentioned), CHUNK):
        values = "VALUES ?iri { " + " ".join(f"<{each}>" for each in mentioned[start : start + CHUNK]) + " }"
        triples += await bench.sparql.construct(DATASET, PREFIXES + types_query(values), timeout_s=600, max_bytes=BIG)
    return triples


async def usage_fragment(bench: Bench, iri: str) -> bytes:
    """Return the triples of an entity's usages: `?s ?p X` and `?s X ?o` (IRI subjects but X), and the owner-map roots
    that name X and aren't owned by it."""
    queries = [
        f"CONSTRUCT {{ ?s ?p <{iri}> }} WHERE {{ ?s ?p <{iri}> FILTER (isIRI(?s) && ?s != <{iri}>) }}",
        f"CONSTRUCT {{ ?s <{iri}> ?o }} WHERE {{ ?s <{iri}> ?o FILTER (isIRI(?s) && ?s != <{iri}>) }}",
        f"DESCRIBE ?o WHERE {{ ?s <{iri}> ?o FILTER (isIRI(?s) && ?s != <{iri}> && isBlank(?o)) }}",
    ]
    term = bench.term(iri)
    if term is not None:
        roots = bench.db.execute(
            "SELECT r.label, o.iri, k.iri, r.reified FROM bnode_ref f JOIN bnode_root r ON r.id = f.root"
            " LEFT JOIN term o ON o.id = r.owner LEFT JOIN term k ON k.id = r.kind"
            " WHERE f.term = ? AND (r.owner IS NULL OR r.owner <> f.term)",
            (term,),
        ).fetchall()
        for start in range(0, len(roots), CHUNK):
            queries += root_triples_query(roots[start : start + CHUNK])
    return await construct_all(bench, queries)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("name")
    parser.add_argument("ontology")
    parser.add_argument("--sample", type=int, default=300)
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--heap", default="8g")
    parser.add_argument("--iri", action="append", default=[])
    parser.add_argument("--max-usages", type=int, default=2**62, help="leave out larger hubs (memory)")
    args = parser.parse_args()
    owlapi.start_jvm(ROBOT_JAR, args.heap)
    ontology, by_value, loaded = await owlapi.run_java(load_whole, str(Path(args.ontology).resolve()))
    print(json.dumps({"loaded_whole_s": round(loaded, 1)}), flush=True)
    async with httpx2.AsyncClient(base_url=args.url, timeout=600) as http:
        bench = Bench(FusekiClient(http), args.name)
        top = [
            row[0]
            for row in bench.db.execute(
                "SELECT iri FROM usage_size WHERE entity AND total <= ? ORDER BY total DESC LIMIT ?",
                (args.max_usages, args.top),
            )
        ]
        random.seed(3)
        everyone = [row[0] for row in bench.db.execute("SELECT iri FROM name_order")]
        iris = list(dict.fromkeys([*top, *args.iri, *random.sample(everyone, min(args.sample, len(everyone)))]))
        same = complete = count_same = 0
        differ, incomplete, count_differ = [], [], []
        for iri in iris:
            expected = await owlapi.run_java(owlapi_set, ontology, by_value, iri)
            own = await owlapi.run_java(fragment_set, await construct_all(bench, [f"DESCRIBE <{iri}>"]))
            usage = await owlapi.run_java(fragment_set, await usage_fragment(bench, iri))
            owlapi_usage = expected - own
            missing, extra = owlapi_usage - usage, usage - owlapi_usage
            if not missing and not extra:
                same += 1
            else:
                differ.append(
                    {
                        "iri": iri,
                        "n_missing": len(missing),
                        "missing": sorted(missing)[:3],
                        "n_extra": len(extra),
                        "extra": sorted(extra)[:3],
                    }
                )
            unreached = expected - own - usage
            if not unreached:
                complete += 1
            else:
                incomplete.append({"iri": iri, "n": len(unreached), "examples": sorted(unreached)[:3]})
            count, _ = await bench.count(iri)
            if count == len(usage):
                count_same += 1
            else:
                count_differ.append(
                    {"iri": iri, "count": count, "usage_axioms": len(usage), "owlapi_usage": len(owlapi_usage)}
                )
            print(
                json.dumps(
                    {
                        "iri": iri,
                        "owlapi": len(expected),
                        "own": len(own),
                        "usage": len(usage),
                        "owlapi_usage": len(owlapi_usage),
                        "missing": len(missing),
                        "extra": len(extra),
                        "unreached": len(unreached),
                        "count": count,
                    }
                ),
                flush=True,
            )
    result = {
        "entities": len(iris),
        "usage_equals_owlapi_minus_own": same,
        "differ": differ[:20],
        "owlapi_set_covered_by_own_and_usage": complete,
        "not_covered": incomplete[:20],
        "count_equals_usage_axioms": count_same,
        "count_differs": count_differ[:20],
        "n_count_differs": len(count_differ),
    }
    text = json.dumps(result, indent=1)
    (DATA / f"{args.name}-check.json").write_text(text)
    print(text)


asyncio.run(main())
