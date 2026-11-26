# P-49: axiom annotations — measurements

Measurements behind P-49 ([#163](https://github.com/jclerman/browl/issues/163)): what showing axiom annotations costs
an entity's Overview, against the caps and N-3. The decisions are in
[DESIGN.md §2.8](../../docs/DESIGN.md#28-entity-pages) (Axiom annotations); this records how they were measured.

## Setup

- Apple M3 Pro, on the Mac, not in containers. Measured 2026-11-25.
- Jena Fuseki 6.2.0 (Java 21, 4 GB heap) serving S-4's TDB2 stores as `/ds` (`research/S-4/fuseki.sh`): Uberon and
  HPO. No imports, so no dataset clause.
- `bench.py` reads each entity's Overview data as the page does: its annotations with their axiom annotations, and its
  axioms from a fragment with theirs, parsed and converted by the OWLAPI. Each entity is read once to warm up, then
  timed; then all again without axiom annotations, as before P-49. The entities are 300 random classes and the 20 that
  the most reified axioms annotate. Results: `data/uberon.json`, `data/hpo.json`.

```sh
poetry run python research/P-49/bench.py http://localhost:3030 ds --top 20
```

## Results

Milliseconds per Overview, fetch to converted axioms:

| Store | Entities | With annotations p50 / p95 / max | Without p50 / p95 / max |
| --- | --- | --- | --- |
| Uberon | 300 random classes | 6.9 / 9.5 / 28.9 | 5.5 / 7.1 / 20.5 |
| Uberon | 20 most annotated | 14.1 / 40.5 / 40.5 | 7.6 / 20.8 / 20.8 |
| HPO | 300 random classes | 7.5 / 10.5 / 25.2 | 5.6 / 7.4 / 44.5 |
| HPO | 20 most annotated | 11.3 / 25.1 / 25.1 | 6.4 / 9.5 / 9.5 |

- **N-3 holds:** the slowest Overview with annotations took 41 ms, against the 300 ms target. The second query runs
  alongside the first, so most of the cost is the larger fragment's parse.
- **Far within the caps:** the most annotated entity in either store, GO_1904746 (imported into both), carries 147 axiom
  annotations on its values (`MAX_AXIOM_ANNOTATIONS` is 2,000), 74 K characters of them (`MAX_ANNOTATIONS_CHARS` is
  4 Mi); its fragment is 139 KB (`CONSTRUCT_MAX_BYTES` is 64 MiB). The most IRIs a fragment mentioned was 63, in
  UBERON_0001132 (`MAX_FRAGMENT_IRIS` is 5,000). No entity's annotations were left out.
- **The fragment grows 3.3–4.4 times for the most annotated entities,** as it holds the reified axioms of the entity's
  annotation assertions too (synonyms' and definitions' cross-references), which the page leaves out with the
  assertions. Filtering them out in the query would repeat `entity-annotations.rq`'s test for an annotation property;
  at these sizes, it isn't worth it.
- **RadLex** (the app's store, not timed): 2,932 reified axioms, at most 8 on one entity, nearly all on its
  `ExternalRefID` values, none nested.
