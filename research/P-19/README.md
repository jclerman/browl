# P-19: search index — measurements

Measurements behind P-19 ([#26](https://github.com/jclerman/browl/issues/26)): how fast name search is on a large
ontology, first with the range scan the design first planned, then with the search table built instead. The decisions
are in [DESIGN.md §2.6](../../docs/DESIGN.md#26-search); this records how they were measured.

## Setup

- Apple M3 Pro (12 cores, 36 GB RAM), on the Mac.
- A derived database of NCBITaxon (3.03 M classes, 32 annotation properties) built with the default settings:
  `NAME_PROPERTIES` `rdfs:label` only, so 3.03 M name values, all direct and untagged, in one lane per type.
- `bench.py` copies its `entity` and `name_value` tables, with their indexes, into a scratch file, builds the search
  tables there with `browl.search`'s own code, and times `browl.search.search` 20 times per query, read-only.

```sh
poetry run python research/P-19/bench.py DERIVED_DB SCRATCH_DB
```

## The range scan first planned

A range scan over an index on `name_value.normalized`, ranking each entity's values and the entities in SQL
(`row_number()` per entity, then `ORDER BY exact, length, value LIMIT 11`). Best of 5:

| Query | Values matched | Time |
| --- | --- | --- |
| `a` | 251,933 | 1,023 ms |
| `s` | 264,302 | 1,059 ms |
| `e` | 148,537 | 589 ms |
| `ba` | 99,512 | 385 ms |
| `hom` | 1,842 | 4 ms |
| `homo sapiens` | few | under 1 ms |

Every match is sorted, as shorter values rank first and no index is in that order: too slow for N-2 (p95 under
100 ms) on short queries, which are what autocomplete sends.

## The search table

Building `search_value` and `search_lane` took 11.1–12.6 s (two runs); `search_value` is 240 MiB. The index on
`name_value.normalized` it replaces was 100 MiB.

Ten results per query:

| Query | Median | Max |
| --- | --- | --- |
| `a` | 0.11 ms | 9.14 ms |
| `s` | 0.12 ms | 2.46 ms |
| `e` | 0.12 ms | 2.00 ms |
| `ba` | 0.12 ms | 1.87 ms |
| `hom` | 0.13 ms | 1.85 ms |
| `homo sapiens` | 0.88 ms | 2.76 ms |
| `unclassified` | 0.14 ms | 1.01 ms |
| `candidatus` | 0.15 ms | 2.38 ms |
| `zzz` (no match) | 0.93 ms | 1.29 ms |
| 40 × `x` (no match) | 0.65 ms | 0.68 ms |
| `a`, annotation properties only | 0.13 ms | 0.44 ms |
| `zzz`, annotation properties only | 0.10 ms | 0.11 ms |

The maximum is usually the first, cold run. A query with no match seeks each lane at every length up to its longest
(153 here), about 5 µs a seek. With the type filter as a residual filter rather than part of the key, in an earlier
prototype, `a` among annotation properties took 1.8 s, as it scanned the classes' matches.

This ontology has few lanes. One with several name properties, languages and types has more, each sought at every
length for a query with few matches: 50 lanes of 150 lengths would be about 40 ms, still inside N-2. P-24 checks the
target on its ontologies.
