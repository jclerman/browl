# P-12: hierarchies pre-caching — measurements

Measurements behind P-12 ([#24](https://github.com/jclerman/browl/issues/24)): the cost of the hierarchies hook on a
large ontology and on two mid-sized ones with multiple parents. The decisions are in
[DESIGN.md §2.5](../../docs/DESIGN.md#25-derived-data-pre-caching); this records how they were measured.

## Setup

- Apple M3 Pro (12 cores, 36 GB RAM), on the Mac, not in containers. Measured 2026-11-20.
- Jena Fuseki 6.2.0 (8 GB heap, Java 21) serving TDB2 stores as `/gen-1`, S-4's but for HPO's:
  - **NCBITaxon** (24 M triples, 3.03 M classes, a pure tree);
  - **Uberon** (1.17 M triples, 27 K classes, 35.6 K subclass and 391 sub-property edges between IRIs);
  - **HPO**, release 2026-09-01 (0.91 M triples, 32.8 K classes, 42.5 K subclass and 292 sub-property edges between
    IRIs; 8.4 K classes have more than one parent). Its store was loaded for P-12 from `hp.owl` (OBO PURL) with
    `tdb2.tdbloader --loc data/tdb-hpo` in `research/S-4`, in 4.2 s.
- `bench.py` runs the entities hook, which the hierarchies hook needs, then the hierarchies hook
  (`browl.precache.pre_cache`), timing each and the hierarchies hook's steps.

```sh
poetry run python research/P-12/bench.py http://localhost:3031
```

## Results

Seconds. NCBITaxon: runs 2 and 3, after a first run that warmed Fuseki (its hook took 72 s). Uberon and HPO: the
second and third runs.

| | entities | hierarchies | of which: staging batches | `_fill`, all hierarchies | `group_nodes` | `count_descendants` |
| --- | --- | --- | --- | --- | --- | --- |
| NCBITaxon | 24.9–26.0 | 56.7–57.8 | 2.2 | 32.5–33.2 | 7.1 | 5.0–5.2 |
| Uberon | 0.2 | 0.5 | 0.0 | 0.2 | 0.0 | 0.0 |
| HPO | 0.3 | 0.5 | 0.0 | 0.2 | 0.0 | 0.0 |

- **NCBITaxon's hook takes about 57 s.**
  - **The query:** run alone with `curl`, the edges query streams its 3.02 M rows (456 MB of TSV) in 20.1–20.7 s. That's
    1–2 s more than S-4's subclass-only query, for the `?relation` column and the two other relations.
  - **`_fill`, 33 s:** the class hierarchy takes nearly all of it. Grouping takes 7.1 s (one Tarjan pass over 3 M
    edges, as there are no cycles), and counting 5.1 s (S-3 measured 4.2 s). The remaining ~21 s is the SQL join from
    staged IRIs to `entity.id`, numbering the entities, and sorting and inserting 3 M rows into each of `edge` and
    `counts`.
  - **The rest, about 2 s:** creating the indexes.
- **Tables:**
  - **Rows:** NCBITaxon gives 3,024,993 in `edge` and 3,025,062 in `counts`; HPO 42,796 and 33,133. `equiv_group`
    is empty for all three: none has an `owl:equivalentClass` between two IRIs, nor a cycle.
  - **Size:** on NCBITaxon, `edge` and its index take 111 MiB, and `counts` 53 MiB; on HPO, about 1 MiB each.
- **Memory:** the bench process peaked at 2.1–2.3 GB resident on NCBITaxon, 0.1 GB on Uberon and HPO (Python and
  SQLite; Fuseki is a separate process).
- **Mid-sized ontologies cost nothing to speak of:** on Uberon and HPO the whole hook takes half a second, most of it
  the query.
- **Possible savings, for P-24:**
  - Number the entities in SQL rather than in Python dicts.
  - Skip Tarjan when a topological pass covers every node.
  - Stage the relation as a small code rather than its IRI.
  - The [4-lens review of PR #94](https://github.com/jclerman/browl/pull/94) lists these.
