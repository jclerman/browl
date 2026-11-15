"""S-3 spike: load, descendant counts, usage and derived-database size at N-1 scale.

Run from research/S-3:  .venv/bin/python scale.py <ontology file> [heap]
Uses S-1's JVM and loading helpers (research/S-1/spike.py) and robot.jar.
"""

import json
import random
import resource
import sqlite3
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "S-1"))
import spike  # noqa: E402
from pyroaring import BitMap  # noqa: E402

RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
NAME_PROPS = {
    RDFS_LABEL,
    "http://www.geneontology.org/formats/oboInOwl#hasExactSynonym",
    "http://www.geneontology.org/formats/oboInOwl#hasRelatedSynonym",
    "http://www.geneontology.org/formats/oboInOwl#hasBroadSynonym",
    "http://www.geneontology.org/formats/oboInOwl#hasNarrowSynonym",
}


def log(**kw: object) -> None:
    print(json.dumps(kw), flush=True)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20


def normalize(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).casefold()


def peak_heap_mb() -> float:
    from java.lang.management import ManagementFactory, MemoryType

    return (
        sum(
            p.getPeakUsage().getUsed()
            for p in ManagementFactory.getMemoryPoolMXBeans()
            if p.getType() == MemoryType.HEAP
        )
        / 2**20
    )


def reset_peaks() -> None:
    from java.lang.management import ManagementFactory

    for p in ManagementFactory.getMemoryPoolMXBeans():
        p.resetPeakUsage()


def descendant_counts(n: int, parents: list[list[int]]) -> tuple[list[int], list[int]]:
    """Children and distinct-descendant counts per node, bottom-up over a DAG with roaring bitmaps."""
    children: list[list[int]] = [[] for _ in range(n)]
    for c, ps in enumerate(parents):
        for p in ps:
            children[p].append(c)
    pending = [len(ch) for ch in children]  # children not yet merged into this node
    waiting = [len(ps) for ps in parents]  # parents that still need this node's set
    sets: dict[int, BitMap] = {}
    counts = [0] * n
    ready = [i for i in range(n) if pending[i] == 0]
    peak_sets = 0
    while ready:
        node = ready.pop()
        s = BitMap()
        for c in children[node]:
            s |= sets[c]
            s.add(c)
            waiting[c] -= 1
            if waiting[c] == 0:
                del sets[c]
        counts[node] = len(s)
        if parents[node]:
            s.run_optimize()
            sets[node] = s
            for p in parents[node]:
                pending[p] -= 1
                if pending[p] == 0:
                    ready.append(p)
        peak_sets = max(peak_sets, len(sets))
    if any(pending):
        raise ValueError("cycle in hierarchy")
    return [len(ch) for ch in children], counts


def main(path: str, heap: str) -> None:
    spike.start_jvm(heap)
    from org.semanticweb.owlapi.model import AxiomType
    from org.semanticweb.owlapi.model.parameters import Imports

    reset_peaks()
    base = spike.java_heap_used_mb()
    t = time.perf_counter()
    _, ont = spike.load(HERE / path)
    load_s = time.perf_counter() - t
    log(
        step="load",
        file=path,
        load_s=round(load_s, 1),
        heap_retained_mb=round(spike.java_heap_used_mb() - base),
        heap_peak_mb=round(peak_heap_mb()),
        axioms=ont.getAxiomCount(),
        logical=ont.getLogicalAxiomCount(),
        classes=ont.getClassesInSignature().size(),
    )

    # Entities and named subclass edges, pulled through JPype.
    t = time.perf_counter()
    classes = [str(c.getIRI()) for c in ont.getClassesInSignature()]
    index = {iri: i for i, iri in enumerate(classes)}
    parents: list[list[int]] = [[] for _ in classes]
    edges = 0
    for ax in ont.getAxioms(AxiomType.SUBCLASS_OF):
        sub, sup = ax.getSubClass(), ax.getSuperClass()
        if sub.isAnonymous() or sup.isAnonymous() or sup.isOWLThing():
            continue
        parents[index[str(sub.asOWLClass().getIRI())]].append(index[str(sup.asOWLClass().getIRI())])
        edges += 1
    log(
        step="edges",
        seconds=round(time.perf_counter() - t, 1),
        classes=len(classes),
        named_edges=edges,
        multi_parent=sum(len(p) > 1 for p in parents),
        python_rss_mb=round(rss_mb()),
    )

    t = time.perf_counter()
    n_children, n_desc = descendant_counts(len(classes), parents)
    log(
        step="counts",
        seconds=round(time.perf_counter() - t, 1),
        max_descendants=max(n_desc),
        roots=sum(not p for p in parents),
        python_rss_mb=round(rss_mb()),
    )

    # Name values (direct only) and display names.
    t = time.perf_counter()
    names: list[tuple[str, str, str, str | None]] = []
    iri_refs: dict[str, int] = defaultdict(int)  # annotation values that are IRIs: candidate Usage rows
    for ax in ont.getAxioms(AxiomType.ANNOTATION_ASSERTION):
        v = ax.getValue()
        if v.isLiteral():
            prop = str(ax.getProperty().getIRI())
            if prop in NAME_PROPS:
                lit = v.asLiteral().get()
                names.append((str(ax.getSubject()), prop, str(lit.getLiteral()), str(lit.getLang()) or None))
        else:
            iri_refs[str(v)] += 1
    log(
        step="names",
        seconds=round(time.perf_counter() - t, 1),
        name_values=len(names),
        annotation_iri_values=sum(iri_refs.values()),
        python_rss_mb=round(rss_mb()),
    )

    # Usage: the OWLAPI index for the most referenced entities, including by annotation value.
    from org.semanticweb.owlapi.model import IRI

    df = ont.getOWLOntologyManager().getOWLDataFactory()
    top = sorted(iri_refs.items(), key=lambda kv: -kv[1])[:3]
    widest = sorted(range(len(classes)), key=lambda i: -n_children[i])[:3]
    for iri in [k for k, _ in top] + [classes[i] for i in widest]:
        t = time.perf_counter()
        by_entity = ont.getReferencingAxioms(df.getOWLClass(IRI.create(iri)), Imports.INCLUDED).size()
        t1 = time.perf_counter()
        by_iri = ont.getReferencingAxioms(IRI.create(iri)).size()  # also annotation values
        t2 = time.perf_counter()
        refs = ont.getReferencingAxioms(IRI.create(iri))
        ordered = sorted(str(a.getSubject()) for a in refs if a.isOfType(AxiomType.ANNOTATION_ASSERTION))[:50]
        t3 = time.perf_counter()
        log(
            step="usage",
            iri=iri,
            as_entity=by_entity,
            as_iri=by_iri,
            entity_ms=round((t1 - t) * 1000, 1),
            iri_ms=round((t2 - t1) * 1000, 1),
            stable_first_page_ms=round((t3 - t2) * 1000),
            first=len(ordered),
        )

    # Derived database: the tables pre-caching would build (no usage table yet), then a usage table.
    db_path = HERE / "data" / f"derived-{Path(path).stem}.sqlite"
    db_path.unlink(missing_ok=True)
    t = time.perf_counter()
    db = sqlite3.connect(db_path)
    db.executescript("""
        PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;
        CREATE TABLE entity (id INTEGER PRIMARY KEY, iri TEXT NOT NULL, type TEXT NOT NULL, url_path TEXT NOT NULL,
                             display_name TEXT);
        CREATE TABLE name_value (entity INTEGER NOT NULL, property TEXT NOT NULL, value TEXT NOT NULL,
                                 normalized TEXT NOT NULL, lang TEXT, direct INTEGER NOT NULL);
        CREATE TABLE edge (parent INTEGER NOT NULL, child INTEGER NOT NULL);
        CREATE TABLE counts (node INTEGER PRIMARY KEY, children INTEGER NOT NULL, descendants INTEGER NOT NULL);
    """)
    label = {s: v for s, p, v, _ in names if p == RDFS_LABEL}
    db.executemany(
        "INSERT INTO entity VALUES (?, ?, 'class', ?, ?)",
        ((i, iri, "/" + iri.split("://", 1)[-1], label.get(iri)) for i, iri in enumerate(classes)),
    )
    db.executemany(
        "INSERT INTO name_value VALUES (?, ?, ?, ?, ?, 1)",
        ((index[s], p, v, normalize(v), lang) for s, p, v, lang in names if s in index),
    )
    db.executemany("INSERT INTO edge VALUES (?, ?)", ((p, c) for c, ps in enumerate(parents) for p in ps))
    db.executemany("INSERT INTO counts VALUES (?, ?, ?)", ((i, n_children[i], n_desc[i]) for i in range(len(classes))))
    db.executescript("""
        CREATE UNIQUE INDEX entity_iri ON entity (iri); CREATE INDEX entity_path ON entity (url_path);
        CREATE INDEX name_norm ON name_value (normalized); CREATE INDEX edge_parent ON edge (parent, child);
    """)
    db.commit()
    build_s = time.perf_counter() - t
    size_core = db_path.stat().st_size
    t = time.perf_counter()
    db.executescript("""CREATE TABLE usage (referenced INTEGER NOT NULL, referring INTEGER NOT NULL,
                                            kind TEXT NOT NULL);""")
    rows = ((p, c, "SubClassOf") for c, ps in enumerate(parents) for p in ps)
    db.executemany("INSERT INTO usage VALUES (?, ?, ?)", rows)
    annot_rows = 0
    for ax in ont.getAxioms(AxiomType.ANNOTATION_ASSERTION):
        v = ax.getValue()
        if not v.isLiteral():
            ref, sub = index.get(str(v)), index.get(str(ax.getSubject()))
            if ref is not None and sub is not None:
                db.execute("INSERT INTO usage VALUES (?, ?, 'AnnotationAssertion')", (ref, sub))
                annot_rows += 1
    db.execute("CREATE INDEX usage_ref ON usage (referenced, referring)")
    db.commit()
    log(
        step="derived_db",
        core_build_s=round(build_s, 1),
        core_mb=round(size_core / 2**20),
        usage_build_s=round(time.perf_counter() - t, 1),
        usage_rows=edges + annot_rows,
        total_mb=round(db_path.stat().st_size / 2**20),
    )

    # Autocomplete-style prefix query against the derived database (a preview of N-2).
    random.seed(1)
    probes = [normalize(v)[:3] for _, _, v, _ in random.sample(names, 500)]
    ms = []
    for q in probes:
        t = time.perf_counter()
        db.execute(
            "SELECT entity, value FROM name_value WHERE normalized >= ? AND normalized < ? LIMIT 20", (q, q + "￿")
        ).fetchall()
        ms.append((time.perf_counter() - t) * 1000)
    log(step="prefix_query", **spike.summary(ms))
    log(step="done", python_rss_peak_mb=round(rss_mb()), heap_peak_mb=round(peak_heap_mb()))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "28g")
