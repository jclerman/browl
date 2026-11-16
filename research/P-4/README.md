# P-4: the app JVM's heap, and JPype findings

P-4 built the Fuseki client (`browl/fuseki.py`) and the OWLAPI bridge (`browl/owlapi.py`). This records the check of
the default `APP_JVM_MAX_HEAP` and what turned up about running the JVM inside the app.

## The app JVM's heap

`heap.py` parses N-Triples through browl's bridge (`owlapi.ntriples_ontology`) with the JVM at decreasing heap sizes,
each in its own process. A page's fragment is at most a few thousand triples, so a whole ontology is a generous upper
bound. Uberon (from S-1's download, converted with Jena's `riot`: 1,167,135 triples, 160 MB), on an M-series Mac with
Java 21:

| Heap | Result | Parse time | Retained heap |
| --- | --- | --- | --- |
| 1 GB | parsed, 391,300 axioms | 8.3 s | 177 MB |
| 512 MB | parsed | 8.5 s | 177 MB |
| 384 MB | parsed | 12.6 s | 177 MB |
| 256 MB | out of heap: the process exited with code 3 (`-XX:+ExitOnOutOfMemoryError`) | | |

The parse also holds the N-Triples bytes in the heap (160 MB), as a `ByteArrayInputStream`'s array. So the default of
1 GB holds the whole of Uberon with about 2.5× headroom, and is far more than per-page fragments need. It could be
lowered (512 MB would do); 1 GB stays the default, since the JVM only takes heap as it needs it.

## JPype findings

- **Start the JVM on the main thread.** Started from a worker thread, everything worked, but at interpreter exit
  JPype's shutdown (`_JTerminate`) waited forever. Started on the main thread with calls made from a worker thread, the
  process exits cleanly. Uvicorn runs the app's startup on the main thread; tests start the JVM in a session fixture,
  since Starlette's test client runs the startup on another thread.
- **`-Xrs` and `interrupt=False`.** With the JVM's own signal handlers, SIGTERM and SIGINT ended the process at once
  (exit codes 143 and 130), skipping Uvicorn's graceful shutdown and the app's lifespan exit. With `-Xrs` (the JVM
  installs no handlers for them) and JPype's `interrupt=False`, both go to Python, and Uvicorn logs "Shutting down" and
  "Application shutdown complete".
- **The OWLAPI fetches imports.** A fragment with an `owl:imports` triple made the OWLAPI fetch the import from its IRI
  (Uberon: 14 s, then out of heap at 256 MB), even with the manager's IRI mappers cleared. The bridge now adds each
  import named in the fragment to the loader configuration's ignored imports: the import declaration is kept, and
  nothing is fetched.
- **N-Triples comments.** The OWLAPI's N-Triples parser, like the format, skips lines starting with `#`, and the text
  Fuseki appends to a result that fails partway can start with `#` (`## Query cancelled due to timeout during
  execution ##`, S-6). So the Fuseki client accepts only triple lines in N-Triples results; Jena writes no comments or
  blank lines.
- **JPype picks the JVM from `JAVA_HOME`.** Without it, on a Mac with several JDKs, JPype found an old Java 8. The app
  image sets `JAVA_HOME`; locally, point it at Java 21 (`export JAVA_HOME=$(/usr/libexec/java_home -v 21)`).

## Reproducing

From the repo root, with S-1's Uberon download (`research/S-1/fetch.sh`) and Jena from S-4 (`research/S-4/fetch.sh`):

```sh
export JAVA_HOME=$(/usr/libexec/java_home -v 21)
mkdir -p research/P-4/data
research/S-4/lib/apache-jena-6.2.0/bin/riot --output=nt research/S-1/data/downloads/uberon.owl \
    > research/P-4/data/uberon.nt
poetry run python research/P-4/heap.py research/P-4/data/uberon.nt research/S-1/lib/robot.jar 1g 512m 384m 256m
```
