# P-3: Fuseki's memory and store layout — measurements

Measurements behind P-3 ([#11](https://github.com/jclerman/browl/issues/11)): how much heap Fuseki needs to serve
NCBITaxon, and what keeping the ontology in a named graph costs. The decisions are in
[DESIGN.md §2.4](../../docs/DESIGN.md#24-load-pipeline) and [§2.14](../../docs/DESIGN.md#214-risks); this records how
they were measured.

## Setup

- Docker Desktop 4.99.0 (Docker Engine 29.9.3) on an Apple M3 Pro (12 cores, 36 GB RAM); the Docker VM had 12 CPUs
  and 7.65 GB.
- The P-3 images: Fuseki 6.2.0 and Jena 6.2.0 on Debian trixie's OpenJDK 21.0.13.
- NCBITaxon 2026-11-09 (S-3's file, 24,029,133 triples), and Uberon 2026-11-05 (S-1's file) as an import.
- Stores built in the app container with `tdb2.xloader` (2 GB heap, gzipped N-Triples or N-Quads input).
- A "page" is the four queries an entity page needs: `DESCRIBE`, children, reference count, and the first 50
  references. `pageload.py` sends pages for 5,005 sampled classes (5 hubs and 5,000 at random) from 1 or 16 concurrent
  clients for 60 s. The bulk queries are S-4's pre-caching queries: the name dictionary and all named subclass edges.

## Results

### Heap

NCBITaxon in a named graph, served with `tdb:unionDefaultGraph`:

| Fuseki heap | Bulk: dictionary, edges | Pages/s, 1 client | Pages/s, 16 clients | p50 / p95 at 16 | Max heap after GC |
| --- | --- | --- | --- | --- | --- |
| 4 GB | 40 s, 18 s | 181 | 407 | 29 / 115 ms | 492 MiB |
| 2 GB | 40 s, 17 s | 206 | 447 | 22 / 102 ms | 436 MiB |
| 1 GB | 41 s, 19 s | 223 | 341 | 22 / 107 ms | 433 MiB |
| 512 MB | 36 s, 17 s | 191 | 234 | 92 / 102 ms | 511 MiB (all of it) |
| 256 MB | dictionary ran out of memory after 7.4 s | — | — | — | — |

- The heap held at most 430–490 MiB after GC, during the dictionary query. The default is 1 GB.
- Throughput varied between sessions by more than between heap sizes: the same store at 2 GB gave 284 and 647 pages a
  second in a later run (below), probably depending on how much of the store the OS had cached.
- At 256 MB, Fuseki had already sent HTTP 200 when it ran out of memory, so the client got a truncated 60 MB result
  (of 451 MB) without an error. Fuseki then hung, collecting garbage, and answered nothing. With
  `-XX:+ExitOnOutOfMemoryError` it exits instead, Compose restarts it, and the client sees the connection close.
- Full GCs were all `System.gc()`, called by the JDK when direct (I/O) buffers reach their limit, which defaults to the
  heap size: 176 at 4 GB, 679 at 1 GB, each pausing 70–275 ms. With the limit at 64 MB they rose to 1,590 and
  throughput fell to 38 / 34 pages a second; at 4 GB they fell to 184, but the buffers held memory the store's cache
  needed (container memory reached 7.3 of 7.65 GB) and throughput fell to 128 / 264.

### Store layout

| NCBITaxon in | Load | Store |
| --- | --- | --- |
| a named graph | 358 s | 5.2 GB |
| the default graph | 165 s | 2.4 GB |

Page queries, Fuseki at 2 GB; the default-graph store with Uberon added as an import (11 s, store 3.2 GB):

| Store, query form | Pages/s, 1 client | Pages/s, 16 clients | p50 / p95 at 16 |
| --- | --- | --- | --- |
| named graph, `tdb:unionDefaultGraph` | 284 | 647 | 21 / 40 ms |
| default graph, plain | 288 | 641 | 21 / 41 ms |
| default graph, `FROM` merge of every graph | 244 | 682 | 21 / 34 ms |
| default graph, `{ … } UNION { GRAPH ?g { … } }` | 177 | 658 | 22 / 35 ms |

Triples about two classes, by query form:

| Class | plain | `FROM` merge | `UNION` |
| --- | --- | --- | --- |
| `NCBITaxon_9606` (NCBITaxon; Uberon restates 6 of its triples) | 8 | 9 | 14 |
| `UBERON_0000948` (Uberon only) | 0 | 66 | 66 |

The `FROM` merge is a set; `UNION` returns a triple once per graph that states it.

## Reproducing

From the repo root, with the stack running (`docker compose up -d`) and S-1's, S-3's and S-4's data fetched:

```sh
mkdir -p research/P-3/data
python3 research/P-3/sample_iris.py > research/P-3/data/iris.txt
# Heap runs: NCBITaxon in a named graph, served as the union default graph
research/P-3/load.sh research/S-3/data/ncbitaxon.owl ncbitaxon-ng http://purl.obolibrary.org/obo/ncbitaxon.owl
FUSEKI_TEST_OPTS=-Dtdb:unionDefaultGraph=true research/P-3/heap-runs.sh ncbitaxon-ng 4g 2g 1g 512m 256m
FUSEKI_TEST_OPTS='-Dtdb:unionDefaultGraph=true -XX:MaxDirectMemorySize=64m' research/P-3/heap-runs.sh ncbitaxon-ng 1g
# Store layout: NCBITaxon in the default graph, Uberon as an import
REGISTER=0 research/P-3/load.sh research/S-3/data/ncbitaxon.owl ncbitaxon-dg
research/P-3/add-import.sh research/S-1/data/downloads/uberon.owl ncbitaxon-dg http://purl.obolibrary.org/obo/uberon.owl
research/P-3/compare.sh
```

The heap runs above were measured on the named-graph store before the default-graph layout was chosen; on the
default-graph store, run them without `-Dtdb:unionDefaultGraph=true`. Remove a dataset and its store afterwards with
`docker compose exec app curl -X DELETE 'http://fuseki:3030/$/datasets/<dataset>'`.
