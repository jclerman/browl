# P-41: search from the start of any word — measurements

Measurements behind P-41 ([#131](https://github.com/jclerman/browl/issues/131)): what word rows cost the search index
on a large ontology, in size, build time and query time. The decisions are in
[DESIGN.md §2.6](../../docs/DESIGN.md#26-search); this records how they were measured.

## Setup

- Apple M3 Pro (12 cores, 36 GB RAM), on the Mac.
- The NCBITaxon derived database [P-19](../P-19/README.md) measured: 3.03 M name values, `rdfs:label` only, all
  direct and untagged.
- `bench.py` copies its `entity` and `name_value` tables, with their indexes, into a scratch file, builds the search
  tables there with `browl.search`'s own code, in one transaction as pre-caching does, and times
  `browl.search.search` 20 times per query, read-only.

```sh
poetry run python research/P-41/bench.py DERIVED_DB SCRATCH_DB
```

## Word starts

A word starts at an alphanumeric character (`str.isalnum`) after one that isn't. The 3.03 M values have 7.46 M later
word starts, about 2.5 each. Finding them all took 3.1 s with a Python loop (`browl.search.word_starts`) and 2.7 s with
the equivalent regular expression, `(?<=[\W_])[^\W_]` (Python's `\w` is `str.isalnum()` and `_`); the two agreed on
every value. The loop is kept, as it reads as the rule.

## The index

| | P-19 | P-41 |
| --- | --- | --- |
| Rows | 3.03 M | 10.48 M (3.03 M whole, 7.46 M word) |
| `search_value` | 247 MiB | 828 MiB (3.4×) |
| Build | 12.1 s | 46.3 s (3.8×) |
| Lanes | 2 | 4 |

The design guessed 2–3× for both. The build grows with the rows, as most of it is sorting them into the key's order
and writing the table.

A first build called Python functions from SQL for each row (the word starts through `json_each`, and the key's
length and cuts): 55.7 s. Of that, 8.6 s was finding the word starts and another 9.3 s was cutting the keys. Cutting
every key in one Python pass, into a temporary table that the sorted insert reads, saves about 10 s.

## Queries

Ten results per query; the maximum is usually the first, cold run.

| Query | Median | Max | First hits |
| --- | --- | --- | --- |
| `a` | 0.09 ms | 23.5 ms | Aa, Age, Aha |
| `s` | 0.11 ms | 19.8 ms | Sar, Sea, Sia |
| `ba` | 0.10 ms | 39.4 ms | Bau, Babr, Baia |
| `homo sapiens` | 0.94 ms | 6.2 ms | Homo sapiens, Homo sapiens neanderthalensis, … |
| `sapiens` (word matches only) | 1.22 ms | 3.5 ms | Amegilla sapiens, Coxiella endosymbiont of Homo sapiens, … |
| `bacterium` | 0.10 ms | 2.1 ms | bacterium, bacterium 8, bacterium 9 |
| `zzz` (word matches only) | 1.17 ms | 2.1 ms | Fusarium sp. zzz101, … |
| 40 × `x` (no match) | 1.39 ms | 1.6 ms | |
| `a`, annotation properties only | 0.22 ms | 1.8 ms | acronym, anamorph, genbank acronym |

The worst case seeks every lane at every length: here a query with no match, at 1.4 ms, against 0.65 ms without word
rows. As the design expected, it at most doubles, since the lanes double. N-2 (p95 under 100 ms) holds.

## FTS5, compared

Would SQLite's full-text index, FTS5, do better? `fts5_bench.py` builds a contentless FTS5 table of the same
normalized values (`unicode61` tokenizer, `detail=full` for phrase queries, prefix indexes of 1–3 characters) in the
scratch file `bench.py` filled, and times queries ranked as §2.6 ranks them, in SQL, ten IRIs a query:

```sh
poetry run python research/P-41/fts5_bench.py SCRATCH_DB
```

It builds in 8.8 s, plus 1.6 s to optimize, into 166 MiB: about a fifth of the word rows' time and size. But FTS5
returns matches in row order, not in the ranking's, so a query sorts every match, as P-19's first range scan did.
Medians of five runs:

| Query | Values matched | Time |
| --- | --- | --- |
| `a` | 770,374 | 972 ms |
| `s` | 1,702,159 | 2,077 ms |
| `ba` | 185,942 | 251 ms |
| `virus` | 169,176 | 237 ms |
| `bacterium` | 102,409 | 143 ms |
| `unclassified` | 66,199 | 88 ms |
| `hom` | 2,751 | 4 ms |
| `sapiens` | 19 | 0.1 ms |
| `sp.` | 1,390,868 | 1,735 ms |

N-2 (p95 under 100 ms) fails for any common word. Keeping the whole-value table and asking FTS5 only for word matches,
when the whole values don't fill the page, doesn't save it: no value starts with `sp.`, but 1.39 M have a later word
that starts with `sp`, so that query still sorts them all, in 1.7 s. A type or property filter that leaves few whole
values matching would do the same. So word rows stay: about 37 s more than FTS5 to build, for every query in about a millisecond.

A dedicated engine (Elasticsearch, Typesense, Postgres full text) has the same choice to make: either rank by relevance
rather than by §2.6's rules, or index every word start's suffix (or every prefix of every word) much as word rows do,
which isn't smaller or faster to build. Its gains are elsewhere, such as typo tolerance (U-20).

## After the review

PR #138's review found that word rows stored text quadratic in a value's length: each carried the value's whole tail
past 256 characters and its own key to the end. A 40,000-character label of one-letter words took 1.23 GB and 10.4 s
to index. Now only words within a value's first 1,000 normalized characters (`MAX_QUERY_CHARS`) get rows, each keyed
by at most 1,000 characters and holding the value's first 256; a hit reads the rest of a longer value from
`name_value`. A value's word rows hold at most about 630,000 characters. The key also gained `word_start`, so a long,
repetitive value's word rows, alike in their first 256 characters, no longer merge.

Rerun on NCBITaxon, where no word starts past 1,000 characters: 48.4 s and 828 MiB, as before; queries as before.

Dropping the value from word rows altogether, so they hold only their key, made `search_value` 577 MiB (30% smaller)
but the build no faster (45.6 s). It also lost rule 7's alphabetical order among word matches with the same key:
"sapiens" listed "Macrobiotus sapiens" before "Amegilla sapiens". So word rows keep the value's first 256 characters.
