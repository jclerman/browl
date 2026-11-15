"""Option A end to end: upload file -> TDB2 -> Fuseki -> up-front queries -> counts -> SQLite. Prints stage times."""

import csv
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "S-3"))
import bench  # noqa: E402
from scale import descendant_counts, normalize  # noqa: E402

src, name = Path(sys.argv[1]).resolve(), sys.argv[2]
loc = HERE / "data" / f"tdb-{name}-pipeline"
stages: dict[str, float] = {}
t0 = t = time.perf_counter()


def mark(stage: str) -> None:
    global t
    now = time.perf_counter()
    stages[stage] = round(now - t, 2)
    t = now


shutil.rmtree(loc, ignore_errors=True)
subprocess.run(
    [str(HERE / "lib/apache-jena-6.2.0/bin/tdb2.tdbloader"), "--loc", str(loc), str(src)],
    check=True,
    capture_output=True,
)
mark("tdb2_load")
subprocess.run([str(HERE / "fuseki.sh"), str(loc)], check=True, capture_output=True)
mark("fuseki_start")
_, names = bench.query("""SELECT ?s ?np ?v WHERE { VALUES ?np { rdfs:label oio:hasExactSynonym oio:hasRelatedSynonym
    oio:hasBroadSynonym oio:hasNarrowSynonym } ?p rdfs:subPropertyOf* ?np . ?s ?p ?v FILTER isLiteral(?v) }""")
mark("dictionary_query")
_, edges = bench.query("SELECT ?c ?p WHERE { ?c rdfs:subClassOf ?p FILTER (isIRI(?c) && isIRI(?p)) }")
mark("edges_query")
pairs = [line.split("\t") for line in edges.decode().splitlines()[1:]]
nodes = {n: i for i, n in enumerate({x for pair in pairs for x in pair})}
parents: list[list[int]] = [[] for _ in nodes]
for c, p in pairs:
    parents[nodes[c]].append(nodes[p])
n_children, n_desc = descendant_counts(len(nodes), parents)
mark("counts")
db_path = HERE / "data" / f"aggregates-{name}.sqlite"
db_path.unlink(missing_ok=True)
db = sqlite3.connect(db_path)
db.executescript("""CREATE TABLE name_value (iri TEXT, property TEXT, value TEXT, normalized TEXT);
    CREATE TABLE counts (iri TEXT PRIMARY KEY, children INTEGER, descendants INTEGER);""")
rows = csv.reader(io.StringIO(names.decode()), delimiter="\t", quoting=csv.QUOTE_NONE)
next(rows)
db.executemany("INSERT INTO name_value VALUES (?, ?, ?, ?)", ((s, p, v, normalize(v)) for s, p, v in rows))
db.executemany("INSERT INTO counts VALUES (?, ?, ?)", ((n, n_children[i], n_desc[i]) for n, i in nodes.items()))
db.execute("CREATE INDEX name_norm ON name_value (normalized)")
db.commit()
mark("sqlite")
print({**stages, "total_s": round(time.perf_counter() - t0, 1), "db_mb": round(os.path.getsize(db_path) / 2**20, 1)})
