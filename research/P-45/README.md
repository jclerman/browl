# P-45: the results page's capped count — measurements

What counting search results up to the results page's cap (`COUNT_TO`, 1,051 IRIs) costs on a large ontology, for
P-45 ([#139](https://github.com/jclerman/browl/issues/139)). The decisions are in
[DESIGN.md §2.6](../../docs/DESIGN.md#26-search) and §2.9; this records how they were measured.

## Setup

- Apple M3 Pro (12 cores, 36 GB RAM), on the Mac.
- NCBITaxon's 3.03 M `rdfs:label` values, from [P-41](../P-41/README.md)'s scratch database (pre-P-42 schema), copied
  into today's schema as `annotation_value` and indexed with `browl.search.fill_index` (54.7 s). Label only, as in
  P-41: NCBITaxon's other name-like values are mostly common values (P-42), which aren't indexed by entity.
- `bench.py` times `browl.search.search` 20 times per query: as the autocomplete runs it (10 results, no count), and
  as the results page does (50 results, counted to `COUNT_TO`), on the first page and on the last page it reaches.

```sh
poetry run python research/P-45/bench.py OLD_DB SCRATCH_DB
```

## Queries

| Query | Count | Autocomplete p50 / p95 | Page 1 p50 / p95 / max | Last page p50 / p95 |
| --- | --- | --- | --- | --- |
| `a` | more than 1,050 | 0.11 / 74.69 ms | 2.03 / 34.40 / 36.08 ms | 1.93 / 11.99 ms |
| `s` | more than 1,050 | 0.12 / 2.44 ms | 2.15 / 12.95 / 13.50 ms | 2.08 / 11.52 ms |
| `e` | more than 1,050 | 0.13 / 2.02 ms | 2.14 / 13.11 / 13.20 ms | 2.05 / 11.29 ms |
| `ba` | more than 1,050 | 0.14 / 1.41 ms | 2.05 / 10.84 / 11.28 ms | 2.10 / 10.15 ms |
| `hom` | more than 1,050 | 0.14 / 1.43 ms | 2.28 / 11.50 / 11.98 ms | 2.24 / 5.96 ms |
| `homo sapiens` | 12 | 0.97 / 3.43 ms | 1.83 / 2.35 / 2.37 ms | 1.82 / 1.91 ms |
| `sapiens` (word matches only) | 19 | 1.28 / 3.12 ms | 2.09 / 3.11 / 3.13 ms | 2.01 / 2.27 ms |
| `candidatus` | more than 1,050 | 0.17 / 1.69 ms | 2.11 / 10.11 / 10.50 ms | 2.13 / 8.42 ms |
| `bacterium` | more than 1,050 | 0.14 / 0.89 ms | 1.85 / 6.72 / 6.96 ms | 1.94 / 4.17 ms |
| `virus` | more than 1,050 | 0.15 / 1.00 ms | 2.96 / 8.42 / 8.68 ms | 2.84 / 8.14 ms |
| `zzz` (word matches only) | 39 | 1.16 / 1.92 ms | 2.18 / 2.63 / 2.63 ms | 2.16 / 2.24 ms |
| 40 × `x` (no match) | 0 | 1.37 / 1.47 ms | 1.43 / 1.87 / 1.87 ms | 1.40 / 1.50 ms |

The p95s and maxima are the first, cold runs (the autocomplete's `a` ran first of all). Warm, counting to the cap
costs about 2 ms a query, whether the query matches many or few: one that matches few reads every lane anyway, to tell
that there are no more. Reading 1,051 IRIs costs about what reading the last page's 1,051 does, so later pages cost no
more than the first. N-2 (p95 under 100 ms) holds, for the autocomplete, which counts none, and for the results page.
