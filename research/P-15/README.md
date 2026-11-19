# P-15: Manchester rendering from fetched fragments — measurements

Measurements behind P-15 ([#36](https://github.com/jclerman/browl/issues/36)): whether the axioms rendered from a
fragment fetched per page match those rendered from the whole ontology, what a page's fetch, parse and render cost on
Uberon and NCBITaxon, and how the lookup of axioms rooted at blank nodes was chosen. The decisions are in
[DESIGN.md §2.8](../../docs/DESIGN.md#28-entity-pages); this records how they were measured.

## Setup

- Apple M3 Pro (12 cores, 36 GB RAM), on the Mac, not in containers. Measured 2026-11-19.
- Jena Fuseki 6.2.0 (Java 21) serving S-4's TDB2 stores as `/ds`: Uberon (1.17 M triples; 4 GB heap) and NCBITaxon
  (24 M triples; 8 GB heap). No imports, so no named graphs.
- `bench.py` runs `browl.axioms.fetch_fragment` and `browl.axioms.entity_axioms` for each entity of a sample, through
  browl's own Fuseki client and Java thread: once to warm up, then again, timed. The sample is 300 random classes and
  some properties, plus the heavily used entities named with `--iri`. With `--ontology`, the OWLAPI also loads the
  whole ontology from the file the store was loaded from, the sample is drawn from it (300 random classes, then every
  object and data property, up to 300), and each entity's axioms are compared with those rendered from it.

```sh
poetry run python research/P-15/bench.py http://localhost:3030 ds --ontology research/S-1/data/downloads/uberon.owl \
  --iri http://purl.obolibrary.org/obo/GO_0065007
poetry run python research/P-15/bench.py http://localhost:3031 ds --heap 12g \
  --ontology research/S-3/data/ncbitaxon.owl --iri http://purl.obolibrary.org/obo/NCBITaxon_500585
```

## Results

### Fragments match the whole ontology

- **Uberon:** 598 of 598 entities identical: 300 random classes, its 292 object and data properties, and six heavily
  used entities (`part_of`, GO_0065007, NCBITaxon_7742, UBERON_0000061, NCBITaxon_9606, RO_0002202).
- **NCBITaxon:** 305 of 305 identical: 300 random classes and the five classes with the most direct subclasses, or the
  root. Loading the whole ontology into the OWLAPI took 137 s.
- **The test ontology** (`tests/data/axioms.ofn`, every kind of OWL 2 axiom): every entity identical, with or without
  the dataset clause for imports (`tests/test_axioms.py`). Without the lookup of blank-node roots, 19 of its 56
  entities differ.

### Time per page

Milliseconds, after a warm-up pass. "Store sample" runs draw their sample from Fuseki, without loading the whole
ontology; "whole" runs load it for the comparison above, in the same JVM.

| Run | Entities | Fetch p50 / p95 | Parse and render p50 / p95 | Total p50 / p95 | Slowest |
| --- | --- | --- | --- | --- | --- |
| Uberon, store sample | 454 | 3.1 / 4.0 | 1.0 / 1.7 | 4.2 / 5.7 | 90.8: GO_0065007 |
| Uberon, whole | 598 | 3.4 / 5.0 | 1.1 / 1.7 | 4.6 / 6.6 | 117.4: GO_0065007 |
| Uberon, store sample, `UNION_FROM` | 454 | 3.5 / 4.7 | 1.2 / 1.8 | 4.7 / 6.5 | 169.8: GO_0065007 |
| NCBITaxon, store sample | 333 | 3.2 / 4.0 | 1.1 / 1.5 | 4.3 / 5.6 | 56.8: NCBITaxon_500585 |
| NCBITaxon, whole | 305 | 3.9 / 5.9 | 1.2 / 1.6 | 5.2 / 7.1 | 43.7: NCBITaxon_500585 |

- **Most pages take 4–7 ms**, about what S-4 measured for its three queries alone (7–10 ms with its rendering).
- **The lookup of blank-node roots adds about 0.3 ms at p50.** Without it (the `DESCRIBE` of the entity alone), the
  store-sample runs fetched in 2.8 / 4.5 ms (Uberon) and 3.0 / 4.2 ms (NCBITaxon) at p50 / p95.
- **Heavily used classes cost tens of milliseconds**, all in the lookup, which walks back through every class
  expression a class is in, and over its subclasses:
  - GO_0065007 (biological regulation) is in 2,582 class expressions in Uberon, the genus of as many
    definitions: 91–117 ms, or 170 ms with `UNION_FROM` (3 ms without the lookup). The NCBITaxon classes that taxon
    constraints use come next: NCBITaxon_7742 (vertebrates) 42–64 ms, NCBITaxon_9606 (human) 46–75 ms, with 22 and 93
    axioms each.
  - NCBITaxon_500585 has 47,783 direct subclasses, the most in NCBITaxon: 44–57 ms (4 ms without the lookup). The
    next two (43.7 K and 36.2 K subclasses) take 31–36 ms.
- **`part_of` (BFO_0000050) takes 5–6 ms**, though it's in 21,142 restrictions in Uberon: the lookup never steps back
  over `owl:onProperty` (below).
- **The dataset clause** for imports (`UNION_FROM`) cost nothing measurable for most pages, but about half again for
  the slowest. Pages add it only when the ontology has imports.

### Finding axioms rooted at blank nodes

General class axioms, n-ary disjointness and difference, and negative property assertions are rooted at a blank node,
which reaches the entity only through class expressions or lists. Three ways of finding them were measured on Uberon:

- **Backward from the entity, over any predicate a class expression uses:** 2.3 s for `part_of`, which is in 21 K
  restrictions, and each one's parents. This is the pathological case expected.
- **Forward from every candidate root:** the blank nodes that are the subject of `rdfs:subClassOf`,
  `owl:equivalentClass` or `owl:disjointWith`, or typed `owl:AllDisjointClasses` and the like, and are no other
  node's object. Finding them takes a pass over every such triple (79 K `rdfs:subClassOf` triples in Uberon, 3.0 M in
  NCBITaxon), and walking forward from Uberon's 1,435 roots to see which reach the entity took 0.3–0.45 s per page,
  for every page. With the entity bound as the path's end, ARQ walked backward from it for each root instead: 14 s
  per page. Not usable per page; it would need the roots listed at load time.
- **Backward from the entity, but never over `owl:onProperty` (chosen):** general class axioms that mention a
  property are its usage (P-27), not its own axioms, so the walk needn't step back from a property into the
  restrictions on it. A property reaches its own roots (`owl:AllDisjointProperties`) only through `rdf:first`. The cost
  is then in proportion to the class expressions a class is in, and its subclasses: the timings above.

If heavily used classes in some ontology are much slower, the roots that reach each IRI can be listed at load time,
in one bulk query, and kept in SQLite. TDB2's blank-node labels are stable for a store: `afn:bnode(?root)` returns a
root's label, and a later query can name it as `<_:label>` (checked on the Uberon store).

### Other findings

- **The renderer breaks lines** in long expressions (`some` followed by a line break before a parenthesized filler),
  whatever its wrapping setting, so `browl.manchester.one_line` joins the text into one line, outside literals.
- **`HasKey` renders without separators** between its properties (`<Person> HasKey <hasSSN><ssn>`), so
  `browl.axioms` builds its text from its operands instead: `<Person> HasKey <hasSSN>, <ssn>`.
- **An individual's class assertion with an anonymous class** (`alice rdf:type _:x`) made the types query return a
  blank node without its triples, and the OWLAPI logged "Entity not properly recognized". The types query now returns
  only named types.
