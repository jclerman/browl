# S-6: Keep Fuseki's container memory limited — findings

Spike for [#59](https://github.com/jclerman/browl/issues/59), found in S-5a: with a 1 GB heap, Fuseki's container grew
to 2.6 GiB under page load. The decisions are in [DESIGN.md §2.4](../../docs/DESIGN.md#24-load-pipeline) and
[§2.14](../../docs/DESIGN.md#214-risks); this records how they were measured.

## Conclusion

- **The cause is Fuseki's 5 MB HTTP output buffer.** Fuseki 6.2.0 sets Jetty's output buffer to 5 MB
  (`JettyLib.httpConfiguration()`). Jetty's buffer pool doesn't keep buffers that large, so every response allocates
  a new 5 MB direct buffer, which only garbage collection frees. Each page's four queries allocate 20 MB, and the heap
  hardly grows, so the direct-buffer limit (by default the heap size) is reached first: the JDK then calls
  `System.gc()`, 1,044 times in one run, about once per 1 GB of buffers (the run's 210,000 queries at 5 MB each).
  glibc keeps much of the freed memory in its arenas, so the container held up to 2.6 GiB of anonymous memory while
  NMT accounted for under 900 MiB. This is Jena's
  [GH-4084](https://github.com/apache/jena/issues/4084), fixed after 6.2.0 (below).
- **Fix: a `jetty.xml` with Jetty's default output buffer (32 KB).** Under 16-client load, Fuseki's anonymous memory
  stays under 400 MiB (it was 2.6 GiB), and page throughput rises by a third (550 / 800 pages a second at 1 / 16
  clients, against 302 / 573), with no `System.gc()` calls. The jetty.xml otherwise sets up the server as Fuseki
  does: port, header sizes, thread pool (2–20) and error handler.
- **Capping direct buffers alone doesn't work** (Q1). At 64–512 MB, the container shrinks but throughput falls to
  112–309 / 153–520 pages a second, from `System.gc()` churn, and at 64 MB some queries fail outright.
- **The other JVM and malloc settings only treat the symptom** (Q2). `MALLOC_ARENA_MAX=2` halves the container under
  load at no cost (1.75 GiB), and `-XX:TrimNativeHeapInterval` or a fixed `MALLOC_MMAP_THRESHOLD_` do about as well,
  the last at a third of the 1-client throughput. SerialGC instead of G1 saves 1.4 GiB, but still leaves 2.1 GiB.
  Metaspace and thread stacks aren't worth capping: NMT puts them at about 45 and 4 MiB.
- **With the fix, Fuseki's peak is the bulk queries' heap** (Q3, Q4). Anonymous memory peaks at 0.8–1.0 GiB, during
  the name-dictionary query. A Compose memory limit of 1.5 GiB or even 1 GiB kills nothing: the kernel takes the
  store's page cache instead, and the bulk queries slow from 40 s to 47 s (1.5 GiB) and 50 s (1 GiB). Page throughput
  doesn't change. A 768 MB heap passes the bulk queries but saves nothing, since the heap committed during them is
  the same (650–770 MiB).
- **Recommended:** the jetty.xml, `-Xmx1g -XX:MaxDirectMemorySize=64m`, and **about 1.5 GiB for Fuseki's container**
  (the heap plus 0.5 GiB). Uberon confirms it: 393 MiB at most, 526 / 766 pages a second.

## Setup

- Docker Desktop 4.99.0 (Docker Engine 29.9.3) on an Apple M3 Pro (12 cores, 36 GB RAM); the Docker VM had 12 CPUs
  and 7.65 GiB, with nothing else running.
- browl's images at `main` after S-5a (Fuseki 6.2.0 on Debian trixie's OpenJDK 21.0.13), and the stores S-5a left:
  NCBITaxon 2026-11-09 (2.4 GB TDB2 store) and Uberon 2026-11-05 (137 MB), both in the default graph and registered
  with Fuseki.
- [`run.sh`](run.sh) measures one setting. It empties the VM's page cache, so the store's pages are charged to the new
  container as Fuseki reads them, recreates Fuseki with the setting (a Compose override), and then measures:
  - idle: 20 s of samples, from 20 s after Fuseki is healthy;
  - page load from 1 and then 16 clients, 60 s each, with S-5's [`pageload.py`](../S-5/pageload.py) (P-3's four
    queries a page, 5,005 sampled NCBITaxon classes or 5,003 Uberon classes);
  - S-4's bulk queries with P-3's [`pageload.py`](../P-3/pageload.py): the name dictionary (451 MB of TSV on
    NCBITaxon) and all named subclass edges (305 MB). They pass if both complete with the full result.
- Memory is sampled about every 2 s: `docker stats`, and the cgroup's `memory.stat` (`anon`, `file`). NMT
  (`-XX:NativeMemoryTracking=summary`, `jcmd VM.native_memory summary`) is taken idle, 45 s into the 16-client load,
  and after the bulk queries, from a JDK image ([`Dockerfile.jcmd`](Dockerfile.jcmd)) sharing Fuseki's PID namespace.
  A GC log counts `System.gc()` calls. Each run also records OOM kills and restarts (`docker inspect`) and query
  errors.
- **Read `anon` rather than `docker stats`.** `docker stats` counts the store's memory-mapped pages while they're
  active, and how much of the 2.4 GB store a run pulled in varied (`file` 0.3–1.7 GiB on the same setting). Those
  pages are cache: the kernel takes them back under a memory limit (below). `anon` is the heap, direct buffers and
  everything else the JVM and malloc hold.

## Results

### 1. Settings on NCBITaxon

Heap 1 GB unless noted. Container memory is `docker stats` (MiB); anon and file are the cgroup's maxima over the
run, bulk queries included.

| Setting | Median: idle / 1 / 16 clients | Max | Max anon / file | Pages/s, 1 / 16 | p50 / p95 at 16 | Bulk: dictionary, edges | `System.gc()` | OOM, restarts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Fuseki's default (direct = heap) | 224 / 2,645 / 3,540 | 3,968 | 2,581 / 1,719 | 302 / 573 | 23 / 43 ms | 40 s, 17 s | 1,044 | no, 0 |
| `MaxDirectMemorySize=64m` | 228 / 388 / 624 | 1,303 | 1,251 / 398 | 112 / 153, **6 errors** | 99 / 155 ms | 39 s, 17 s | 6,294 | no, 0 |
| `MaxDirectMemorySize=128m` | 213 / 474 / 991 | 1,726 | 1,314 / 755 | 178 / 264 | 59 / 84 ms | 40 s, 17 s | 4,520 | no, 0 |
| `MaxDirectMemorySize=256m` | 228 / 550 / 989 | 1,620 | 1,470 / 469 | 244 / 399 | 36 / 56 ms | 40 s, 17 s | 3,105 | no, 0 |
| `MaxDirectMemorySize=512m` | 224 / 1,043 / 1,620 | 2,158 | 2,076 / 396 | 309 / 520 | 33 / 41 ms | 39 s, 18 s | 1,992 | no, 0 |
| `MALLOC_ARENA_MAX=2` | 222 / 1,342 / 1,751 | 2,079 | 2,033 / 396 | 331 / 582 | 23 / 43 ms | 40 s, 17 s | 1,087 | no, 0 |
| `MALLOC_MMAP_THRESHOLD_=131072` | 225 / 1,016 / 995 | 1,352 | 1,309 / 396 | 206 / 536 | 25 / 48 ms | 40 s, 17 s | 881 | no, 0 |
| `-XX:TrimNativeHeapInterval=1000` | 187 / 1,293 / 1,393 | 1,537 | 1,424 / 396 | 337 / 598 | 23 / 39 ms | 38 s, 17 s | 1,111 | no, 0 |
| `-XX:+UseSerialGC` | 222 / 1,711 / 2,116 | 2,582 | 2,520 / 397 | 322 / 535 | 23 / 48 ms | 42 s, 20 s | 1,062 | no, 0 |
| jetty.xml, 32 KB buffer, Jetty's thread pool (1) | 235 / 397 / 451 | 1,016 | 972 / 415 | 535 / 772 | 20 / 22 ms | 44 s, 18 s | 0 | no, 0 |
| **Recommended**: jetty.xml, `MaxDirectMemorySize=64m` | 223 / 1,572 / 1,622 | 2,319 | **948** / 1,734 | **550 / 800** | 20 / 22 ms | 40 s, 17 s | 0 | no, 0 |
| Recommended, heap 768 MB | 219 / 385 / 402 | 988 | 948 / 412 | 559 / 794 | 20 / 22 ms | 42 s, 18 s | 0 | no, 0 |
| Fuseki's default, `mem_limit: 1536m` (2) | 186 / 776 / 1,301 | 1,536 | 1,469 / 319 | 296 / 704 | 22 / 27 ms | 66 s, 22 s | 0 | no, 0 |
| Recommended, `mem_limit: 1536m` (2) | 192 / 310 / 331 | 1,036 | 991 / 412 | 561 / 793 | 20 / 22 ms | 47 s, 20 s | 0 | no, 0 |
| Recommended, `mem_limit: 1g` (2) | 198 / 307 / 321 | 1,024 | 799 / 412 | 565 / 798 | 19 / 23 ms | 50 s, 24 s | 0 | no, 0 |
| Recommended, heap 768 MB, `mem_limit: 1g` (2) | 194 / 316 / 333 | 1,024 | 789 / 412 | 528 / 787 | 20 / 24 ms | 65 s, 28 s | 0 | no, 0 |

1. An earlier jetty.xml that left out Fuseki's thread pool (2–20 threads) and error handler, so Jetty's defaults (up
   to 200 threads) applied. The final jetty.xml restores both; the runs below it use it.
2. Below 1,792 MiB the JVM counts the container as a small machine and picks SerialGC over G1, so these runs had no
   G1 structures (GC 0 MiB in NMT). Young collections are then frequent enough to free the 5 MB buffers before the
   direct-buffer limit, which is why Fuseki's default, limited to 1.5 GiB, made no `System.gc()` calls.

- **Direct-buffer limit (Q1).** Lower limits shrink the container but call `System.gc()` more often: the 16-client
  median page time rises from 23 ms to 33 (512 MB), 36 (256 MB), 59 (128 MB) and 99 ms (64 MB). At 64 MB, 6 page
  queries failed with `OutOfMemoryError: Cannot reserve 5242880 bytes of direct buffer memory`, which also explains
  P-3's slow 64 MB run. No limit gives both a small container and full throughput.
- **Other settings (Q2).** Each reduces what glibc keeps after the buffers are freed, not the churn itself, so the
  `System.gc()` count stays near 1,000. Jetty's thread pool can only be set through a jetty.xml; with the 5 MB buffer
  fixed, Jetty's default pool of 200 and Fuseki's 20 gave the same memory and throughput. `MaxMetaspaceSize` and
  `-Xss` weren't run: NMT puts metaspace and class space at 45 MiB and thread stacks at 4 MiB committed (50 threads).
- **Memory limit (Q3).** Nothing was OOM-killed, at 1.5 GiB or at 1 GiB. Under a limit, the cgroup's page cache for
  the store (`file`) is what gives way: up to 1.7 GiB of it unlimited, 0.3–0.4 GiB limited. Page queries touch little
  of the store and didn't slow down; the bulk queries, which read all of it, took 7–10 s longer at the recommended
  setting. With Fuseki's default options the 1.5 GiB limit held only because the JVM switched to SerialGC (2), and
  the dictionary query took 66 s.
- **Heap 768 MB (Q4).** The bulk queries pass, but G1 committed 650–770 MiB of heap during them either way, so the
  peak is the same; with a 1 GiB limit as well they slowed to 65 s and 28 s. P-3 found 512 MB too tight, so 1 GB
  stays the default.

### 2. Where the memory goes (NMT)

Committed MiB, 45 s into the 16-client load (and after the bulk queries):

| Setting | Total | Java heap | Class | Thread | Code | GC | Other (direct buffers) | Symbol | NMT | Threads |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Fuseki's default | 542 (864) | 94 (677) | 6 (6) | 4 (4) | 29 (28) | 55 (69) | 285 (5) | 13 (13) | 3 (3) | 52 (52) |
| `MaxDirectMemorySize=256m` | 386 (838) | 60 (662) | 6 (6) | 4 (4) | 23 (23) | 54 (69) | 170 (5) | 13 (13) | 3 (3) | 52 (52) |
| `-XX:+UseSerialGC` | 675 (850) | 124 (737) | 6 (6) | 4 (4) | 27 (27) | 0 (2) | 445 (5) | 13 (13) | 2 (2) | 36 (35) |
| Recommended | 314 (858) | 144 (670) | 6 (6) | 4 (4) | 31 (31) | 56 (69) | 0 (0) | 14 (14) | 3 (3) | 51 (52) |
| Recommended, `mem_limit: 1g` | 196 (896) | 71 (772) | 7 (6) | 4 (4) | 40 (38) | 0 (3) | 0 (0) | 14 (14) | 3 (2) | 36 (35) |

Metaspace (part of Class here) is a further 39 MiB in every run.

- Under page load, Fuseki's heap commits only 60–145 MiB: full `System.gc()` collections shrink it. The direct
  buffers in use ("Other") swing between 0 and the limit.
- With Fuseki's default, NMT accounts for 0.5–0.9 GiB while the cgroup holds up to 2.6 GiB of anonymous memory. The
  difference is freed direct buffers that glibc keeps in its per-thread arenas (up to 8 per CPU), which is why
  `MALLOC_ARENA_MAX`, a fixed mmap threshold and native-heap trimming help.
- With the recommended setting, direct buffers stay under 1 MiB and the JVM's own overhead (code, GC, class,
  symbols, threads) is about 120 MiB. The peak is the heap during the bulk queries: 670–790 MiB committed, and about
  1 GiB anonymous memory in all.

### 3. Uberon

| Setting | Median MiB: idle / 1 / 16 clients | Max MiB | Max anon / file | Pages/s, 1 / 16 | p50 / p95 at 16 | Bulk | `System.gc()` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Fuseki's default | 228 / 1,832 / 2,774 | 2,940 | 2,769 / 291 | 300 / 471 | 23 / 63 ms | 1 s, 0 s | 911 |
| Recommended | 226 / 351 / 392 | 393 | 353 / 151 | 526 / 766 | 20 / 23 ms | 1 s, 0 s | 0 |

Uberon's bulk queries are small, so its peak is the page load: under 400 MiB.

## Upstream

- Jena [GH-4084](https://github.com/apache/jena/issues/4084), "Fuseki Memory Churn When Serving HTTP Responses", is
  this problem. PR [#4088](https://github.com/apache/jena/pull/4088) (merged 2026-08-02) sizes Jetty's buffer pool
  to Fuseki's output buffer so the buffers are reused, and commit `e923f0043` reduces the buffer to 2 MB. Jena 6.2.0
  (tagged 2026-07-27), the latest release, has neither. **Drop browl's jetty.xml once it moves to a Jena release with
  the fix, and remeasure then.**
- Jena's documentation lists `--jetty=jetty.xml` as a supported option, with no caveats. With it, Fuseki builds a plain
  Jetty `Server` from the file, skipping its own thread pool and error handler (a workaround for Jetty GH-10474), so
  browl's jetty.xml sets both as Fuseki does. Error responses (404, 400) were checked to be the same as without it.
- The users@jena list has seen this since Jetty 10 (users@jena, archived at lists.apache.org): "Mystery memory leak in
  fuseki" (2023) found Fuseki's process at 1.5–3 GB with heap and non-heap near 100 MB, and turning off Jetty's direct
  buffers in a Jetty configuration fixed it at a "barely above noise" cost; "Ever-increasing memory usage in Fuseki"
  (Nov 2023) used `-XX:MaxDirectMemorySize` against container kills, with a rule of thumb of about 4x the heap for the
  container. With the smaller buffer, browl's figure is the heap plus 0.5 GiB.

## Raised for browl's plan

1. **Fuseki gets a jetty.xml** (`docker/fuseki/jetty.xml`): Jetty's default 32 KB output buffer, with Fuseki's port,
   header sizes, thread pool and error handler. The image starts Fuseki with `--jetty` instead of `--port`.
2. **Fuseki's JVM options become `-Xmx${FUSEKI_HEAP:-1g} -XX:MaxDirectMemorySize=64m`**, and hosting guidance gives
   Fuseki's container the heap plus about 0.5 GiB: 1.5 GiB at the default heap. The app container comes on top
   (0.4 GiB idle in S-5a, before its rendering JVM does any work).
3. **No `mem_limit` by default.** A limit at that figure worked, and costs only page cache, but a fixed limit in
   `docker-compose.yml` wouldn't follow `FUSEKI_HEAP` when an operator raises it, and it changes the JVM's GC choice
   (SerialGC below 1,792 MiB). The design doc gives the figure for operators who want a limit.
4. **When Jena has the GH-4084 fix**, remove the jetty.xml and rerun `research/S-6/matrix.sh '^nc-(default|rec)'` to
   confirm Fuseki's own buffer pool keeps the container as small.

## Reproducing

From the repo root, with browl's images built from this branch (`docker compose build`) and the stores S-5a's
`browl-run.sh` leaves (NCBITaxon and Uberon, registered with Fuseki), and P-3's and S-5's IRI samples
(`research/P-3/data/iris.txt`, `research/S-5/data/iris-uberon.txt`; see their READMEs). Stop the app
(`docker compose stop app`); nothing else should run in the Docker VM.

```sh
docker build -f research/S-6/Dockerfile.jcmd -t browl-s6-jcmd research/S-6
research/S-6/matrix.sh                       # every setting above, about 4.5 min each; or a pattern, e.g. '^nc-mdm'
python3 research/S-6/summary.py nc-default nc-rec ub-default ub-rec     # Markdown tables
docker compose up -d --force-recreate fuseki # back to the defaults
```

Outputs go to `research/S-6/data/`, which isn't committed: for each setting, its Compose override (`.yml`), memory
samples (`.mem.tsv`), page and bulk results (`.run.jsonl`, `.bulk.txt`), NMT summaries (`.nmt-<phase>.txt`), GC log
and Fuseki's log. `run.sh` takes a setting from the environment (`HEAP`, `OPTS`, `ENVS`, `MEM_LIMIT`, and `JETTY=1`
for the image's jetty.xml; without it, Fuseki's own server setup). It empties the VM's page cache with a privileged
container before each run.
