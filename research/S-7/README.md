# S-7: Usage tab timings and the blank-node owner map — findings

Spike for [#106](https://github.com/jclerman/browl/issues/106), which de-risks P-27
([#41](https://github.com/jclerman/browl/issues/41)): does the tentative Usage tab design in
[DESIGN.md §2.5 and §2.8](../../docs/DESIGN.md#28-entity-pages) hold on NCBITaxon, Uberon and HPO? It measures the
blank-node owner map (build and correctness against the OWLAPI), the counts, streaming and sorting every usage, the
walk in name order, the threshold between them, the usage cache, and pages rendered through the fragment path.

## Conclusion

**The design's parts work, and the owner map is correct, but three of its estimates are wrong, and one of them
changes how the count must be queried.**

- **The owner map gives the OWLAPI's usages exactly:** 323 of 325 Uberon entities and 323 of 324 HPO entities, hubs
  included, have the same usage axioms as the OWLAPI's referencing axioms (plus annotations by value) minus their own
  axioms, and every count equals its usage axioms; the three differences are the ontology header's annotations, which
  the OWLAPI keeps as ontology annotations rather than axioms. NCBITaxon's check, rerun with a 14 GB heap once
  memory was free, matches for all 325 entities checked (section 1). Getting there took three fixes to the tentative
  map, below: a root is a top-level triple, not a blank node, since stores share blank nodes between subjects; a
  reified axiom's source, property and target aren't its references; and annotations on any blank node count.
- **Counts must not filter the subject.** `FILTER (isIRI(?s))` makes TDB2 decode every subject: NCBITaxon's largest
  counts take 11.7–18.9 s that way, against 0.21–0.26 s unfiltered (up to 0.47 s for the first query after a
  restart). The owner map can supply the correction (how many triples with a blank subject name each IRI), so counts
  stay under 300 ms warm.
- **Streaming costs 8–11 µs per usage on NCBITaxon**, not 5–6, and 2.4–4 µs on Uberon and HPO: 25 s to stream and
  sort NCBITaxon's largest (3.08 M), 28 s with the cache's inserts.
- **On NCBITaxon, usages are clustered in name order as a rule, not as an exception:** nearly every class's usages
  are its subclasses, whose names share a prefix ("Lepidoptera sp. …", "Vibrio sp. …"). Of NCBITaxon's 278 entities
  with over 1,000 usages, 30 fill page 1 within 20 K candidates; the other 248 need 22 K up to 2.99 M of its 3.03 M
  (about 0.25–38 s). But the hubs too large to stream are the opposite: ranks, annotation properties and synonym
  types, used by most taxa, which fill page 1 within 50–6,000 candidates (7–75 ms). Every clustered hub is small
  enough to stream (0.5 s at most, at 47.8 K usages).
- **Recommended T = 20,000** usages: streaming up to it takes at most 0.21 s on NCBITaxon and 0.09 s on Uberon and
  HPO. The uniform-density crossover the design assumed is 12.6 K on NCBITaxon (1.9–2.1 K on Uberon and HPO), but
  clustering moves the real one far higher for class hubs, and the walk still wins by far for the dense hubs above T.
- **Worst first view:** NCBITaxon about 0.75 s (a clustered class hub just above T: the walk's time limit, then a
  0.3–0.5 s cache build), else at most 0.21 s streamed or 75 ms walked, plus the page's 9–78 ms; Uberon and HPO
  about 0.15 s, page included. A deep page of a 2.5–3.1 M hub waits 21–28 s for its cache.

So the design stands with T = 20 K, unfiltered counts corrected by the owner map, a walk time limit of about 0.25 s,
and the revised owner-map rules. [Proposed design changes](#proposed-design-changes) lists the edits to §2.5 and §2.8.

## Setup

- Apple M3 Pro (12 cores, 36 GB RAM), on the Mac, not in containers. Measured 2026-11-20. browl's Docker stack (app,
  Fuseki, sandbox) was stopped before the first measurement, so it was down for all of them. But a Protégé session,
  not part of the spike, held about 15 GB, and by the NCBITaxon runs macOS had 27–30 GB in swap; so NCBITaxon's
  timings may be somewhat pessimistic. Those comparable with S-4's (counts, streaming in `curl`) came out about the
  same as S-4 measured.
- Jena Fuseki 6.2.0 (Java 21, 8 GB heap) on port 3031, serving one of S-4's TDB2 stores at a time as `/gen-1`
  (`fuseki.sh`): NCBITaxon (24.0 M triples, 3.03 M entities), Uberon (1.17 M, 27.3 K) and HPO 2026-09-01 (0.91 M, 33.1
  K). No imports.
- `precache.py` builds browl's derived database (the entities and names hooks, `browl.precache`) and adds
  `name_order`, each entity IRI ranked by casefolded display name, then IRI: the order the Usage tab sorts subjects
  in and the walk reads. `ownermap.py` builds the owner map into the same file. `bench.py` runs the measurements,
  through browl's Fuseki client (`browl.fuseki.FusekiClient`), so TSV parsing is included. `check.py` compares the usage
  sets with the OWLAPI's (`robot.jar`).
- "Warm" is the median (and p95 or max) of 5 runs after a warm-up; "first" is the first query after Fuseki started, so
  its JIT and TDB2 caches are cold. The OS page cache stayed warm: purging it needs `sudo purge`, which the agent
  couldn't run, so a truly cold disk wasn't measured.
- A usage is an axiom, as the design counts it: a triple `?s ?p X` or, for a property, `?s X ?o`, with an IRI subject
  other than X; or an owner-map root that names X and isn't X's own.

## Results

### 1. The owner map

One bulk query streams every triple with a blank node as subject or object (blank nodes as their TDB2 labels,
`afn:bnode`, literals as 0); Python finds the roots and the IRIs inside each, and writes three tables.

| | Uberon | HPO | NCBITaxon |
| --- | --- | --- | --- |
| Rows streamed | 837 K | 602 K | 2.69 M |
| Query and parse | 5.2 s | 3.8 s | 70.5 s |
| ... typed (below) | 7.5 s | 5.9 s | 26.2 s |
| Build in Python and SQLite | 0.9 s | 0.7 s | 15.9 s (12.7 s typed) |
| Roots: top-level triples (owned) | 59,706 | 27,696 | 0 |
| Roots: reified axioms with IRIs in their annotations | 92,024 | 63,254 | 538,849 |
| Roots: no owner (general axioms, n-ary, SWRL rules) | 1,438 | 25 | 0 |
| References (root, IRI) | 258 K | 198 K | 1.08 M |
| Tables: `bnode_root` + index, `bnode_ref`, `bnode_count` | 8.7 + 3.0 + 0.3 MiB | 5.1 + 2.3 + 0.3 MiB | 32.2 + 14.1 + 4.0 MiB |
| Peak memory (Python) | 0.4 GB | 0.3 GB | 1.9 GB (2.6 GB typed) |

- **A store shares blank nodes between subjects.** Uberon has 1,175 blank nodes with several IRI parents (6,750 pairs
  of `rdfs:subClassOf` triples on one node, e.g. the same `part_of some X` under two classes), as the OWLAPI that
  wrote it reuses identical anonymous expressions. So a root is a top-level triple (owner, predicate, blank node),
  not a blank node; keyed by the node alone, the map lost those owners. No blank node had several blank parents.
- **A reified axiom's references are only its annotations' IRIs and properties**, not its source, property or
  target: the axiom is counted from its own triple. Counting them double-counted three Uberon axioms annotated with
  their own property. Reified targets are the main triple's own blank node in these stores, never copies.
- **Annotations count on any blank node**, not just `owl:Axiom`s: Uberon's three SWRL rules carry `rdfs:comment`.
  Any predicate outside the OWL, RDF and SWRL vocabularies and RDFS's axiom properties is an annotation property.
- **The query is slow on NCBITaxon because of its filter, not its size.** `FILTER (isBlank(?s) || isBlank(?o))`
  makes TDB2 decode both nodes of all 24 M triples (`COUNT(*)` of every triple takes 2.5 s; with `isBlank(?s)` alone
  23 s). A **typed** shape (`ownermap.py --typed`) finds the blank nodes through the predicate index instead: those
  typed `owl:Axiom`, `owl:Restriction`, `owl:AllDisjointClasses` and the like (and SWRL's types), and the subjects of
  `rdf:first`, `owl:unionOf`, `owl:intersectionOf`, `owl:complementOf`, `owl:oneOf` and the datatype constructors;
  then their triples and IRI parents. It gave the same map (references and counts) on all three, in 26 s instead of
  70 s on NCBITaxon, though 1.5–2 s slower on Uberon and HPO. Its first version missed one Uberon reference, an
  inverse property (`_:x owl:inverseOf BFO_0000066`, untyped) in a property chain, until `owl:inverseOf` joined the
  list; it would still miss other untyped blank nodes, such as anonymous individuals, which none of these have. So
  it's faster but depends on the RDF mapping's types; the plain filter is safe, and slow only on large stores.
- **`bnode_count`** holds, per IRI, the triples with a blank subject that have it as object or predicate, for the
  counts (section 2). The spike's `term` table (every IRI the map names, 189 MiB on NCBITaxon) stands for
  `entity.id`; production would key by entity, and keep only the few IRIs that aren't entities.

**Correctness** (`check.py`). For each entity X, the OWLAPI's set is `getReferencingAxioms` of each entity with X's
IRI, plus the annotation assertions with X as subject or value, plus the axioms with an annotation whose value is X,
all without annotations or declarations, minus X's own axioms (those the OWLAPI parses from `DESCRIBE X`). browl's set
is what the OWLAPI parses from the usages' triples: `?s ?p X` and `?s X ?o` (with blank objects' closure) and the owner
map's roots, with their declaration types. The sample: the 25 most used entities and 300 random ones (and
PATO_0000460 in HPO).

| | Uberon | HPO | NCBITaxon |
| --- | --- | --- | --- |
| Loading the whole ontology in the OWLAPI | 4.3 s | 3.7 s | not run |
| Entities checked | 325 | 324 | — |
| Usage sets identical | 323 | 323 | — |
| OWLAPI's set covered by own axioms and usages | 325 | 324 | — |
| Count (section 2) equals the usage axioms | 325 | 324 | — |

- **NCBITaxon matches too, for all 325 entities checked.** The first attempt ran out of a 6 GB heap, and a 14 GB run
  stalled with the machine swapping (Setup). Rerun with a 14 GB heap once memory was free (2026-11-20, Fuseki's 8 GB
  alongside), the OWLAPI loaded NCBITaxon in 116 s. It covered 300 sampled entities and 25 hubs, with `--max-usages
  200000` leaving out the hubs whose usages wouldn't fit the heap as fragments. The largest checked has 134 K usages
  (`TAXRANK_0000005` and `NCBITaxon_genus`), 1.17 M usages in all. Every usage set equals the OWLAPI's minus the
  entity's own axioms, and every count equals its usage axioms. Hubs over 200 K usages rest on the counts matching the
  filtered queries for NCBITaxon's 30 largest entities (section 2).

- **The differences are the ontology header's annotations:** `dc:contributor` (41) and `rdfs:comment` (1) on the
  ontology IRI in Uberon, `rdfs:comment` in HPO. The OWLAPI holds them as ontology annotations, not axioms; browl
  lists them, as usages by the ontology's entity.
- **The fragment's types must be declaration types only.** Fetching every named type of every IRI a usage page
  mentions, as `entity-types.rq` does for a page's own axioms, added other entities' axioms to the page: property
  characteristics, and class assertions such as ORCID IRIs typed as human (`NCBITaxon_9606`) in Uberon. The page fetch
  asks only for `owl:Class`, the property types, `owl:NamedIndividual`, `rdfs:Datatype` and `swrl:Variable` (without
  the last, SWRL rules didn't parse).
- **General class axioms are both own axioms and usages.** The Overview lists them for every class they mention
  (§2.8), and U-26 lists them under every entity they mention, so a GCI shows on both tabs; for UBERON_0007626, six of
  its twelve usages. Likewise a symmetric axiom stated on the other entity.
- Literals: the OWLAPI parses `"x"` from RDF/XML and `"x"^^xsd:string` from N-Triples as different literals, so the
  comparison reads them as one.

### 2. Counts

Count = Fuseki's unfiltered count of `?s ?p X` (and `?s X ?o` for a property), minus X's triples about itself (one
lookup, `{ X ?p X } UNION { X X ?o }`), minus the owner map's `bnode_count`, plus its roots that name X. Milliseconds.

| | Uberon | HPO | NCBITaxon |
| --- | --- | --- | --- |
| Random sample (200), warm p50 / p95 | 1.9 / 2.2 | 1.9 / 2.1 | 2.2 / 3.1 |
| Random sample, first p50 / max | 3.4 / 8.5 | 3.0 / 7.0 | 3.1 / 8.5 |
| Largest, warm | 25 (hasDbXref, 122 K), 11 (hasExactSynonym, 40 K) | 17 (hasDbXref, 84 K), 11 (hasExactSynonym, 45 K) | 260 (hasDbXref, 3.08 M), 257 (hasOBONamespace, 3.03 M), 230 (has_rank, 2.76 M), 206 (species, 2.50 M) |
| Largest, first | 165, 23 | 140, 42 | 475, 335, 272, 214 |
| Largest, with `FILTER (isIRI(?s) && ?s != X)` | 626, 73 | 444, 135 | 18,928, 17,274, 14,405, 11,706 |
| Over 300 ms, warm | none | none | none |
| Over 300 ms, first query after a restart | none | none | 2 (hasDbXref, hasOBONamespace) |

- **Time grows with the count**, about 85 ns per usage on NCBITaxon: 14 ms at 134 K, 92 ms at 539 K, 0.26 s at 3.1 M.
  A count through the owner map alone (hasSynonymType, 539 K reified axioms) is a SQLite join: tens of ms.
- **The filter costs 50–70× on NCBITaxon** (12–25× on Uberon and HPO), since TDB2 decodes each subject to test it;
  the unfiltered count reads only the index.
  The subtraction gave the same count as the filtered query for all 30 of NCBITaxon's largest, and the same as the
  OWLAPI's usages for every entity checked (section 1).
- **Caching counts per generation** (as §2.8 plans) is still worth it for the few over 100 ms: 13 entities on
  NCBITaxon, none on Uberon or HPO.

### 3. Streaming and sorting

Stream every usage from Fuseki (`?s ?p` of `?s ?p X`, and `?s` of `?s X ?o`) through browl's client, add the owner
map's roots, join each subject's place in name order in SQLite (a temporary table joined to `name_order`), and sort.

| | Uberon | HPO | NCBITaxon |
| --- | --- | --- | --- |
| Entities over 1,000 usages | 48 | 31 | 278 |
| µs per usage, median (1 K–10 K / 10 K–100 K / over 100 K) | 3.5 / 3.8 / 2.4 | 4.3 / 4.1 / — | 11.3 / 10.5 / 8.2 |
| Largest: usages, ms | 121,833: 294 | 84,247: 168 | 3,075,835: 24,922 |
| of which Fuseki / names / sort | 170 / 76 / 48 | 90 / 46 / 32 | 16,891 / 6,192 / 1,839 |
| Worst at 20 K or fewer | 77 ms (17.8 K) | 91 ms (15.7 K) | 213 ms (18.6 K) |
| Worst at 50 K or fewer | 154 ms (40.5 K) | 168 ms (45.3 K) | 506 ms (47.8 K) |

- **Most of the cost is Fuseki producing rows and browl parsing them.** `curl` alone streamed hasOBONamespace's 3.03
  M subjects in 13.8 s (15.6 s with the subject filter), 4.6–5.2 µs per row, as S-4 found. browl's TSV parsing adds
  1–7 µs per row, most with two IRI columns (NCBITaxon_500585: 71 ms in `curl`, 421 ms through browl). Streaming only
  the subject, where the predicate is `rdfs:subClassOf` anyway, would help.
- **Owner-map usages are cheap**, coming from SQLite: hasDbXref in Uberon, mostly in axiom annotations, costs 2.4 µs
  per usage; hasSynonymType in NCBITaxon 7.8 µs.
- The names join (a temporary table of streamed IRIs, then a join to the name index) is 20–35% of the time; the
  Python sort under 10%.

### 4. Walk in name order

Read `name_order` in batches; for each batch, ask Fuseki for its members' usages of X (`VALUES ?s { … }` joined to `?s
?p X`, and `?s X ?o` for a property) and the owner map for roots in its rank range; stop once the page is filled.

| Batch size | 500 | 1,000 | 2,000 | 5,000 |
| --- | --- | --- | --- | --- |
| µs per candidate, Uberon | 13–22 | 11–16 | 10–14 | 10–13 |
| µs per candidate, HPO | 11–21 | 10–18 | 9–16 | 9–15 |
| µs per candidate, NCBITaxon | 11–24 | 10–18 | 10–17 | 10–20 |

Page 1 and page 10, NCBITaxon, batch 1,000 (best of 3, after a warm-up):

| Entity | Usages | Page 1: candidates, ms | Page 10: candidates, ms |
| --- | --- | --- | --- |
| hasDbXref (property) | 3.08 M | 1,000: 18 | 1,000: 18 |
| species (TAXRANK_0000006) | 2.50 M | 1,000: 14 | 1,000: 14 |
| genus (TAXRANK_0000005) | 134 K | 5,000: 60 | 12,000: 134 |
| strain (TAXRANK_0001001) | 49 K | 3,000: 31 | 22,000: 245 |
| genbank common name (owner map) | 33 K | 6,000: 74 | 48,000: 623 |
| OMO_0003003 | 14 K | 6,000: 75 | 113,000: 1,459 |

Clustered hubs, page 1 (batch 2,000 and 5,000, one run each):

| Entity | Usages | Name of its first usage | Candidates | ms |
| --- | --- | --- | --- | --- |
| unclassified Bacteria (NCBITaxon_49928) | 34 K | "anammox bacterium enrichment culture …" | 44 K | 455 |
| environmental samples, Bacteria (NCBITaxon_48479) | 24 K | "activated sludge bacterium …" | 134 K | 1,370 |
| unclassified Lepidoptera (NCBITaxon_500585) | 48 K | "Lepidoptera sp. 038 PS-2011" | 1.57 M | 18,995 |
| unclassified Vibrio (NCBITaxon_2614977) | 14 K | "Vibrio sp. 0407CG132" | 2.96 M | 36,398 |

- **10–14 µs per candidate** at batches of 1,000 or more, close to the design's 10; smaller batches pay a per-query
  overhead. Most of it is Fuseki's `VALUES` join.
- **Clustering is the rule for NCBITaxon's classes.** The sweep (section 3) records where page 1's walk would stop
  for every entity over 1,000 usages. On NCBITaxon only 6 of the 234 between 1 K and 10 K usages, and 11 of the 31
  between 10 K and 100 K, fill page 1 within 20 K candidates; the deepest need 2.99 M of 3.03 M (unclassified
  Wolbachia). Uberon and HPO are mostly uniform (35 of 36 and 20 of 21 under 10 K usages fill page 1 within 20 K), but
  with only 27–33 K entities a whole walk takes 0.3–0.5 s. Their clustered ones are of the same kind: GO_0065007
  (biological regulation), whose usages are "regulation of …" definitions, needs 21 K of HPO's 33 K candidates (205
  ms) and 14 K of Uberon's 27 K (150 ms), against 3–4 ms to stream.
- **Above T it's the other way round.** Of NCBITaxon's 28 entities over 20 K usages, the 19 used by most taxa (ranks,
  annotation properties, synonym types) fill page 1 within 50–5,519 candidates, 7–75 ms; the 9 clustered ones are
  classes of 24–48 K usages, which stream and sort in 0.27–0.51 s.
- **Deep pages** cost in proportion: page 10 of genbank common name 0.6 s, of OMO_0003003 1.5 s; a walk is only
  worth it for the first few pages.

### 5. The threshold

With b µs per streamed usage and c µs per walked candidate, page 1 of an entity with n usages spread evenly over N
entities costs b·n to stream and c·50·N/n to walk: equal at T = √(50·c·N/b).

| | Uberon | HPO | NCBITaxon |
| --- | --- | --- | --- |
| b, c (µs) | 3.8, 12 | 4.1, 12 | 10.5, 11 |
| Uniform crossover T | 1.9 K | 2.1 K | 12.6 K |
| Stream at that T | 7 ms | 9 ms | about 130 ms |
| Entities over 20 K usages | 7 | 5 | 28 |

- **The uniform crossover doesn't hold on NCBITaxon**: just above 12.6 K, most entities are clustered classes whose
  walks need hundreds of thousands to millions of candidates (the 1.3–38 s above), while streaming them costs 0.15–0.5
  s. Streaming wins up to about 50 K for classes, and the walk wins for the dense hubs at any size above about 10 K.
- **T = 20 K** keeps every streamed page at 0.21 s or less on NCBITaxon (0.09 s on Uberon and HPO), within N-3's 300
  ms with room for the page itself (section 7). Above it, the walk with a time limit of about 0.25 s (about 20 K
  candidates) fills page 1 of every dense hub; the 9 clustered NCBITaxon classes over 20 K hit the limit and wait for
  their cache, which builds in 0.3–0.5 s. The worst first view is then about 0.75 s, for those 9.
- **Uberon and HPO need neither the walk nor the cache**: their largest entity streams in 0.29 and 0.17 s. A fixed T
  sends their 7 and 5 largest through the walk and the cache anyway, at no cost to speak of.
- T could instead be a time budget, choosing by estimated cost (count × µs per usage), but a fixed count is simpler and
  the measured costs per usage vary less than 3× between ontologies.

### 6. Usage cache

Each built entity's sorted list, one row per usage (entity, position, subject rank, kind, detail), in a `WITHOUT
ROWID` table keyed by (entity, position).

| | Uberon | HPO | NCBITaxon |
| --- | --- | --- | --- |
| Largest: build (stream, sort, insert) | 0.29 + 0.09 s | 0.17 + 0.06 s | 24.9 + 2.8 s (hasDbXref, 3.08 M) |
| Bytes per row | 21.9 | 19.2 | 36.2 |
| Every entity over T = 20 K: entities, usages | 7, 298 K | 5, 214 K | 28, 19.1 M |
| Every entity over T: build, size | 0.9 s, 6 MiB | 0.7 s, 4 MiB | about 159 s + 18 s, 658 MiB |

- The 6 hubs over 2 M usages are 87% of NCBITaxon's total and take 21–28 s each; the other 22 take 0.2–5 s.
- The rows are larger on NCBITaxon because the detail column holds each direct usage's predicate IRI as text; an id
  would halve it.
- Building every entity over T right after a load, rather than on first view, would cost NCBITaxon about 3 minutes
  (1.5 at two at once), on top of its load, and spare the 21–28 s waits for deep pages of its largest hubs.

### 7. Pages

A page of 50 usages through the fragment path: the direct triples (`CONSTRUCT` with `VALUES`), the roots' owner
triples and closures (`DESCRIBE <_:label> …`, and for reified axioms their main triple), the declaration types of
every IRI mentioned; then the OWLAPI parses the fragment and browl converts the axioms to its tree and renders their
Manchester text (`browl.axioms.convert_axioms`). The largest entities and a random sample with usages; pages 1, 2 and
100 where they exist. Milliseconds, after a warm-up.

| | Pages | Fetch p50 / p95 / max | Parse and render p50 / p95 / max | Total p50 / p95 / max | Slowest |
| --- | --- | --- | --- | --- | --- |
| Uberon | 149 | 5.0 / 10.5 / 14.2 | 2.1 / 8.3 / 22.0 | 7.3 / 18.5 / 35.2 | GO_0065007, page 2 (78 KB of triples) |
| HPO | 135 | 4.7 / 17.6 / 41.6 | 2.5 / 20.0 / 42.5 | 7.3 / 37.8 / 73.1 | RO_0002573, page 100 (175 KB) |
| NCBITaxon | 74 | 5.3 / 9.8 / 26.0 | 3.1 / 9.4 / 72.8 | 8.5 / 22.6 / 78.1 | has_rank, page 100 (a 73 ms parse; 15 KB) |

- Pages of class expressions (PATO_0000460 "abnormal", in 6,268 class expressions in HPO, whose own page takes 611
  ms) take 45–50 ms: a page fetches its 50 roots by label, never walking back from the entity.
- A property's page fetches every value its 50 subjects have, since the stream holds only the subject: Uberon's
  hasDbXref page 2 had 76 axioms for 50 usages. Streaming the object as well, or paging by subject, would fix it.

### 8. Subjects not in `entity`

| | Uberon | HPO | NCBITaxon |
| --- | --- | --- | --- |
| Triples with an undeclared IRI subject | 261, from 70 subjects | 0 | 0 |
| Owner-map roots owned by an undeclared IRI | 98 | 0 | 0 |
| Triples with the ontology as subject (in `entity`) | 163 | 15 | 8 |

Uberon's undeclared subjects are ORCID IRIs (typed as individuals of NCBITaxon_9606), and a few OMO and IAO terms it
uses without declaring. So the design's last sort group (subjects not in `entity`, until U-18) is real but tiny.

## Verdict on the tentative design

| Part | Verdict | Why |
| --- | --- | --- |
| Usages from three sources | Confirmed | Equal to the OWLAPI's usages, but the ontology header's annotations (section 1). |
| Blank-node owner map, one bulk query per load | Revised | Key roots by top-level triple, not blank node; reified axioms reference only their annotations; annotations count on any blank node; add per-IRI counts of blank-subject triples. 4.5–6 s on Uberon and HPO, 86 s on NCBITaxon (39 s with the typed query). |
| Count: Fuseki's two counts plus the roots | Revised | Unfiltered counts, minus `bnode_count` and X's triples about itself; filtering `?s` costs 50–70×. 0.26 s warm, 0.47 s first, for NCBITaxon's largest. |
| Counts cached in memory per generation | Confirmed | 13 entities over 100 ms on NCBITaxon. |
| Order: display name, then IRI; general axioms; undeclared subjects | Confirmed | Undeclared subjects: 261 usages in Uberon, none in the others. |
| Pages of 50, page count from the count | Confirmed | Pages take 7–9 ms at p50 and 78 ms at most; a property's page needs each usage's object. |
| Stream and sort up to T (T perhaps 15 K, at most 0.1 s) | Revised | T = 20 K; at most 0.21 s on NCBITaxon, as streaming costs 8–11 µs per usage there. |
| Walk in name order above T | Confirmed, narrowed | Fast for the dense hubs above T (7–75 ms); the clustered ones above T are 9 NCBITaxon classes of 24–48 K, which hit a 0.25 s limit. Worth it for page 1 and a few more only. |
| Usage cache, built on first view, two at once | Confirmed | 21–28 s for NCBITaxon's six largest, 0.2–5 s for the rest; 658 MiB for every entity over T. |
| "Streaming 5–6 µs per row; 15–18 s for 2.5–3 M" | Refuted | 8–11 µs per usage through browl on NCBITaxon; 21–25 s, 28 s with the cache's inserts. |
| "Worst first view about 0.5 s" | Revised | About 0.75 s, for a clustered class just above T; 0.3 s for anything else. |
| Risk: usages clustered in name order | Confirmed, worse than thought | The rule for NCBITaxon's classes, but confined to entities small enough to stream. |
| Risk: subjects missing from `entity` | Small | Section 8. |

## Proposed design changes

**§2.5, blank-node owner map:** drop "tentative". Say that it's built by one bulk query per load, over the blank
nodes found through their types (`owl:Axiom`, `owl:Restriction`, the n-ary and SWRL types) and the predicates of
anonymous classes, lists, datatypes and inverse properties: 26 s on NCBITaxon, against 70 s for a filter on node kind,
which makes TDB2 decode every triple's nodes (5–7 s either way on Uberon and HPO). It relies on the RDF mapping's types,
and would miss anonymous individuals, which U-18 should revisit; the filter is the fallback. The map holds:

- one root per top-level triple with a blank object (owner, predicate, blank-node label), since a store can share one
  blank node between several subjects' axioms; one per blank node nothing links to (general class axioms, n-ary
  axioms, negative property assertions, SWRL rules: no owner); and one per reified `owl:Axiom` with IRIs in its
  annotations (owned by its `owl:annotatedSource`);
- each root's IRIs: the objects reached through blank nodes (but OWL and RDF types and `rdf:nil`), and the properties
  of annotations (any predicate outside the OWL, RDF and SWRL vocabularies and RDFS's axiom properties); for a reified
  axiom only its annotations', not its source, property or target;
- per IRI, how many triples with a blank subject have it as object or predicate, which the count subtracts.

Its size: 12 MiB on Uberon, 8 on HPO, 50 on NCBITaxon, keyed by entity id.

**§2.8, Usage tab:** drop "tentative", and change:

- **The count:** Fuseki's counts of `?s ?p <entity>` (and `?s <entity> ?o`) without a filter on `?s`, minus the
  entity's triples about itself and the owner map's blank-subject counts, plus the owner map's roots. 2 ms for most
  entities; 0.21–0.26 s for NCBITaxon's largest (2.5–3.1 M), 0.47 s for the first query after a restart. Filtering
  the subject would take 12–19 s.
- **Two ways:** up to T = 20,000 usages, stream, sort and slice (at most 0.21 s on NCBITaxon, 0.1 s on Uberon and HPO;
  8–11 µs per usage on NCBITaxon, 2–4 on the others). Above it, walk with a time limit of about 0.25 s, which fills
  page 1 of the dense hubs (ranks, annotation properties) in 7–75 ms; usages of NCBITaxon's classes cluster in name
  order (the subclasses' shared prefixes), so walks for its 9 classes over T reach the limit and wait for the cache
  (0.3–0.5 s to build). Drop the claim that the two ways bound each other with the worst case near T: clustering
  breaks it.
- **Usage cache:** 21–28 s to build for NCBITaxon's six largest (2.5–3.1 M usages), 0.2–5 s for its other 22 over T;
  658 MiB for all 28. Note the option of building every entity over T right after a load (about 3 minutes on
  NCBITaxon, about a second on Uberon and HPO), which would spare first views the wait.
- **The walk's index:** the walk needs entities in name order; the spike's `name_order` table took 490 MiB on
  NCBITaxon (323 table, 166 index). Numbering `entity.id` in name order, or an index on the sort key, would cost less.
- **Page fetch:** a usage page fetches only declaration types (`owl:Class`, the property types,
  `owl:NamedIndividual`, `rdfs:Datatype`, `swrl:Variable`) of the IRIs it mentions, not every type as
  `entity-types.rq` does, or other entities' class assertions and property characteristics join the page; and a
  property's usages need their objects in the stream, so a page holds exactly 50.
- **General class axioms and symmetric axioms** stated on another entity show on both tabs (Overview and Usage); say
  so, or leave the Usage tab's count without them.
- Replace the estimates in the Risks table: "Sorting needs every usage, 15–18 s" becomes 21–28 s (stream, sort and
  cache) for NCBITaxon's largest.

P-27's acceptance criteria (#41) should then name T = 20 K, the unfiltered count with the owner map's correction,
and the walk's time limit.

## Reproducing

The data is regenerated, not committed: `data/` is ignored by git. The spike reads S-4's TDB2 stores, which these
build:

```sh
make robot-jar                    # .cache/robot.jar, which check.py and bench.py use
research/S-1/fetch.sh             # Uberon 2026-11-05 into research/S-1/data/downloads/
research/S-3/fetch.sh             # NCBITaxon v2026-11-09 into research/S-3/data/
research/S-4/fetch.sh             # Jena and Fuseki 6.2.0 into research/S-4/lib/
mkdir -p research/S-4/data/hpo && curl -fsSL -o research/S-4/data/hpo/hp.owl \
  http://purl.obolibrary.org/obo/hp/releases/2026-09-01/hp.owl   # HPO 2026-09-01, as P-12
cd research/S-4
lib/apache-jena-6.2.0/bin/tdb2.tdbloader --loc data/tdb-uberon ../S-1/data/downloads/uberon.owl
lib/apache-jena-6.2.0/bin/tdb2.tdbloader --loc data/tdb-hpo data/hpo/hp.owl
lib/apache-jena-6.2.0/bin/tdb2.xloader --loc data/tdb-ncbitaxon --tmpdir data/tmp ../S-3/data/ncbitaxon.owl
```

Then, from the repository's root:

```sh
export JAVA_HOME=$(/usr/libexec/java_home -v 21)
research/S-7/fuseki.sh research/S-4/data/tdb-uberon 8g       # or tdb-hpo, tdb-ncbitaxon; port 3031, /gen-1
U=http://localhost:3031
poetry run python research/S-7/precache.py $U uberon         # data/uberon.sqlite: entity, names, name_order
poetry run python research/S-7/ownermap.py $U uberon         # owner map (--typed for the typed query)
poetry run python research/S-7/bench.py $U uberon census     # usage sizes (usage_size table) and question 8
research/S-7/fuseki.sh research/S-4/data/tdb-uberon 8g       # restart, so counts' first runs are cold
poetry run python research/S-7/bench.py $U uberon counts --top 20 --sample 200
poetry run python research/S-7/bench.py $U uberon sweep --min 1000 --cache 10
poetry run python research/S-7/bench.py $U uberon walk --iri IRI --batch 500 --batch 1000 --page 1 --page 10
poetry run python research/S-7/bench.py $U uberon pages --top 20 --sample 150 --page 1 --page 2 --page 100
poetry run python research/S-7/check.py $U uberon research/S-1/data/downloads/uberon.owl --sample 300
```

For HPO the file is `research/S-4/data/hpo/hp.owl`, for NCBITaxon `research/S-3/data/ncbitaxon.owl`, with
`--heap 14g --max-usages 200000` (`--max-usages` leaves out the hubs too large for the heap). For the clustered
walks, `--runs 1 --limit 90`. Each command writes its results to `data/NAME-COMMAND.json`; `data/` is ignored by git.
