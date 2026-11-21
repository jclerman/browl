"""Build the blank-node owner map from a Fuseki serving a store as /gen-1, into data/NAME.sqlite (from precache.py).

Usage: poetry run python research/S-7/ownermap.py URL NAME [--typed]

One bulk query returns every triple with a blank node as subject or object, blank nodes as their TDB2 labels
(`afn:bnode`) and literals as 0. A root is either a triple with an IRI subject (its owner) and a blank object, a class
expression or list, keyed by the triple since a store can share one blank node between several subjects' axioms; or a
blank node nothing links to, but through `owl:annotatedSource` or `owl:annotatedTarget`: a general class axiom, an
n-ary axiom, a negative property assertion or a SWRL rule (no owner), or a reified `owl:Axiom` (owned by its
`owl:annotatedSource`). The IRIs inside a root are the objects reached from it through blank nodes, but `owl:`/`rdf:`
types and `rdf:nil`, with the properties of annotations (any property outside the OWL, RDF and SWRL vocabularies and
RDFS's axiom properties); for a reified axiom only its annotations, and not its source, property or
target, since the axiom itself is counted from its own triple.

Tables: `term` (every IRI the map names; ids are `name_order` ranks for entities), `bnode_root`, `bnode_ref`, and
`bnode_count`: how many triples with a blank subject have each IRI as object or predicate, which a usage count
subtracts from Fuseki's unfiltered counts, as filtering out blank subjects there makes TDB2 decode every subject.
"""

import asyncio
import json
import resource
import sqlite3
import sys
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

import httpx2

from browl.fuseki import FusekiClient
from browl.rdf import IRI, Literal

DATA = Path(__file__).parent / "data"
OWL = "http://www.w3.org/2002/07/owl#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
TYPE = f"{RDF}type"
NIL = f"{RDF}nil"
SOURCE, PROPERTY, TARGET = (f"{OWL}annotated{part}" for part in ("Source", "Property", "Target"))
ANNOTATED = {SOURCE, PROPERTY, TARGET}
AXIOM_TYPES = {f"{OWL}Axiom", f"{OWL}Annotation"}
SWRL = "http://www.w3.org/2003/11/swrl#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
STRUCTURAL = {f"{RDFS}{name}" for name in ("subClassOf", "subPropertyOf", "domain", "range")}
PROLOGUE = "PREFIX afn: <http://jena.apache.org/ARQ/function#>"
QUERY = """SELECT ?s ?p ?o WHERE {
  ?s0 ?p ?o0
  FILTER (isBlank(?s0) || isBlank(?o0))
  BIND (IF(isBlank(?s0), afn:bnode(?s0), ?s0) AS ?s)
  BIND (IF(isBlank(?o0), afn:bnode(?o0), IF(isLiteral(?o0), 0, ?o0)) AS ?o)
}"""
TYPED_QUERY = """SELECT ?s ?p ?o ?l WHERE {
  {
    { VALUES ?type { owl:Axiom owl:Annotation owl:Restriction owl:AllDisjointClasses owl:AllDisjointProperties
                     owl:AllDifferent owl:NegativePropertyAssertion swrl:Imp swrl:AtomList swrl:ClassAtom
                     swrl:IndividualPropertyAtom swrl:DatavaluedPropertyAtom swrl:BuiltinAtom swrl:SameIndividualAtom
                     swrl:DifferentIndividualsAtom swrl:DataRangeAtom }
      ?b rdf:type ?type }
    UNION { VALUES ?link { rdf:first owl:unionOf owl:intersectionOf owl:complementOf owl:oneOf owl:onDatatype
                           owl:datatypeComplementOf owl:withRestrictions }
            ?b ?link ?any }
    UNION { ?b owl:inverseOf ?any FILTER isBlank(?b) }
  }
  { ?b ?p ?o0 BIND (afn:bnode(?b) AS ?s) BIND (IF(isBlank(?o0), afn:bnode(?o0), IF(isLiteral(?o0), 0, ?o0)) AS ?o)
    BIND (IF(isLiteral(?o0), STR(?o0), "") AS ?l) }
  UNION
  { ?x ?p ?b FILTER isIRI(?x) BIND (?x AS ?s) BIND (afn:bnode(?b) AS ?o) }
}"""
"""The owner map's query in another shape, with `--typed`: it finds blank nodes by their OWL and SWRL types and the
predicates of anonymous classes, datatypes, lists and inverse properties, through Fuseki's predicate-first index,
instead of testing every triple's nodes, which makes TDB2 decode them all. It misses other untyped blank nodes, such as
anonymous individuals."""
TYPED_PROLOGUE = (
    PROLOGUE
    + "\nPREFIX owl: <http://www.w3.org/2002/07/owl#>\nPREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>"
    + "\nPREFIX swrl: <http://www.w3.org/2003/11/swrl#>"
)
SCHEMA = """
DROP TABLE IF EXISTS term;
DROP TABLE IF EXISTS bnode_root;
DROP TABLE IF EXISTS bnode_ref;
DROP TABLE IF EXISTS bnode_count;
CREATE TABLE term (id INTEGER PRIMARY KEY, iri TEXT NOT NULL);
CREATE TABLE bnode_root (id INTEGER PRIMARY KEY, label TEXT NOT NULL, owner INTEGER, kind INTEGER,
  reified INTEGER NOT NULL);
CREATE TABLE bnode_ref (term INTEGER NOT NULL, root INTEGER NOT NULL, PRIMARY KEY (term, root)) WITHOUT ROWID;
CREATE TABLE bnode_count (term INTEGER PRIMARY KEY, object INTEGER NOT NULL, predicate INTEGER NOT NULL);
"""
INDEXES = """
CREATE UNIQUE INDEX term_iri ON term (iri);
CREATE INDEX bnode_root_owner ON bnode_root (owner);
"""


def term_value(term):
    """Return an IRI as itself, a blank node's label as '_:label', and a literal as None."""
    if isinstance(term, IRI):
        return term
    assert isinstance(term, Literal)
    return None if term.datatype.endswith("integer") else f"_:{term.value}"


async def fetch(url: str, typed: bool):
    children: dict[str, list[tuple[str, str]]] = {}
    iri_parent: dict[str, list[tuple[str, str]]] = {}
    blank_parented: set[str] = set()
    target_only: set[str] = set()
    axioms: set[str] = set()
    stats = Counter()
    blank_object: Counter = Counter()
    blank_predicate: Counter = Counter()
    async with (
        httpx2.AsyncClient(base_url=url, timeout=600) as http,
        FusekiClient(http).bulk_select(
            "gen-1", TYPED_QUERY if typed else QUERY, TYPED_PROLOGUE if typed else PROLOGUE
        ) as stream,
    ):
        seen: set[tuple] = set()
        s_col, p_col, o_col = (stream.variables.index(name) for name in ("s", "p", "o"))
        l_col = stream.variables.index("l") if typed else None
        async for row in stream:
            s, p, o = term_value(row[s_col]), str(row[p_col]), term_value(row[o_col])
            if typed:  # a blank node can match several of the query's branches; ?l tells literals apart
                key = (s, p, o, row[l_col])
                if key in seen:
                    continue
                seen.add(key)
            stats["rows"] += 1
            if s.startswith("_:"):
                children.setdefault(s, []).append((p, o))
                blank_predicate[p] += 1
                if o is not None and not o.startswith("_:"):
                    blank_object[o] += 1
                if p == TYPE and o in AXIOM_TYPES:
                    axioms.add(s)
                if o is not None and o.startswith("_:"):
                    if p == TARGET:
                        target_only.add(o)
                    elif p != SOURCE:
                        if o in blank_parented:
                            stats["blank nodes with several blank parents"] += 1
                        blank_parented.add(o)
            elif o is not None and o.startswith("_:"):
                if o in iri_parent:
                    stats["blank nodes with several IRI parents"] += 1
                iri_parent.setdefault(o, []).append((s, p))
    return children, iri_parent, blank_parented, target_only, axioms, stats, blank_object, blank_predicate


def inside(root: str, children, axioms) -> set[str]:
    found: set[str] = set()
    stack = [root]
    seen = {root}
    while stack:
        node = stack.pop()
        reified = node in axioms
        for p, o in children.get(node, ()):
            if reified and p in ANNOTATED:
                continue
            if not (p.startswith((OWL, RDF, SWRL)) or p in STRUCTURAL):
                found.add(p)  # an annotation's property, on a reified axiom, a SWRL rule or another blank node
            if o is None:
                continue
            if o.startswith("_:"):
                if o not in seen:
                    seen.add(o)
                    stack.append(o)
            elif not (o == NIL or (p == TYPE and o.startswith((OWL, RDF)))):
                found.add(o)
    return found


def build(
    name: str, children, iri_parent, blank_parented, target_only, axioms, stats, blank_object, blank_predicate
) -> dict:
    path = DATA / f"{name}.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(SCHEMA)
        terms = dict(connection.execute("SELECT iri, rank FROM name_order"))
        next_id = len(terms)

        def term(iri: str) -> int:
            nonlocal next_id
            if iri not in terms:
                terms[iri] = next_id
                next_id += 1
            return terms[iri]

        roots, refs = [], []

        def add(node: str, owner: str | None, kind: str | None, reified: bool, iris: set[str]) -> None:
            root_id = len(roots)
            stats["roots, " + ("reified" if reified else "owned" if owner else "no owner")] += 1
            owner_id = None if owner is None else term(owner)
            kind_id = None if kind is None or kind.startswith("_:") else term(kind)
            roots.append((root_id, node[2:], owner_id, kind_id, reified))
            refs.extend((term(iri), root_id) for iri in iris)

        for node, parents in iri_parent.items():  # each top-level triple, even if its blank node is shared
            iris = inside(node, children, axioms)
            if node in blank_parented:
                stats["top-level blank nodes also inside another blank node"] += 1
            for owner, predicate in parents:
                add(node, owner, predicate, False, iris)
        for node in set(children) | target_only:
            if node in blank_parented or node in iri_parent:
                continue
            reified = node in axioms
            if node in target_only and not reified:
                stats["reified targets with no other parent (copies), skipped"] += 1
                continue
            if reified:
                source = next((o for p, o in children[node] if p == SOURCE), None)
                owner = source if source is not None and not source.startswith("_:") else None
                kind = next((o for p, o in children[node] if p == PROPERTY), None)
            else:
                owner = None
                kind = next((o if p == TYPE else p for p, o in children[node] if p != TYPE or o), None)
            iris = inside(node, children, axioms)
            if reified:  # what the axiom names is counted from its own triple
                iris -= {o for p, o in children[node] if p in ANNOTATED}
            if reified and not iris:
                stats["reified axioms without IRIs in their annotations, skipped"] += 1
                continue
            add(node, owner, kind, reified, iris)
        refs.sort()
        connection.executemany("INSERT INTO term VALUES (?, ?)", ((i, iri) for iri, i in terms.items()))
        connection.executemany("INSERT INTO bnode_root VALUES (?, ?, ?, ?, ?)", roots)
        connection.executemany("INSERT INTO bnode_ref VALUES (?, ?)", refs)
        counted = set(blank_object) | set(blank_predicate)
        connection.executemany(
            "INSERT INTO bnode_count VALUES (?, ?, ?)",
            sorted((term(iri), blank_object[iri], blank_predicate[iri]) for iri in counted),
        )
        connection.executescript(INDEXES)
        connection.commit()
        connection.execute("ANALYZE")
        sizes = dict(
            connection.execute(
                "SELECT name, sum(pgsize) FROM dbstat WHERE name IN"
                " ('term', 'term_iri', 'bnode_root', 'bnode_root_owner', 'bnode_ref', 'bnode_count') GROUP BY name"
            )
        )
    stats["refs"] = len(refs)
    return {"MiB": {key: round(value / 2**20, 1) for key, value in sizes.items()}, **stats}


async def main() -> None:
    url, name = sys.argv[1:3]
    typed = "--typed" in sys.argv[3:]
    start = time.perf_counter()
    fetched = await fetch(url, typed)
    query_s = time.perf_counter() - start
    result = build(name, *fetched)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**30
    print(
        json.dumps(
            {
                "query_and_parse_s": round(query_s, 1),
                "build_s": round(time.perf_counter() - start - query_s, 1),
                **result,
                "peak_rss_GiB": round(peak, 2),
            },
            indent=1,
        )
    )


asyncio.run(main())
