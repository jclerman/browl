# S-2: ROBOT reasoning with time and memory limits — findings

Spike for [#12](https://github.com/jclerman/browl/issues/12). The decisions are in
[DESIGN.md §2.4](../../docs/DESIGN.md#24-load-pipeline), [§2.12](../../docs/DESIGN.md#212-configuration) and
[§2.14](../../docs/DESIGN.md#214-risks); this records how they were reached. Measured on an Apple M3 Mac (12 cores,
36 GB), Java 21 (Temurin 21.0.13), ROBOT v1.9.11, the version browl's Dockerfile pins.

## Conclusion

- **A runaway run can be killed cleanly** (Q1). browl starts ROBOT in its own process group and, at the time limit,
  sends the group SIGTERM, then SIGKILL after a grace period. With ELK, HermiT and ReasonReuse alike, the JVM exited
  on SIGTERM within a fraction of a second (status 143) and left no process behind. ROBOT is one JVM: the `robot`
  shell script `exec`s `java`, so there's no wrapper to outlive it, and P-7 can run `java -jar robot.jar` directly.
- **A memory breach is detected reliably only with `-XX:+ExitOnOutOfMemoryError`** (Q2). Without it, the
  `OutOfMemoryError` text depends on which thread hit it: HermiT's main thread printed
  `Exception in thread "main" java.lang.OutOfMemoryError`, but ELK's workers printed only `Exception in worker
  thread:` and ROBOT's usual "Use the -vvv option" lines (status 1), the same as any other failure. With the flag, any
  thread's breach ends the JVM at once with status 3 and `Terminating due to java.lang.OutOfMemoryError: Java heap
  space`, for all three reasoners. Status 3 is otherwise unused by ROBOT, which exits 1 on every error.
- **Every failure kind browl reports has a distinct signature** (below), each reproduced by a small ontology from
  `gen.py`. The four logical and parse failures take under a second.
- **Defaults** (Q3): keep `REASONER_TIMEOUT_S=3600`. `REASONER_MAX_HEAP=8g` is ample for Uberon-sized ontologies with
  ELK (which needs 1 GB), but NCBITaxon with ELK needs 16 GB (it fails at 12 GB). HermiT can't classify Uberon in an
  hour, or in 16 GB, so no default makes HermiT work at that scale; the limits are what stop it.
- **ReasonReuse isn't worth folding in yet.** It didn't finish Uberon either (time limit, an hour in; stock HermiT ran
  out of 16 GB after 8 minutes). It also adds a failure kind: on a 40,000-class chain its deeper recursion overflowed
  the default 2 MB thread stack (`StackOverflowError`), where stock HermiT ran out of heap instead. Revisit when the
  ROBOT PR lands, on an ontology HermiT can finish.

## Failure kinds

browl checks, in order (the first match wins):

| Kind | Detected by | Reproduced by |
| --- | --- | --- |
| time limit | browl's own timer fired and killed the group (status 143, or −9 after SIGKILL) | `php-12.ofn` (HermiT, ReasonReuse: 12-hole pigeonhole, exponential for a tableau), `chain-40000.ofn` (ELK with a 16 GB heap and 5 s limit) |
| memory limit | status 3 and `Terminating due to java.lang.OutOfMemoryError` | `chain-40000.ofn` with a 1 GB heap (ELK, HermiT); ReasonReuse with `-Xss512m` |
| stack overflow | `StackOverflowError` in the output | `chain-40000.ofn`, ReasonReuse with the default stack |
| inconsistent | `ERROR org.obolibrary.robot.ReasonerHelper - The ontology is inconsistent` | `inconsistent.ofn` |
| unsatisfiable classes | `ERROR org.obolibrary.robot.ReasonerHelper - There are N unsatisfiable classes`, followed by one `unsatisfiable: IRI` line per class | `unsatisfiable.ofn` |
| parse error | `INVALID ONTOLOGY FILE ERROR` | `parse-error.ttl` |
| other failure | any other non-zero status; report ROBOT's last lines | — |

Notes:

- Match ROBOT's log lines, not exception names. `robot reason` prints no stack trace without `-vvv`, so
  `InconsistentOntologyException` and `IncoherentTBoxException` appear only under ReasonReuse, which doesn't catch
  them. The `ReasonerHelper` lines appear with both.
- ROBOT's log lines need `-vv` (or ReasonReuse, which sets INFO itself); `INVALID ONTOLOGY FILE ERROR` and the
  out-of-memory line are printed regardless.
- The unsatisfiable-classes lines name the classes, which the job could show.
- ROBOT writes the output only after reasoning succeeds, so a failed run leaves no partial output. P-7 should still
  write to a temporary path and rename it, since a kill during the write could.

## Measurements

`run.py` runs one case and prints a row; peak RSS is the child's `ru_maxrss`. On macOS that undercounts a large heap
(memory compression): the GC logs (`-Xlog:gc`) are the better measure of the heap needed.

Small cases, 1 GB heap unless stated:

| Input | Reasoner | Result | Time |
| --- | --- | --- | --- |
| `ok`, `inconsistent`, `unsatisfiable`, `parse-error` | all three | as named | ≤ 0.5 s |
| `php-12` | HermiT, ReasonReuse | time limit (30 s) | 30.1 s |
| `chain-40000` | ELK | memory limit | 11 s |
| `chain-40000` | ELK, 16 GB | time limit (5 s) | 5.2 s |
| `chain-40000` | HermiT | memory limit | 17 s |
| `chain-40000` | HermiT, 4 GB (G1 / Parallel GC) | memory limit | 65 / 79 s |
| `chain-40000` | HermiT, 12 GB | memory limit | 338 s |
| `chain-40000` | ReasonReuse | stack overflow | 1.5 s |
| `chain-40000` | ReasonReuse, 4 GB, `-Xss512m` | memory limit | 12 s |

Before a memory-limit exit, the JVM spends its last 10–20 s in back-to-back full collections (24 with G1, 54 with
ParallelGC at 4 GB) that free almost nothing. `-XX:GCTimeLimit` doesn't apply to G1, and the breach was detected either
way, so browl needn't tune this.

Real ontologies, from S-1 and S-3's downloads (Uberon 2026-11-05, NCBITaxon as in S-3):

| Input | Reasoner | Heap | Result | Time | Live heap (GC log) |
| --- | --- | --- | --- | --- | --- |
| Uberon | ELK | 1 / 2 / 16 GB | ok | 13–14 s | 0.65 GB |
| Uberon | HermiT | 16 GB | memory limit | 467 s | 16 GB |
| Uberon | ReasonReuse | 16 GB | time limit (1 h) | 3,600 s | 8 GB, no full GCs |
| NCBITaxon (21.3 M axioms) | ELK | 12 GB | memory limit | 271 s | — |
| NCBITaxon | ELK | 16 / 20 / 24 GB | ok | 380 / 438 / 440 s | 15–17 GB after remark |

Both HermiT runs on Uberon were still in ROBOT's unsatisfiable-class check, which classifies the ontology.

## Running it

```sh
./fetch.sh                      # robot.jar, ReasonReuse compiled against it, and the generated ontologies
python3 run.py data/ok.ofn --reasoner elk --heap 1g --timeout 60
python3 run.py data/chain-40000.ofn --reasoner hermit --heap 4g --timeout 600 --jvm-arg=-Xlog:gc:file=data/logs/gc.log
```

For the real ontologies, link S-3's downloads into `data/real/` (`uberon.owl`, `ncbitaxon.owl`).
