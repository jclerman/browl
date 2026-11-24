# P-42: every annotation property searchable — measurements

Measurements behind P-42 ([#132](https://github.com/jclerman/browl/issues/132)): what indexing every annotation
property's literal values costs search, in lanes, size, build time and query time, on HPO, Uberon and NCBITaxon; two
ways to keep queries fast across many lanes; and whether long, prose values (definitions, comments) are better served
by a full-text index. The decisions are in [DESIGN.md §2.6](../../docs/DESIGN.md#26-search); this records how they
were measured.

## Setup

- Apple M3 Pro (12 cores, 36 GB RAM). Values fetched in the dev stack's app container, from its Fuseki; indexes
  built and queried on the Mac, as in [P-19](../P-19/README.md) and [P-41](../P-41/README.md).
- The stores in the dev stack's Fuseki: HPO (0.91 M triples), Uberon (1.17 M) and NCBITaxon (24.0 M).
- `bench.py fetch` reads the entities as the entities hook does (`entity-list.rq`, and the ontology), and every
  literal value of each annotation property on an IRI: the declared ones and OWL's built-in ones, as NCBITaxon
  doesn't declare `rdfs:label`. Each value is stored once, under the property it's asserted with.
- `bench.py build` builds the index with `browl.search.fill_index` over `rdfs:label` alone, as now, and over every
  property, one property rank per property: so one lane per (type, property, language rank, word), as P-42 keys
  them. It times `browl.search.search` 20 times per query.
- `variants.py` prototypes two ways to make queries over many lanes fast (below), timed 10 times per query against
  `search` as built, and checks that they return the same hits.
- `prose.py` classifies the properties as prose or not, and builds an FTS5 index of the prose.

```sh
poetry run python research/P-42/bench.py fetch DATASET VALUES_DB  # in the app container
poetry run python research/P-42/bench.py build[:names|:all] VALUES_DB SCRATCH_DB
poetry run python research/P-42/variants.py SCRATCH_DB SMALL_ROWS TOP_K [A|B|AB]
poetry run python research/P-42/prose.py VALUES_DB SCRATCH_DB
```

## The values

| | HPO | Uberon | NCBITaxon |
| --- | --- | --- | --- |
| Properties with literal values | 27 | 66 | 14 |
| Values, `rdfs:label` | 33 K | 27 K | 3.03 M |
| Values, every property | 202 K | 218 K | 9.77 M |
| Fetch query and staging | 2.3 s | 1.9 s | 118 s |

- **NCBITaxon's are mostly noise** for search: `hasDbXref` (3.08 M, e.g. `GC_ID:1`) and `hasOBONamespace` (3.03 M,
  all `ncbi_taxonomy`). The names hook's own query of the labels takes about 50 s ([P-11](../P-11/README.md)).
- **Not only strings:** `xsd:dateTime` (`dcterms:date`, 13.6 K in HPO), `xsd:boolean` (`owl:deprecated`) and
  `xsd:anyURI` (`foaf:depiction`, `rdfs:seeAlso`) values too.
- **The ontology's own annotations** (title, description, license, …) are values of one entity, but each property
  makes lanes of its own: 16 of HPO's lanes, 29 of Uberon's.

## Every property in the lane index

| | HPO | Uberon | NCBITaxon |
| --- | --- | --- | --- |
| Lanes (whole + word) | 10 → 92 | 12 → 213 | 4 → 36 |
| `search_value` | 11 → 394 MiB | 8 → 436 MiB | 828 → 1,680 MiB |
| Build | 0.5 → 8.2 s | 0.4 → 8.2 s | 49 → 127 s |
| Query over `rdfs:label`, worst median | 3 → 3.5 ms | 3.8 → 4.5 ms | 2 → 2 ms |
| Query over every property, worst median | 72 ms | 286 ms | 13 ms |
| Query over every property, max | 148 ms | 806 ms | 23 ms |

- **Lanes are fewer than feared**, 36–213 rather than 600, but a query that matches few seeks each at every length:
  Uberon's 213 lanes sum to about 26 K seeks. So a search over every property fails N-2 (p95 under 100 ms) on Uberon
  and nearly on HPO, while one over `rdfs:label` stays as fast as now, whatever else is indexed.
- **Most lanes are small:** 117 of Uberon's 213 hold 100 rows or fewer, and 63 hold 10 or fewer.
- **Prose dominates the size.** A word row holds up to 256 characters of its value and a key of up to 1,000, so a
  long value with many words makes many long rows: Uberon's definitions (`IAO_0000115`, 22 K values) take 171 MiB of
  the 436.

## Two ways to query many lanes

On Uberon's index of every property, 22 queries, with 10 and 100 results, over every property and over the name
properties.

- **A, small lanes read whole:** the index as built, but a lane of at most 1,000 rows is read with one query, its
  matches bucketed by length in Python, rather than sought at each length.
- **B, keyed by value, with each busy prefix's best values:** the index keyed by (lane, normalized, value, entity,
  word start), so one seek finds a lane's matches of a prefix, in value order; and `search_top`, which holds, for
  each lane and each prefix that more than K of its keys start with, their best K in rank order. A prefix whose keys
  all continue with the same character isn't stored, as in a compressed trie, since the longer prefix has the same
  matches; a query reads the first stored prefix that starts with its text. Without that, prefixes ran 256
  characters deep through repeated wording in definitions, each storing the same rows again: 700 MiB to 1.5 GiB.

Both returned the same hits as `search` for every query.

| Uberon, K = 128 | As built | A | B |
| --- | --- | --- | --- |
| p95 | 271 ms | 129 ms | 4.4 ms |
| Max | 574 ms | 235 ms | 10 ms |
| Matching few (e.g. `sapiens`), every property | ~260 ms | ~120 ms | ~3.5 ms |
| `a`, every property | 2 ms | 10 ms | 1.2 ms |
| Extra space, build | — | — | `search_top` 266 MiB, 10 s |

A halves the worst case, as Uberon's 48 lanes of more than 1,000 rows still take about 120 ms, and reading the small
lanes costs 8 ms more on every query: it fails N-2. B is fast, but its cost was then measured on NCBITaxon:

| NCBITaxon, K = 128 | `rdfs:label` | Every property |
| --- | --- | --- |
| As built: p95, max | 4.4 ms, 28 ms | 17.9 ms, 35 ms |
| B: p95, max | 0.9 ms, 1.4 ms | 1.1 ms, 2.9 ms |
| `search_value` | 828 MiB | 1,680 MiB |
| `search_top` | 690 MiB | 794 MiB |
| `search_top` build | 50 s | 94 s, 2.5 GB peak memory |

- **K doesn't shrink it:** with K = 32, `search_top` was 659 MiB (NCBITaxon) and 280 MiB (Uberon), as more
  prefixes are busy.
- **Pages past K are slow:** a query that needs more than a busy prefix's K values reads and sorts all its matches.
  With K = 32 and 100 results, `a` took 270 ms and `sp.` 1.3 s on NCBITaxon, against about 1 ms as built. Pages up
  to `MAX_OFFSET` would need K over 1,100.

So neither is adopted: as built, the index is fast whenever a prefix has many matches, deep pages included; B only
when it has few.

## Names and prose

The properties split by their values' word counts. Names, synonyms, IDs, cross-references and dates have a median of
at most 5 words a value, and a 90th percentile of at most 7; prose has a median of 8 to 46:

| Property | Ontology | Median words | 90th percentile | Longest (chars) |
| --- | --- | --- | --- | --- |
| `rdfs:label` | Uberon | 3 | 6 | 144 |
| `oboInOwl:hasExactSynonym` | Uberon | 4 | 6 | 461 |
| `oboInOwl:hasNarrowSynonym` | Uberon | 5 | 7 | 111 |
| `oboInOwl:hasDbXref` | Uberon | 1 | 1 | 203 |
| `UBPROP_0000002` | Uberon | 7 | 11 | 153 |
| `IAO_0000112` (example of usage) | Uberon | 8 | 23 | 386 |
| `IAO_0000116` (editor note) | Uberon | 10 | 33 | 2,057 |
| `rdfs:comment` | Uberon | 15 | 48 | 1,943 |
| `IAO_0000115` (definition) | Uberon | 17 | 48 | 1,682 |
| `IAO_0000115` (definition) | HPO | 16 | 41 | 2,036 |

With "prose if the median value has at least 8 words", HPO has 5 prose properties (the definition, `rdfs:comment`,
and three of the ontology's own), Uberon 18 (the definition, comment, editor, curator and usage notes, 11 `UBPROP`
notes, and two of the ontology's own), and NCBITaxon none. None is misplaced.

**FTS5 for the prose.** An FTS5 table (`unicode61` tokenizer, diacritics removed, prefix indexes of 1–3 characters)
over the prose values, with external content, so the text isn't stored twice. A query requires a prefix match of
each of its words, in any order, and is ranked by BM25; one hit per entity.

| | HPO | Uberon |
| --- | --- | --- |
| Prose values | 35 K, 5.1 MiB | 33 K, 5.1 MiB |
| Lane index, every property | 395 MiB, 7.2 s | 435 MiB, 6.9 s |
| Lane index, all but prose | 43 MiB, 2.0 s | 48 MiB, 2.4 s |
| Lane index, prose only | 351 MiB, 4.5 s | 387 MiB, 5.7 s |
| FTS5, prose only | 7.9 MiB, 0.2 s | 8.0 MiB, 0.3 s |

| Query (Uberon, unless noted) | Matches | Time |
| --- | --- | --- |
| `heart`, `lung`, `epithelium`, `bone marrow`, `neural crest` | 67–832 | 0.3–1.4 ms |
| `abnormality`, `increased` (HPO) | 1.2–1.7 K | 2–3 ms |
| `part of the` | 3.3 K | 7 ms |
| `the` | 23 K | 35 ms |
| `a` | 29 K | 39 ms |
| `zzz`, 40 × `x` | 0 | under 0.1 ms |

- **Ranking suits prose:** short, focused values come first, e.g. `abnormality` finds "A phenotypic abnormality.",
  `cardiac muscle` finds "Muscle contraction of cardiac muscle tissue.". `highlight()` marks the matched words.
- **Time grows with the matches,** as BM25 scores them all: about 1.3 µs each. A one- or two-letter word could be
  required to match a whole word rather than a prefix, for larger corpora.
- **Restricting to one property** (the definition) costs nothing more.
- **Phrases** (`"cardiac muscle"`, words adjacent and in order) use the positions FTS5 keeps by default, and are no
  slower than words: on Uberon, `"cardiac muscle"` 0.40 ms (107 matches, against 129 for the words), `"part of the"`
  3.2 ms (2,014, against 3,339 in 5.8 ms), `"of the"` 15 ms (11.7 K).

**The lane index without prose.** Rebuilt from the values of the properties that aren't prose (`bench.py build:all`
on a copy of the values without them):

| | HPO | Uberon |
| --- | --- | --- |
| Lanes (whole + word) | 70 | 127 |
| `search_value`, build | 43 MiB, 2.1 s | 48 MiB, 2.4 s |
| Query over every property, worst median | 27 ms | 60 ms |
| Query over every property, max | 30 ms | 245 ms (a first, cold run) |

So with prose out, searching every remaining property at once mostly holds N-2 here too: what's left of the lane
count is the cost.

## Conclusions

- Index every annotation property's values by the property they're asserted with, but sort the properties into
  names and prose: built-in lists, sub-properties of `rdfs:label`, then the median-words rule, which admins can
  override (P-44).
- Names (and IDs, cross-references, …) go in the lane index as now: about 4–6× its size with `rdfs:label` alone, not
  36–54×. Search covers the name properties and synonyms by default, other properties when chosen.
- Prose goes in FTS5, ranked by BM25, searched when chosen: 2% of the lane index's size for the same values.
- Searching every non-prose property at once takes up to about 60 ms (Uberon's 127 lanes), so it mostly holds N-2;
  an ontology with many more small properties could miss it. If that matters, B's prefix lookup could serve it, at
  its cost in space.
