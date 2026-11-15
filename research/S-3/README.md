# S-3: scale test with a large ontology — findings

Spike for [#13](https://github.com/jclerman/browl/issues/13): at N-1 scale (millions of entities), how much heap does
the
OWLAPI need, how long does loading take, can distinct descendant counts be computed, and how big are the `usage` table
and the derived database? Design context: [`docs/DESIGN.md`](../../docs/DESIGN.md) §2.4, §2.5, §2.12, §2.14. Builds on
[S-1](../S-1/README.md), whose JVM and loading code it reuses.

**Conclusion: N-1 scale is workable on one machine: NCBITaxon loads from `.ofn` in a 6 GB heap.** Serve the ontology
from `.ofn`, which loads twice as fast and needs half the heap. Count descendants with roaring bitmaps (compressed sets
of integers; see [Descendant counts](#descendant-counts)). Keep the `usage` table: the OWLAPI can't find references by
annotation value, and its IRI lookup is a full scan.

## Setup

- Same machine as S-1: Apple M3 Pro (12 cores), 36 GB RAM; OpenJDK 17.0.17; Python 3.12.8; ROBOT v1.9.11 (OWLAPI 4.5);
  JPype 1.7.1; [pyroaring](https://github.com/Ezibenroc/PyRoaringBitMap) 1.2.0 (roaring bitmaps, below).
- **NCBITaxon** release v2026-11-09 (`ncbitaxon.owl`, RDF/XML, 2.1 GB): 21,334,880 axioms, 3,025,030 classes, a pure
  tree (no class has two parents), 3.56 M label and synonym values, 5.5 M annotation values that are IRIs (mostly
  `has_rank`).
- Uberon (S-1's timing ontology) re-run for a hierarchy with multiple parents: 26,739 classes, 8,170 with more than one
  parent.
- `scale.py` runs every step in one process (24 GB heap limit) and writes a prototype of the derived database
  (§2.5): `entity`, `name_value` (labels and synonyms), `edge`, `counts`, then `usage`.

## Results

### Loading and heap

| NCBITaxon as… | Load time | Heap retained | Loads with |
| --- | --- | --- | --- |
| RDF/XML (release file) | 112–130 s | 6.6–6.8 GB | `-Xmx12g`; fails at `-Xmx8g` |
| `.ofn` | 71–74 s | 3.4–3.7 GB | `-Xmx6g` |

- Converting to `.ofn` with ROBOT took 163 s once (`-Xmx24g`), giving a 1.7 GB file.
- Retained heap is about 0.32 KB per axiom from RDF/XML, and about half that from `.ofn`. Uberon (S-1) retained about
  0.45 KB per axiom from RDF/XML, so the per-axiom figure depends on the ontology's shape.
- With a generous limit, the JVM's observed peak was 16–20 GB, but that reflects how much it chose to use, not how much
  it needs: the "Loads with" column is the measured requirement.

**→ design (§2.4): serve from `.ofn`.** Save the built ontology as `.ofn`, whatever format was uploaded: ROBOT's
reasoning step can write it directly, and an upload without reasoning gets a one-time conversion. Every later load (app
restarts) is then about 2× faster and needs about half the heap.

**→ design (§2.12): `APP_JVM_MAX_HEAP`** defaults to `8g`: NCBITaxon served from `.ofn` loads in 6 GB, and the rest is
headroom for rendering. Smaller deployments can lower it; the upload page reports "memory limit" when it's too small.

### Pre-caching cost

Pulling data out of the OWLAPI into Python through JPype dominates pre-caching:

| Step (NCBITaxon) | Time |
| --- | --- |
| Class list and 3.0 M named subclass edges | 54 s |
| Descendant counts (roaring bitmaps) | 4.2 s |
| 21 M annotation assertions scanned for names and IRI values | 146 s |
| Derived database without `usage` (inserts and indexes) | 22 s |
| `usage` table (8.5 M rows) | 97 s |

About 8–18 µs per axiom crosses from Java to Python. Load plus pre-caching comes to roughly 6–9 minutes for
NCBITaxon, which the design already accepts for uploads (§2.4); restarts skip pre-caching. The process peaked at about
12 GB resident (JVM and Python together).

### Where the time goes

**Restart** (built ontology and derived database already on disk): the app can't serve pages until the OWLAPI has
re-parsed the ontology, 71–74 s for NCBITaxon from `.ofn`. Pre-caching is skipped. That one parse is the whole cost.

**Upload**, NCBITaxon, no reasoner: about 2.5 min converting to `.ofn` (once), 1.2 min loading, then about 5 min of
pre-caching, of which about 3.3 min is moving data from Java to Python through JPype and 2 min is SQLite inserts and
indexes. Reasoning would add its own time (S-2).

So the current design is never ready in under about 70 s at this scale, even on restart. Two designs could change that,
and are left to a separate spike, [S-4](https://github.com/jclerman/browl/issues/47):

- **Fuseki / TDB2, queried on demand:** the ontology is bulk-loaded once into a store that persists on disk, so restart
  is fast. Entity pages query it when requested; only whole-ontology data (the autocomplete dictionary, the edges
  behind the tree counts) is fetched up front, each with one SPARQL query. Bulk-load time, query latency and how to
  produce Manchester syntax need measuring.
- **OWLAPI at build time only:** pre-render every logical axiom to HTML during pre-caching and serve pages from SQLite
  alone. Restart becomes opening a file, and the serving app needs no JVM. Uploads get somewhat slower.

### Descendant counts

A class's distinct descendants are a set of class numbers, and the counting pass (§2.5) builds each class's set as the
union of its children's sets. At 3 M classes those sets must be small and fast to merge. A
[roaring bitmap](https://roaringbitmap.org/) is a compressed set of integers built for exactly this: it splits the
numbers into blocks of 65,536 and stores each block in whichever form is smallest (a sorted list for a few members, a
plain bitmap for many, or ranges for long consecutive runs), and unions work block by block. It is widely used in search
engines and databases. [pyroaring](https://github.com/Ezibenroc/PyRoaringBitMap) is its Python binding, wrapping the C
library CRoaring.

The bottom-up pass over roaring bitmaps handled NCBITaxon's 3.0 M classes in 4.2 s and Uberon's DAG in under
0.1 s, with no memory problem: a node's set is freed as soon as all its parents have merged it, and run-length
compression keeps tree-shaped sets small. No SQLite closure-table fallback is needed.

**→ design (§2.5): use roaring bitmaps** (`pyroaring`), not Python ints: an int bitset costs memory proportional to the
highest index, about 370 KB per set at this scale.

### Usage

| Lookup (NCBITaxon) | Time |
| --- | --- |
| References to an entity, OWLAPI index (up to 47,785 referencing axioms) | 0.3–8 ms |
| References to an IRI, OWLAPI `getReferencingAxioms(IRI)` | 4.8–11 s |

- Lookup by entity is indexed and fast. Lookup by IRI is a full scan, and it doesn't find annotation assertions whose
  *value* is the IRI: for the most frequently used `has_rank` value (the species rank), it found 4.
- U-26 requires those annotation-value references, so they can only come from browl's own table.
- The `usage` table (subclass references plus annotation values that are entities) has 8.5 M rows and adds about
  400 MB to the database.

**→ design (§2.5, P-27): keep the `usage` table.** It is the only way to list annotation-value references, and it gives
counts and stable paging for entities referenced millions of times. S-1's suggestion that the OWLAPI index might be
enough holds only for logical axioms.

### Derived database

| | NCBITaxon | Uberon |
| --- | --- | --- |
| Without `usage` | 1.3 GB | 21 MB |
| With `usage` | 1.7 GB | 23 MB |

A prefix query on `name_value.normalized` (the v1 autocomplete query, §2.6) took p95 0.39 ms over 3.56 M values,
against the N-2 target of 100 ms. P-24 still needs to test the full ranked query.

## Reproducing

```sh
cd research/S-3
../S-1/fetch.sh                      # robot.jar (shared with S-1)
./fetch.sh                           # NCBITaxon into data/
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
export JAVA_HOME=$(/usr/libexec/java_home -v 17)
.venv/bin/python scale.py data/ncbitaxon.owl 24g        # all steps; prints one JSON line per step
java -Xmx24g -jar ../S-1/lib/robot.jar convert -i data/ncbitaxon.owl -o data/ncbitaxon.ofn
.venv/bin/python loadonly.py data/ncbitaxon.ofn 24g     # load time and retained heap for one file and heap limit
```

For Uberon, link or copy S-1's `uberon.owl` into `data/` and run `scale.py data/uberon.owl 8g`.
