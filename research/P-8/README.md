# P-8: load format and loader — measurements

Measurements behind P-8 ([#20](https://github.com/jclerman/browl/issues/20)): which RDF syntax ROBOT should write for
the TDB2 loader, which of Jena's loaders to use, and what listing an ontology's imports costs. The decisions are in
[DESIGN.md §2.4](../../docs/DESIGN.md#24-load-pipeline); this records how they were measured.

## Setup

- Apple M3 Pro (12 cores, 36 GB RAM); the app container in Docker Desktop's VM (12 CPUs, 8 GB).
- Jena 6.2.0, ROBOT 1.9.11, Java 21.
- NCBITaxon 2026-11-09 (S-3's file, 24,029,133 triples, 2.1 GB of RDF/XML) and Uberon 2026-11-05 (S-1's file,
  1,167,135 triples), each converted by ROBOT to RDF/XML (`.owl`) and Turtle (`.ttl`), the two RDF syntaxes it writes.
- Loaders run with a 4 GB heap on the Mac (`load-formats.sh`) and a 2 GB heap in the app container.

## Results

### Syntax and loader, on the Mac

| Ontology, syntax | File | `tdb2.tdbloader` | `tdb2.xloader` |
| --- | --- | --- | --- |
| Uberon, RDF/XML | 96 MB | 5.0 s (store 338 MB) | 10.6 s (313 MB) |
| Uberon, Turtle | 71 MB | 4.6 s (339 MB) | 10.8 s (313 MB) |
| NCBITaxon, RDF/XML | 2.0 GB | 60 s (3.8 GB) | 186 s (2.6 GB) |
| NCBITaxon, Turtle | 1.5 GB | 63 s (3.8 GB) | 190 s (2.6 GB) |

- The syntax makes no difference to load time; Turtle is the smaller file (by a quarter), so ROBOT writes Turtle.
- ROBOT took 273 s to convert NCBITaxon to either syntax (24 GB heap).

### Loader, in the app container

NCBITaxon as Turtle: `tdb2.tdbloader` 66 s (store 3.7 GB), `tdb2.xloader` 150 s (2.4 GB). Despite the container's
smaller memory, `tdbloader` stays more than twice as fast, so browl uses it for every load. Its store is half as large
again; P-24 checks page speed on it.

### Listing imports

Jena's `riot` streams a document as N-Triples, from which browl keeps the `owl:imports` triples: 1.9 s for Uberon,
22.5 s for NCBITaxon (RDF/XML, on the Mac).

### Checks by hand

In a Compose stack built from the branch: Uberon went from upload to ready in 9 s, with 1,167,135 triples, matching the
loader's count. An ontology importing BFO (by its PURL, which redirects), `file:///etc/passwd` and Fuseki's own admin
URL loaded BFO (1,221 triples) and dropped the other two (`only http and https imports are fetched`; `fuseki has no
public address (172.21.0.2)`), with ELK reasoning. After both containers were recreated, the same dataset was served
and no load ran.

An RDF/XML file whose `DOCTYPE` declares an external entity (`<!ENTITY secret SYSTEM "file://…">`) and uses it in a
label: both `riot` and ROBOT read the label as empty, not the file's content.

## Reproducing

From the repo root, with `JENA_HOME` set to Jena 6.2.0's tools (`make jena` downloads them to `.cache/jena`) and
`robot.jar` at `.cache/robot.jar` (`make robot-jar`), and S-1's and S-3's data fetched:

```sh
research/P-8/load-formats.sh research/S-1/data/downloads/uberon.owl uberon 8g
research/P-8/load-formats.sh research/S-3/data/ncbitaxon.owl ncbitaxon 24g
# In the app container:
docker compose build app
docker compose run --rm --no-deps -v "$PWD/research/P-8/data:/m:ro" --entrypoint bash app -c \
  'time JVM_ARGS=-Xmx2g $JENA_HOME/bin/tdb2.tdbloader --loc /tmp/db /m/ncbitaxon.ttl'
docker compose run --rm --no-deps -v "$PWD/research/P-8/data:/m:ro" --entrypoint bash app -c \
  'time JVM_ARGS=-Xmx2g $JENA_HOME/bin/tdb2.xloader --loc /tmp/db --tmpdir /tmp/xt /m/ncbitaxon.ttl'
# Listing imports:
time ($JENA_HOME/bin/riot --output=nt research/S-3/data/ncbitaxon.owl | grep -c -F '<http://www.w3.org/2002/07/owl#imports>')
```
