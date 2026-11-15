# S-4: serving from Fuseki or pre-rendered SQLite — findings

Spike for [#47](https://github.com/jclerman/browl/issues/47). [S-3](../S-3/README.md) found that the current design,
the ontology held in an in-process OWLAPI, can't serve anything until the OWLAPI has parsed the whole ontology: 71–74 s
for NCBITaxon on every restart. This spike measures two alternatives:

- **A. Fuseki / TDB2, queried on demand.** The ontology is bulk-loaded into a TDB2 store, which persists on disk.
  Entity pages query Fuseki when they're requested. Only whole-ontology data (the autocomplete dictionary and the edges
  behind the tree counts) is fetched up front, one SPARQL query each, and saved in SQLite.
- **B. OWLAPI at build time only.** The upload is loaded into the OWLAPI once, every logical axiom is rendered to HTML,
  and the app serves pages from SQLite alone.

## Conclusion

**Both fix the restart problem; A is the better fit, so I recommend it.**

| | Current design | **A: Fuseki on demand** | B: pre-rendered SQLite |
| --- | --- | --- | --- |
| Ready after a restart | 71–74 s (NCBITaxon) | **0.6–0.9 s** | about instant (opens a file) |
| Upload, Uberon, to ready | about 10–16 s (estimate) | **7.0 s (measured end to end)** | about 11 s (estimate) |
| Upload, NCBITaxon, to ready | about 9 min (estimate) | **about 5 min (estimate)** | about 7–8 min (estimate) |
| Entity page | 0.3 ms | about 7–10 ms (queries plus rendering) | under 1 ms |
| Memory while serving (NCBITaxon) | 3.4–3.7 GB of heap held | not measured (TDB2 is disk-based, memory-mapped) | small |
| Containers | one | **two (app and Fuseki); see O-1 below** | one |

Uploads exclude reasoning, which S-2 covers. Estimates add up measured stages from S-3 and this spike. Why A over B:

- **Faster uploads**, at every size, because nothing has to be loaded into the OWLAPI or extracted through JPype.
- **New page features are just queries.** In B, everything a page shows must be computed at upload time; in A, the
  store can answer any question about the ontology when it's asked.
- **A SPARQL endpoint for free**, which makes the Later requirement U-23 (RDF by content negotiation) nearly trivial.
- **Manchester syntax is unchanged:** the OWLAPI renders each page from the triples fetched for it, with output
  identical to rendering from the full ontology (300 of 300 Uberon classes).

What A costs: a second service, a small JVM still in the app (for rendering fragments), and pages about 30× slower than
now (7–10 ms instead of 0.3 ms), which is still about 30× under the N-3 target.

**One decision for you: O-1** says the service runs as a single container. A needs Fuseki running too. Options: run
Fuseki as a second process in the same container (keeps O-1 as written, but the container runs two processes), or relax
O-1 to "one `docker compose up`" with two services (cleaner; what I'd suggest).

## Setup

- Same machine as S-1 and S-3: Apple M3 Pro (12 cores), 36 GB RAM. Apache Jena and Fuseki 6.2.0 on Temurin 21.0.13
  (Jena 6 needs Java 21); the OWLAPI parts reuse S-1's code and `robot.jar`.
- Data: NCBITaxon v2026-11-09 (S-3) and Uberon 2026-11-05 (S-1).
- Fuseki runs with a 4 GB heap limit, without its web UI (`fuseki.sh`). Queries go over HTTP from Python (`bench.py`);
  "first" is a cold query, "best" the fastest of three.
- Some Uberon timings ran while the NCBITaxon bulk load was running; they may be slightly pessimistic.

## Results

### 1. Bulk load and restart

| | Uberon | NCBITaxon |
| --- | --- | --- |
| Triples | 1.17 M | 24.0 M |
| TDB2 load | 3.9 s (`tdb2.tdbloader`) | 185 s (`tdb2.xloader`) |
| Store on disk | 339 MB | 2.6 GB |
| Fuseki ready after start | 0.67–0.87 s | 0.63–0.70 s |

TDB2 reads the RDF/XML release directly; no conversion step is needed. The store persists, so a restart only starts
Fuseki, whatever the ontology's size.

### 2. Queries an entity page needs (NCBITaxon)

| Query | Typical | Slowest case seen |
| --- | --- | --- |
| The entity's own triples | 2–4 ms | 37 ms (cold) |
| Children, first 50 | 2.5 ms | 30 ms (50 children, 40,000 deep, of a class with 47,783) |
| Count of references to the entity | 2 ms | 265–380 ms (the species rank, referenced about 2.5 M times) |
| A page of 50 references, in index order | 2–4 ms | 187 ms (offset 2,000,000) |

Query shape mattered, as expected:

- **Don't sort big result sets.** `ORDER BY` on the 2.5 M references took 15.5 s; paging in the store's own index
  order (no `ORDER BY`) takes milliseconds and is stable as long as the store doesn't change. The Usage tab can page
  that way.
- **Always `LIMIT` children.** All 47,783 children of one class took 57–242 ms; the first 50 take 2.5 ms.
- **Don't use property paths for whole subtrees.** Counting descendants with `rdfs:subClassOf+` took 147–881 ms on
  NCBITaxon and 281 ms on Uberon. Descendant counts stay precomputed (S-3's bitmaps), as planned.
- **Counts of very large reference sets can't be made fast by rewording.** Grouping by predicate or restricting to one
  predicate gave 288–444 ms; the count has to touch every reference. Either compute those counts at upload (one query,
  below) or count on first view and cache the result.

### 3. Up-front queries

| Query | Uberon | NCBITaxon |
| --- | --- | --- |
| Name dictionary: labels and synonyms, including values of their sub-properties (`rdfs:subPropertyOf*`) | 0.76–0.93 s, 92,028 rows | 41–48 s, 3.56 M rows |
| Named subclass edges | 0.32–0.33 s, 35,623 rows | 17.6–19.2 s, 3.0 M rows |
| Reference counts, IRIs referenced over 1,000 times | — | 41 s, 274 IRIs |

- These return the same data S-3 extracted through the OWLAPI. On Uberon the dictionary has 833 more values (92,028 vs
  91,195): values of sub-properties of the name properties, which the RDFS entailment in §2.5 wants anyway.
- Bulk results stream at about 90–100 K rows per second, whatever the query looks like: dropping filters or the
  property path saved at most 10–15%, and CSV was twice as slow as TSV. `curl` alone took 17.4 s for the edges (15.7 s
  in Jena's binary Thrift format), so the cost is in Fuseki producing results, about 5–6 µs per row, not in browl.
- For NCBITaxon, two ways to cut this, not tried: skip the up-front reference-count query (41 s) and count on first
  view instead, and extract the dictionary and edges from the upload file in parallel with the TDB2 load.

### 4. Manchester syntax from fetched triples

For each class: `DESCRIBE` the class (its triples and their blank-node closure), fetch the triples where it is the
object of `owl:disjointWith` or `owl:equivalentClass`, and fetch the types of every IRI mentioned. The OWLAPI parses
that
fragment as a small ontology and renders it as in S-1.

- **Identical output** to rendering from the full ontology for **300 of 300** random Uberon classes. Before adding the
  second query it was 294 of 300: the six misses were `DisjointWith` axioms stated on the other class.
- **Time:** fetching p50 4.8 ms, p95 6.4 ms (three queries); parsing and rendering p50 2.0 ms, p95 3.5 ms. So about
  7–10 ms per page.
- **Not yet covered:** n-ary `owl:AllDisjointClasses` axioms and general class axioms that mention the class, since the
  class is only an object deep inside those. They need one more lookup, like the Usage tab's.

The app still needs a JVM for this, but only for parsing and rendering small fragments: no whole-ontology heap.

### 5. RDFS: direct and entailed relations

- **Direct** relations (children, asserted name values) are plain triples: fast (section 2).
- **Entailed name values** come free in the dictionary query through `rdfs:subPropertyOf*`; the path costs little.
- **Entailed subclass relations** through `rdfs:subClassOf+` are slow for big subtrees (section 2).
- **Materializing the subclass closure** as plain triples (as a named graph would in production; here, a separate
  store) makes those fast: counting descendants took 7–21 ms (230 ms for NCBITaxon's root, about 3 M descendants), and a
  page of descendants about 3 ms. But NCBITaxon's closure is 59.2 M triples, 2.5× the ontology: 25 s to compute, 11.6
  min
  to load, 4.4 GB on disk. For Uberon it would be small.

So the closure layer is the fallback for any feature that needs to list or count descendants on demand. browl's v1
pages only need descendant counts, which the bitmaps compute in seconds, so it isn't needed yet. Loading the ontology
twice (with and without RDFS) isn't needed either: the closure graph is the only part that would differ.

### 6. Option B: pre-rendering every axiom

Rendering all 98,186 of Uberon's logical axioms to HTML and storing them in SQLite took 1.5 s (15 µs each), 26 MB.
Extrapolated to NCBITaxon's 3.0 M logical axioms: about 45 s. On top of that, B keeps the current design's upload costs
(convert, load into the OWLAPI, extract through JPype; S-3), which is why its uploads are slower than A's.

### 7. Option A end to end (Uberon)

`pipeline.py` runs everything from the upload file to ready to serve: TDB2 load, Fuseki start, the dictionary and
edges queries, descendant counts, and the SQLite writes (dictionary and counts).

| Stage | Time |
| --- | --- |
| TDB2 load | 4.71 s |
| Fuseki start | 0.67 s |
| Dictionary query | 0.93 s |
| Edges query | 0.32 s |
| Descendant counts | 0.05 s |
| SQLite writes | 0.31 s |
| **Total** | **7.0 s** |

A real upload also needs the entity list (every declared entity with its type, for URLs and display names): one more
query, likely about as fast as the edges query.

## What A would change in the plan

If you choose A, the design doc and issues need revising in one pass (not done in this PR, pending your decision):

- **§2.1 architecture:** Fuseki/TDB2 holds the ontology; SQLite holds aggregates (dictionary, counts, entity list, job
  state, admins); the app's JVM only renders fragments. No whole-ontology OWLAPI load anywhere.
- **§2.4 load pipeline:** reasoning (ROBOT) writes RDF; then a TDB2 bulk load into a new store, swapped in when ready;
  then the up-front queries. No `.ofn` conversion.
- **§2.5 derived data:** built from SPARQL queries instead of the OWLAPI. The `usage` table may go: Fuseki finds
  references by value directly and pages them in index order. But a reference inside a class expression reaches the
  entity through blank nodes, and walking up to the owning axiom wasn't tested.
- **§2.8 rendering:** the fragment approach above.
- **Requirements:** O-1 (single container), as above.
- **Issues:** P-3 (Compose with Fuseki), P-4 (becomes a fragment renderer plus a Fuseki client), P-8 to P-12 (load and
  aggregates via TDB2 and SPARQL), P-15 (fragment rendering), P-27 (Usage from Fuseki).

## Reproducing

```sh
cd research/S-4
export JAVA_HOME=$(/usr/libexec/java_home -v 21)
./fetch.sh                                           # Jena and Fuseki 6.2.0 into lib/
lib/apache-jena-6.2.0/bin/tdb2.tdbloader --loc data/tdb-uberon ../S-1/data/downloads/uberon.owl
lib/apache-jena-6.2.0/bin/tdb2.xloader --loc data/tdb-ncbitaxon --tmpdir data/tmp ../S-3/data/ncbitaxon.owl
./fuseki.sh data/tdb-ncbitaxon                       # start Fuseki on a store; prints seconds until ready
../S-3/.venv/bin/python bench.py pages <IRI>...      # page-query latencies
../S-3/.venv/bin/python bench.py bulk                # up-front queries
./fuseki.sh data/tdb-uberon && ../S-3/.venv/bin/python bench.py render 300   # Manchester from fragments
../S-3/.venv/bin/python pipeline.py ../S-1/data/downloads/uberon.owl uberon  # option A end to end
```

The closure test used `closure.py` (closure from an edges TSV saved from the edges query) and `tdb2.xloader`.
