"""S-4 spike: Fuseki/TDB2 query latency, up-front queries, and Manchester rendering from fetched triples.

Run from research/S-4 against a running Fuseki (dataset /ds):  ../S-3/.venv/bin/python bench.py <command> [args]
"""

import json
import random
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
ENDPOINT = "http://localhost:3030/ds/sparql"
PREFIXES = """PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX oio: <http://www.geneontology.org/formats/oboInOwl#>
"""


def query(q: str, accept: str = "text/tab-separated-values") -> tuple[float, bytes]:
    data = urllib.parse.urlencode({"query": PREFIXES + q}).encode()
    req = urllib.request.Request(ENDPOINT, data=data, headers={"Accept": accept})
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=3600) as r:
        body = r.read()
    return (time.perf_counter() - t) * 1000, body


def rows(body: bytes) -> int:
    return max(0, body.count(b"\n") - 1)


def log(**kw: object) -> None:
    print(json.dumps(kw), flush=True)


def timed(name: str, q: str, repeat: int = 3, **extra: object) -> None:
    times, body = [], b""
    for _ in range(repeat):
        ms, body = query(q)
        times.append(ms)
    log(query=name, first_ms=round(times[0], 1), best_ms=round(min(times), 1), rows=rows(body), **extra)


def cmd_pages(*iris: str) -> None:
    """Latency of the queries an entity page needs, for each IRI."""
    for iri in iris:
        x = f"<{iri}>"
        timed("describe", f"SELECT ?p ?o WHERE {{ {x} ?p ?o }}", iri=iri)
        timed("children", f"SELECT ?c WHERE {{ ?c rdfs:subClassOf {x} FILTER isIRI(?c) }}", iri=iri)
        timed("refs_count", f"SELECT (COUNT(*) AS ?n) WHERE {{ ?s ?p {x} }}", iri=iri)
        timed("refs_page_index_order", f"SELECT ?s ?p WHERE {{ ?s ?p {x} }} LIMIT 50 OFFSET 1000", iri=iri)
        timed("refs_page_sorted", f"SELECT ?s ?p WHERE {{ ?s ?p {x} }} ORDER BY ?s LIMIT 50", repeat=1, iri=iri)
        timed(
            "descendants_count",
            f"SELECT (COUNT(DISTINCT ?d) AS ?n) WHERE {{ ?d rdfs:subClassOf+ {x} }}",
            repeat=1,
            iri=iri,
        )


def cmd_bulk() -> None:
    """The up-front queries: the name dictionary (with sub-property values) and all named subclass edges."""
    names = """SELECT ?s ?np ?v WHERE {
        VALUES ?np { rdfs:label oio:hasExactSynonym oio:hasRelatedSynonym oio:hasBroadSynonym oio:hasNarrowSynonym }
        ?p rdfs:subPropertyOf* ?np . ?s ?p ?v FILTER isLiteral(?v) }"""
    timed("dictionary", names, repeat=1)
    timed("edges", "SELECT ?c ?p WHERE { ?c rdfs:subClassOf ?p FILTER (isIRI(?c) && isIRI(?p)) }", repeat=1)


def cmd_render(sample: int) -> None:
    """Render class pages from triples fetched per class, and compare with rendering from the full ontology."""
    sys.path.insert(0, str(HERE.parent / "S-1"))
    import spike

    spike.start_jvm("4g")
    from java.io import ByteArrayInputStream
    from org.semanticweb.owlapi.apibinding import OWLManager
    from org.semanticweb.owlapi.io import StreamDocumentSource
    from org.semanticweb.owlapi.model.parameters import Imports

    _, full = spike.load(HERE.parent / "S-1" / "data" / "downloads" / "uberon.owl")
    random.seed(1)
    classes = random.sample([c for c in full.getClassesInSignature() if not c.isOWLThing()], sample)
    renderer = spike.make_renderer(spike.iri_provider())

    def logical(ont, cls) -> list[str]:
        return sorted(str(renderer.render(a)) for a in ont.getAxioms(cls, Imports.EXCLUDED) if a.isLogicalAxiom())

    fetch_ms, parse_ms, same, total, diffs = [], [], 0, 0, []
    for cls in classes:
        iri = str(cls.getIRI())
        t = time.perf_counter()
        # The class's triples with their blank-node closure (CBD), then the types of every IRI they mention.
        _, nt = query(f"DESCRIBE <{iri}>", accept="application/n-triples")
        # Symmetric axioms stated on the other class (A owl:disjointWith X) belong to X's page too.
        _, sym = query(
            f"CONSTRUCT {{ ?s ?p <{iri}> }} WHERE {{ VALUES ?p {{ owl:disjointWith owl:equivalentClass }} "
            f"?s ?p <{iri}> FILTER isIRI(?s) }}",
            accept="application/n-triples",
        )
        nt += b"\n" + sym
        mentioned = {w.strip("<>") for line in nt.decode().splitlines() for w in line.split() if w.startswith("<http")}
        values = " ".join(f"<{m}>" for m in mentioned)
        _, types = query(
            f"CONSTRUCT {{ ?e a ?t }} WHERE {{ VALUES ?e {{ {values} }} ?e a ?t }}", accept="application/n-triples"
        )
        fetch_ms.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter()
        manager = OWLManager.createOWLOntologyManager()
        frag = manager.loadOntologyFromOntologyDocument(StreamDocumentSource(ByteArrayInputStream(nt + b"\n" + types)))
        rendered = logical(frag, manager.getOWLDataFactory().getOWLClass(cls.getIRI()))
        parse_ms.append((time.perf_counter() - t) * 1000)
        expected = logical(full, cls)
        total += 1
        if rendered == expected:
            same += 1
        elif len(diffs) < 5:
            diffs.append(
                {
                    "iri": iri,
                    "missing": sorted(set(expected) - set(rendered))[:3],
                    "extra": sorted(set(rendered) - set(expected))[:3],
                }
            )
    log(
        step="render_from_fragment",
        classes=total,
        identical=same,
        fetch=spike.summary(fetch_ms),
        parse_and_render=spike.summary(parse_ms),
    )
    for d in diffs:
        log(diff=d)


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    if cmd == "pages":
        cmd_pages(*args)
    elif cmd == "bulk":
        cmd_bulk()
    elif cmd == "render":
        cmd_render(int(args[0]) if args else 300)
