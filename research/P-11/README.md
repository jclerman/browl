# P-11: names pre-caching — measurements

Measurements behind P-11 ([#23](https://github.com/jclerman/browl/issues/23)): the cost of the names hook on a large
ontology, and whether reading the next batch of a bulk result while the last one is inserted pays. The decisions are in
[DESIGN.md §2.5](../../docs/DESIGN.md#25-derived-data-pre-caching); this records how they were measured.

## Setup

- Apple M3 Pro (12 cores, 36 GB RAM), on the Mac, not in containers.
- Jena Fuseki 6.2.0 (8 GB heap, Java 21) serving S-4's TDB2 store of NCBITaxon (24 M triples, 3.03 M classes) as
  `/gen-1`.
- `bench.py` runs the entities and names hooks (`browl.precache.pre_cache`) against it and times each, and each of the
  names hook's database steps. The name properties are `rdfs:label` and the four oboInOwl synonym properties, as in
  S-4, whose dictionary query returned 3.56 M rows for them.

```sh
poetry run python research/P-11/bench.py http://localhost:3031 [--overlap] [--upload ncbitaxon.owl]
```

## Results

Seconds, one run each after a first run that warmed Fuseki (the first run's names hook took 112 s).

| | entities | names | of which: CURIEs | staging batches | name values (join, deduplicate) | display names | total |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Sequential (as built) | 25.1 | 84.7 | 8.8 | 8.1 | 17.6 | 8.0 | 110.3 |
| Overlapped | 24.9 | 87.1 | 8.5 | 29.2 | 18.7 | 8.0 | 112.6 |

The derived file is 1.4 GB.

- **Overlapping doesn't pay.** With a batch's insert running on the database's thread while the next batch is read
  (`--overlap`, at most one insert in flight), neither hook got faster. Staging took 29 s instead of 8 s: the batch's
  Python work (normalizing values, building rows) competes for the GIL with the event loop parsing the next batch, so
  the two threads take turns rather than run together. The hooks keep reading and inserting in turn.
- **The query dominates.** Of the names hook's 85 s, about 50 s is the query streaming and its batches being staged;
  S-4 timed the query alone at 41–48 s. Next come mapping IRIs to `entity.id` and merging duplicate values (18 s, one
  `INSERT … SELECT … GROUP BY` sorting 3.56 M rows), the CURIEs (9 s, one `UPDATE` per prefix over its namespace's
  range of the IRI index; here nearly every IRI is under `obo:`) and the display names (8 s).
- **Possible savings, for P-24:** setting each entity's CURIE and display name in one `UPDATE` rather than two would
  rewrite each `entity` row once; that might save a few seconds. Reading the prefixes from the 2 GB RDF/XML file's start
  takes under 0.01 s.
