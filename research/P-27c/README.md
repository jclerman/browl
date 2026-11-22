# P-27c: the usage table — measurements

Measurements behind P-27c ([#110](https://github.com/jclerman/browl/issues/110)): the usage table's bulk build, and the
Usage tab's pages read from it. The decisions are in [DESIGN.md §2.5](../../docs/DESIGN.md#25-derived-data-pre-caching)
(the table) and [§2.8](../../docs/DESIGN.md#28-entity-pages) (the tab); this records how they were measured, and the
build's history.

## Setup

- S-7's setup ([`research/S-7/README.md`](../S-7/README.md)): the Mac, Fuseki 6.2.0 serving S-4's TDB2 stores as
  `/gen-1` (`research/S-7/fuseki.sh`), and the derived databases P-27b's bench built (`research/P-27b/data/NAME/derived`,
  linked as `data/NAME/derived`).
- `bench.py` builds the table for generation 1 and times its phases (preparing the ranks and counting, the scan, the
  writing), samples the process's memory, and records the file's size: `data/NAME-build.json`.
- `pages.py` times the tab's pages once the table is ready (N-3): `data/NAME-pages.json`.
- `streamed.py` times the streamed pages again, with a property's values read per page: `data/NAME-streamed.json`.
- `scan.py` times browl's bulk SELECT client reading every triple alone.

```sh
poetry run python research/P-27c/bench.py http://localhost:3031 ncbitaxon --runs 1
poetry run python research/P-27c/pages.py http://localhost:3031 ncbitaxon
```

## The build

| | total | of which: scan | writing share | triples in all | usages | file |
| --- | --- | --- | --- | --- | --- | --- |
| NCBITaxon | 253 s | 186 s | 0.27 | 24.0 M | 21.9 M | 528 MiB |
| Uberon | 6.9–7.7 s | 5.1–5.8 s | 0.24–0.26 | 1.17 M | 601 K | 14 MiB |
| HPO | 5.3–6.0 s | 3.9–4.7 s | 0.23–0.26 | 0.91 M | 467 K | 11 MiB |

NCBITaxon's steps:

- **Ranking** every entity IRI in name order: 6 s. The count of the triples the scan reads runs meanwhile: 3 s for
  every triple, 4.3 s with the scan's filter (as now), and 2.3 s for every triple less the left-out ones counted from
  the `rdf:type` index; the filtered count is the simplest, and hidden behind the ranking.
- **The scan:** 186 s, Python-bound at 9 µs per triple; parsing the TSV takes half, and Fuseki sends it in 86 s. It
  leaves out the `rdf:type` triples whose object is OWL's vocabulary and no entity: 3.56 M of 24.03 M (15%), so it
  reads 20.47 M.
- **The positions:** 68 s, the window function sorting 21.9 M rows.
- **Memory:** the process peaked at 1.4 GB, growing by about 1.3 GB: mostly the ranks (an IRI → rank dict of 3 M
  entries) and SQLite's caches (256 MiB for the table, whose sort spills to temporary files beyond it, and 64 MiB for
  the scratch file). Building the ranks and ids from one sorted list of tuples, rather than sorting the IRIs by key,
  measured the same peak on 3 M synthetic IRIs (656 vs 667 MiB), so it wasn't changed.

## History

- A first version took 302 s, with the scan's rows handled in plain Python and the ranks sorted in SQL. The scan's own
  code (`_Scan.add`), leaving out the declarations, and sorting the ranks in Python brought it to 253 s.
- The scan's estimate first counted every triple, so it overstated the scan's length by 15% on NCBITaxon and dropped
  when the scan ended; the count now applies the scan's filter. Run again with it, NCBITaxon's build counted
  20,465,221 triples, as many as the scan read, and took 259 s (preparing and counting 9.6 s, the scan 191 s), the
  same usages and file.
- The positions' step once ended by deleting the staged rows, which only moved the scratch file's pages to its
  freelist; the file is deleted after the build, so that was dropped.
