# P-47: individuals' pages — measurements

Measurements behind P-47 ([#162](https://github.com/jclerman/browl/issues/162)): the entities and owner-map hooks'
build times and sizes before and after named individuals became entities and the owner map found anonymous
individuals, where Uberon's undeclared subjects went, and N-3 for individuals' pages. The decisions are in
[DESIGN.md §2.5](../../docs/DESIGN.md#25-derived-data-pre-caching) (the owner map),
[§2.8](../../docs/DESIGN.md#28-entity-pages) (the pages) and [§2.9](../../docs/DESIGN.md#29-page-layout) (the column);
this records how they were measured.

## Setup

- S-7's setup ([`research/S-7/README.md`](../S-7/README.md)): the Mac, Fuseki 6.2.0 serving S-4's TDB2 stores as
  `/gen-1` (`research/S-7/fuseki.sh`), one at a time. Measured 2026-11-26.
- `build.py` builds a derived database with only the entities and owner-map hooks, times each hook from the stage's
  progress reports (the owner map's time includes the build's end), and records the `entity` rows, the tables' sizes
  and the owner map's roots: `data/NAME-LABEL.json`. `before` is main's code (f7caba6), run with `PYTHONPATH` set to an
  export of it; `after` is this branch's.
- `query.py` times `owner-map.rq`, or a variant, alone, through browl's bulk SELECT client.
- `subjects.py` counts the triples whose IRI subject isn't in `entity`, before and after, as S-7 did.
- `pages.py` runs the app in a test client on a full pre-cache and usage table, and times every named individual's
  page and Usage tab: `data/NAME-pages.json`.

```sh
poetry run python research/P-47/build.py http://localhost:3031 ncbitaxon after --runs 2
PYTHONPATH=/path/to/main-export poetry run python research/P-47/build.py http://localhost:3031 ncbitaxon before
poetry run python research/P-47/query.py http://localhost:3031 browl/sparql/owner-map.rq
poetry run python research/P-47/subjects.py http://localhost:3031 uberon
poetry run python research/P-47/pages.py http://localhost:3031 uberon
```

## The hooks

Medians of 2–3 runs, in seconds:

| | entities before | entities after | owner map before | owner map after | named individuals |
| --- | --- | --- | --- | --- | --- |
| NCBITaxon | 26.4 | 26.8 | 35.1 | 36.5 | 0 |
| Uberon | 0.24 | 0.23 | 9.1 | 8.9 | 67 |
| HPO | 0.29 | 0.27 | 6.4 | 6.5 | 0 |

- **The entities hook** is unchanged where there are no individuals, as the entity-list query only adds
  `owl:NamedIndividual` to its types. Uberon's 67 individuals add 4 KiB to `entity` (2.0 MiB) and 204 KiB to the
  file (22.9 MiB). No targeted ontology has many individuals, so the scale case wasn't measured; a row costs what a
  class's does: about 86 bytes in `entity`, 177 with its indexes, by NCBITaxon's 3.03 M (511 MiB with indexes).
- **The owner map** costs the same as before. None of the three has an anonymous individual, so the roots are the same
  but for Uberon's 3 triples owned by individuals, now entities (59,703 to 59,706 owned roots; references 253,693 to
  253,799).
- **A filter-order trap:** the first version took 54–59 s on NCBITaxon, against 35 s before. The query alone took
  44.6 s against 23.0 s: Jena split the anonymous-individual branch's two filters and tested `isBlank(?b)` first,
  decoding the subject of each of NCBITaxon's 3.56 M `rdf:type` triples. That branch alone takes 1.05 s with the type
  filter only, 18.5 s with `isBlank(?b)` added as a separate filter, written before or after it, and 1.3 s with both in
  one `IF(type test, isBlank(?b), false)`, which can't be split. With the `IF`, the whole query takes 21.3–22.3 s, the
  build 36.4–36.5 s. The declared properties' branch costs nothing measurable: without the type branch, the query
  took 20.8 s.

## Uberon's undeclared subjects

S-7 found 261 triples whose IRI subject isn't in `entity`, from 70 subjects: ORCID IRIs typed as NCBITaxon_9606, and
a few OMO and IAO terms. All but 3 of the subjects are declared named individuals, so 258 of those triples are now
their own axioms or usages under them. The 3 left are SWRL variables (`urn:swrl:var#x`, `#y`, `#z`), one triple each;
HPO and NCBITaxon have none.

## N-3: individuals' pages

Uberon's 67 named individuals, 5 runs each after a warm-up, app and client in one process (so the client's time is
included), the store and usage table warm:

| | p50 | p95 | max |
| --- | --- | --- | --- |
| Page (header, Overview, hierarchy column) | 14.3 ms | 58.2 ms | 144.6 ms |
| Usage tab panel | 15.6 ms | 83.8 ms | 189.6 ms |

The slowest are IAO's curation-status individuals (IAO_0000121, IAO_0000103). N-3 holds; HPO and NCBITaxon have no
individuals to time.
