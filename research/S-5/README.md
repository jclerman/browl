# S-5a: browl and OLS4, a best-effort comparison — findings

Spike for [#55](https://github.com/jclerman/browl/issues/55), part of S-5 ([#54](https://github.com/jclerman/browl/issues/54)).
It compares browl with EBI's [OLS4](https://github.com/EBISPOT/ols4) on Uberon and NCBITaxon before browl renders
pages, so **browl's figures are lower bounds**: they leave out what isn't built yet (below). S-5b
([#56](https://github.com/jclerman/browl/issues/56)) repeats the comparison once browl serves real pages.

## Conclusion

- **browl loads faster and needs less to do it.** Uberon is servable in about 14 s in browl, against 65–78 s in OLS4.
  NCBITaxon takes browl about 4 min, while OLS4 can't load it in this Docker VM at all: its `rdf2json` step runs out
  of Java heap at the default 5 GB cap, and again at 7 GB, the most the 7.65 GiB VM allows.
- **browl's page queries are faster.** At 16 clients, browl's Fuseki serves 499 pages a second (Uberon) and 645
  (NCBITaxon), against OLS4's 243 for Uberon. At 1 client, browl's median page takes 2.1–2.4 ms, or about 4.4 ms with
  S-4's rendering estimate added; OLS4's takes 16.6 ms. browl doesn't render pages yet, so these are lower bounds.
- **browl's store is smaller, but Fuseki uses more memory under load.** Uberon's TDB2 store is 137 MB against OLS4's
  299 MB of PostgreSQL. Idle, the two stacks use about the same memory (626 and 677 MiB). Under 16-client load,
  Fuseki's container grows to 2.6 GiB, while OLS4's three containers total 1.7 GiB. Fuseki's growth is anonymous
  memory (heap plus direct buffers), not page cache.
- **OLS4's "Related from" is a strict subset of browl's planned Usage tab (U-26).** On 55 Uberon classes it lists
  335 of the 555 entities U-26 would show, and none that U-26 wouldn't. It drops entities that refer to a class
  only through an equivalence definition (genus or union operand), a GCI, `DisjointClasses`, a property domain or
  range, or an annotation value. Direct subclasses are missing from it too, but OLS4 shows them in its tree.
- **Equivalence-only references never show up in OLS4.** None of 22 such referrers in Uberon, or 39 in the pizza
  ontology, appear in "Related from". In pizza, whose release isn't relaxed, OLS4's `hasTopping` tree also misses the
  children that come from equivalence definitions (`CheeseyPizza` under `CheeseTopping`), which U-38 includes. On
  Uberon the two part-of trees match exactly, because Uberon's release adds relaxed `SubClassOf` copies of those
  definitions.

## Setup

- Docker Desktop 29.9 on an Apple M3 Pro (12 cores, 36 GB RAM); the Docker VM had 12 CPUs and 7.65 GiB. Each stack
  ran on its own, with nothing else running in the VM.
- **OLS4**, commit `94cf11a` (2026-11). The `:dev` images of the backend, dataload and frontend, and `pgvector`, are
  published for arm64 and ran natively:

  | Image | Digest |
  | --- | --- |
  | `ghcr.io/ebispot/ols4-backend:dev` | `sha256:55be32d2…` |
  | `ghcr.io/ebispot/ols4-dataload:dev` | `sha256:56f66323…` |
  | `ghcr.io/ebispot/ols4-frontend:dev` | `sha256:7a375975…` |
  | `pgvector/pgvector:0.8.0-pg17` | `sha256:40b40496…` |

  Only the Nextflow image is amd64-only. It's built here from [`Dockerfile.nextflow`](Dockerfile.nextflow), OLS4's
  own Dockerfile moved from Debian bullseye (whose apt archive is gone, so it no longer builds) to bookworm.
  Under emulation (the informal run in #54), Uberon's dataload took 87 s; natively it took 65–78 s.
- **browl**: `main` at P-3 (the Compose stack, Fuseki 6.2.0, 1 GB Fuseki heap). Each ontology is loaded with P-3's
  `load.sh` (RDF/XML to N-Triples with `riot`, then `tdb2.xloader` with a 2 GB heap) into the default graph. Then S-4's
  bulk pre-caching queries run: the name dictionary and all named subclass edges.
- **Ontologies:** Uberon 2026-11-05 (S-1's file, 96 MB RDF/XML), NCBITaxon 2026-11-09 (S-3's file, 2.1 GB RDF/XML,
  24 M triples), and the [pizza ontology](https://github.com/owlcs/pizza-ontology) for content (97 classes).
- **One load generator for both**, [`pageload.py`](pageload.py), run in a container on each stack's network. Each
  client sends a page's requests one after another, for 60 s:
  - a **browl page** is P-3's four queries: `DESCRIBE`, children, reference count, first 50 references;
  - an **OLS4 page** is the seven API calls its class page makes (`frontend/src/pages/ontologies/ontologiesSlice.ts`):
    the ontology, the entity, its individuals, every page of "Related from", and for the tree, the hierarchical
    ancestors, hierarchical children and direct children.

  The browser makes OLS4's calls partly in parallel, so a user sees less than the sum. Neither figure includes
  rendering: OLS4 renders in the browser, browl will render on the server (S-4: p50 2.0 ms, p95 3.5 ms a page).
  Pages sample 5 hub classes and 5,000 at random: [`sample_iris.py`](sample_iris.py) for Uberon, P-3's sample for
  NCBITaxon.
- **Memory** is `docker stats` for each container, sampled every second: idle (30 s after start, then 20 s of
  samples) and during each load run.

What browl's figures leave out, which S-5b will include:

- **Load:** reasoning (S-2), the derived SQLite database (entity list, counts, search index), and the job around the
  load. S-4 measured Uberon end to end, upload to ready, at 7.0 s.
- **Store:** the derived SQLite database; S-4's Uberon aggregates were 23 MB.
- **Memory:** the app's rendering JVM working; the app container here is idle (413–435 MiB).
- **Page speed:** rendering, the app's HTTP layer, and the Usage tab's and relation trees' queries.

## Results

### 1. Load

| | Uberon | NCBITaxon |
| --- | --- | --- |
| **OLS4** dataload | **78 s** (reruns: 65 s, 70 s) | **fails**: out of heap after 79 s (5 GB cap) and 115 s (7 GB) |
| **browl** TDB2 load (convert, xloader, register) | 12 s (1 s, 10 s) | 178 s (24 s, 140 s) |
| **browl** bulk queries (dictionary, edges) | 2 s (0.9 s, 0.3 s) | 54 s (37 s, 16 s) |
| **browl** total, lower bound | **14 s** | **232 s** |

OLS4's Uberon steps (Nextflow trace, first run):

| Step | Time | Peak RSS |
| --- | --- | --- |
| `rdf2json` | 11.6 s | 3.5 GB |
| `linker__link_ontologies` | 15.7 s | 356 MB |
| `json2postgres` | 11.4 s | 16 MB |
| `create_postgres` | 13.4 s | 28 MB |
| `extract_sssom`, `linker__create_manifest`, others | about 15 s together | up to 2.8 GB |

Nextflow's own start-up and scheduling make up the rest. Steps run one at a time here.

**NCBITaxon in OLS4.** `rdf2json` parses the whole RDF/XML file into memory with Jena before converting it. With the
local config's 5 GB per step, it gets a 4.6 GB heap (90%) and fails with `Java heap space` after 79 s. At 7 GB (6.3 GB
heap; `NF_MEM=7.GB`, below) it fails after 115 s, at a 6.7 GB peak RSS. Neither failure stops the pipeline:
`dataload.sh` exits 0, the later steps run on empty input, and only `out/loading_report.txt` says
`FAILED_NO_FALLBACK`. Loading NCBITaxon would need a Docker VM larger than 7.65 GiB: at least 8 GB for `rdf2json`
alone, probably more. browl loads it with a 2 GB xloader heap and serves it from a 1 GB Fuseki heap.

### 2. Store

| | Uberon | NCBITaxon |
| --- | --- | --- |
| **OLS4** PostgreSQL (`out/postgres`) | 299 MB | — |
| OLS4 text-tagger database (gzipped) | 4 MB | — |
| **browl** TDB2 store | **137 MB** | **2.4 GB** |
| browl derived SQLite (not built; S-4's figure) | about 23 MB | — |

OLS4's dataload also leaves 2.1 GB of Nextflow work files for Uberon (`tmp/work`), which can be deleted afterwards,
and `out/postgres.tgz`, a copy of the database for shipping.

### 3. Memory

MiB, median over each phase (Uberon, and NCBITaxon for browl):

| | Idle | 1 client | 16 clients |
| --- | --- | --- | --- |
| OLS4 PostgreSQL | 99 | 167 | 208 |
| OLS4 backend | 516 | 829 | 1,425 |
| OLS4 frontend (nginx) | 62 | 61 | 61 |
| **OLS4 total, Uberon** | **677** | **1,057** | **1,694** |
| browl Fuseki, Uberon | 191 | 1,755 | 2,627 |
| browl app (idle throughout) | 435 | 435 | 435 |
| **browl total, Uberon** | **626** | **2,190** | **3,062** |
| browl Fuseki, NCBITaxon | 194 | 1,727 | 2,637 |
| browl total, NCBITaxon | 607 | 2,140 | 3,051 |

- **Fuseki's memory under load is the same for both ontologies**, so it's set by the JVM, not by the store. After the
  NCBITaxon run, the container's cgroup counted 2,509 MiB anonymous memory and 59 MiB file cache. That fits a 1 GB
  heap, about as much again in direct (I/O) buffers, whose default limit is the heap size (P-3), and the JVM's own
  overhead. P-3 measured at most 430–490 MiB of heap in use after GC, so the heap itself isn't the limit.
- OLS4's backend grows with load in the same way (516 to 1,425 MiB).

### 4. Page speed

| | Pages/s, 1 client | p50 / p95, 1 client | Pages/s, 16 clients | p50 / p95, 16 clients |
| --- | --- | --- | --- | --- |
| **OLS4**, Uberon | 57 | 16.6 / 31.7 ms | 243 | 61 / 109 ms |
| **browl** queries, Uberon | 293 | 2.4 / 5.3 ms | 499 | 22 / 55 ms |
| **browl** queries, NCBITaxon | 374 | 2.1 / 3.7 ms | 645 | 21 / 35 ms |

- With S-4's rendering time (p50 2.0 ms, p95 3.5 ms) added, a browl page at 1 client would take about 4.4 ms at the
  median and 9 ms at p95, still about 4× faster than OLS4's API calls.
- OLS4's slowest call is `hierarchicalAncestors` (p50 9.1 ms alone, 28 ms at 16 clients); each of the others has a
  median of about 1 ms alone. browl's four queries take about 0.5–0.6 ms each.
- OLS4's backend answered in 9 s after `docker compose up`; S-4 measured Fuseki at 0.6–0.9 s.
- No errors in any run.

### 5. Content: "Related from" and Usage, part-of trees

[`content.py`](content.py) loads the ontology in the OWLAPI (via JPype, from S-1's `robot.jar`) and, for each sampled
class, compares what U-26 would list with OLS4's "Related from" (every page), and U-38's children for one property
with OLS4's hierarchical children minus its subclass children. Samples: Uberon's 5 hubs plus 50 random classes, with
`part_of` (`BFO_0000050`) as OLS4's hierarchical property; all 97 pizza classes, with `hasTopping`. U-26's referring
entities are as the design says: the axiom's subject (the named subclass, the subject of an annotation assertion,
the named operands of `EquivalentClasses` and `DisjointClasses`, the property of a property axiom), otherwise every
named entity the axiom mentions.

| | Uberon (55 classes) | Pizza (97 classes) |
| --- | --- | --- |
| U-26 axioms / referring entities | 761 / 555 | 1,197 / 1,082 |
| OLS4 "Related from" entities | 335 | 147 |
| In both / browl only / OLS4 only | 335 / 220 / **0** | 147 / 935 / **0** |
| Referrers through equivalence definitions only | 22, **none in OLS4** | 39, **none in OLS4** |
| Relation-tree children, U-38 / OLS4 | 147 / 147 | 116 / 113 |

Entities only browl lists, by how they refer to the class:

| How the entity refers to the class | Uberon | Pizza |
| --- | --- | --- |
| Direct subclass, `SubClassOf(C, X)` (in OLS4's tree instead) | 118 | 84 |
| Direct subclass, also through an equivalence definition | 26 | — |
| Equivalence definition only: genus, `X and (P some D)`, or union operand | 22 | 39 |
| `SubClassOf` with `X` in a GCI, e.g. `(A and part_of some NCBITaxon_40674) SubClassOf …` | 32 | 1 |
| `DisjointClasses` | 3 | 794 (and 1 also through an equivalence definition) |
| Property domain or range | 16 | 11 |
| Annotation assertion with `X` as its value | 2 | — |
| Class assertion (an individual) | — | 5 |

- **OLS4's "Related from" is what the design says** (§Later, "Novel features"): only entities with
  `SubClassOf(P some X)` restrictions on `X`. It's never larger than U-26 and adds nothing U-26 lacks.
- **Heart (`UBERON_0000948`)**: U-26 gives 107 axioms from 69 entities, OLS4 68 entities. The one entity OLS4
  misses refers to heart in an annotation value (`RO_0002171`, "mutually spatially disjoint with"). (#54's informal
  count of 110 axioms from 71 entities used a different rule for referrers.)
- **Relation trees:** on Uberon, U-38's part-of children and OLS4's match exactly (147). That's because Uberon's
  release is relaxed: every `C ≡ A and (part_of some D)` also has `C SubClassOf part_of some D`. On pizza, which isn't
  relaxed, OLS4's `hasTopping` tree misses the 3 children that come only from equivalence definitions: `CheeseyPizza`
  under `CheeseTopping`, `MeatyPizza` under `MeatTopping`, `SpicyPizza` under `SpicyTopping`. U-38 includes them.
- **Pizza's `DisjointClasses` dominate its Usage tab:** 794 of the 935 browl-only referrers. Its toppings are declared
  pairwise disjoint, so each topping lists every sibling. U-34's filter by axiom type addresses this.

## Raised for browl's plan

1. **Fuseki's container memory isn't its heap.** `docker-compose.yml` says a 1 GB heap "serves NCBITaxon with about
   2x headroom", which is true of the heap, but under load Fuseki's container reaches about 2.6 GiB (§3). Filed as
   S-6 ([#59](https://github.com/jclerman/browl/issues/59)).
2. **`DisjointClasses` in the Usage tab (U-26).** U-26 lists a `DisjointClasses` axiom under every named class it
   mentions, so each class's Usage tab repeats the disjointness its Details tab already shows under Disjoint With.
   In pizza, whose 23 named pizzas are pairwise disjoint, each named pizza's Usage tab holds 22 entries, all of
   them `DisjointClasses`. That fits on one page (U-26 pages 50 at a time), so it isn't a navigation problem, but
   the tab and its count show nothing the Details tab doesn't. Decided: U-26 stays as it is, a complete list of
   the axioms that refer to the entity, and U-34's filter by axiom type handles the repetition.
3. **For S-5's conclusions (#54):** browl loads ontologies that OLS4 can't load in a small VM
   (NCBITaxon), and serves pages faster. Its Usage tab shows what OLS4's "Related from" leaves out. Its relation
   trees follow equivalence definitions in releases that aren't relaxed.

## Reproducing

From the repo root. Fetch S-1's and S-3's data first (`research/S-1/fetch.sh`, `research/S-3/fetch.sh`), create
S-1's venv (`research/S-1/.venv`, from its `requirements.txt`), and build browl's images (`docker compose build`).
Content needs a Java 21 JDK for JPype.

```sh
research/S-5/fetch.sh                                   # pizza ontology and its class list
python3 research/S-5/sample_iris.py > research/S-5/data/iris-uberon.txt

# OLS4: a checkout at 94cf11a, with the arm64 Nextflow image
export OLS=$PWD/../ols4       # anywhere Docker Desktop can mount
git clone https://github.com/EBISPOT/ols4 "$OLS" && git -C "$OLS" checkout 94cf11a
docker build -f research/S-5/Dockerfile.nextflow -t ols4-nextflow:arm64 research/S-5

# 1–4 on Uberon. Stop browl's stack (docker compose stop) before OLS4's runs, and OLS4's before browl's.
research/S-5/ols4-load.sh uberon UBERON research/S-1/data/downloads/uberon.owl http://purl.obolibrary.org/obo/BFO_0000050
research/S-5/ols4-serve.sh uberon research/S-5/data/iris-uberon.txt
(cd "$OLS" && HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose -p ols4 down)
research/S-5/browl-run.sh uberon research/S-1/data/downloads/uberon.owl research/S-5/data/iris-uberon.txt

# NCBITaxon: OLS4 at its default 5 GB per step, then at 7 GB; browl
docker compose stop
research/S-5/ols4-load.sh ncbitaxon NCBITaxon research/S-3/data/ncbitaxon.owl
NF_MEM=7.GB research/S-5/ols4-load.sh ncbitaxon NCBITaxon research/S-3/data/ncbitaxon.owl
research/S-5/browl-run.sh ncbitaxon research/S-3/data/ncbitaxon.owl research/P-3/data/iris.txt

# 5. Content (browl's stack stopped)
docker compose stop
research/S-5/content.sh uberon UBERON research/S-1/data/downloads/uberon.owl \
  http://purl.obolibrary.org/obo/BFO_0000050 research/S-5/data/iris-uberon.txt 50
B=https://raw.githubusercontent.com/owlcs/pizza-ontology/refs/heads/master/pizza.owl
ONTO_URI=$B BASE_URI="$B#" research/S-5/content.sh pizza pizza research/S-5/data/pizza.owl "$B#hasTopping" \
  research/S-5/data/iris-pizza-all.txt 200
```

Outputs go to `research/S-5/data/`, which isn't committed:

- `ols4-<id>-load.json`, `-trace.tsv` and `-dataload.log` for each OLS4 load;
- `ols4-<id>-serve.jsonl`, `browl-<dataset>-run.jsonl`, and the matching `-mem.tsv` samples;
- `content-<id>.jsonl`, one line per class and a summary.

The NCBITaxon outputs above were renamed `-5g-` and `-7g-` by hand, since the scripts name them by ontology only.

[`ols4-load.sh`](ols4-load.sh) writes OLS4's ontology config, the only OLS4 configuration changed. It runs OLS4's
`dataload.sh` unchanged, except for the Nextflow image and a trace file. Running OLS4 locally, mind these (from #54):
the ontology file must be under the OLS4 checkout and given by absolute path; a failed ontology still exits 0;
and Compose mounts `out/text_tagger_db.bin`, which the scripts unzip from the `.gz` the pipeline writes. Also, stop
OLS4's stack before reloading: the load deletes `out/` from under a running PostgreSQL.
