# S-1: OWLAPI via JPype — findings

Spike for [#8](https://github.com/jclerman/browl/issues/8): can the app hold the ontology in the OWLAPI (from
`robot.jar`, in-process through JPype), and render entity pages in Manchester syntax with entity links, fast enough for
N-3? Design context: [`docs/DESIGN.md`](../../docs/DESIGN.md) §2.1, §2.8, §2.14.

**Conclusion: yes.** The approach works and is far faster than N-3 needs. Four findings change the design (marked
**→ design**), and one assumption about the py-horned-owl fallback turned out to be wrong.

## Setup

- Apple M3 Pro (12 cores), 36 GB RAM, macOS; OpenJDK 17.0.17; Python 3.12.8.
- ROBOT v1.9.11 (`robot.jar`, which bundles OWLAPI 4.5), JPype 1.7.1, py-horned-owl 2.0.0.
- **Coverage ontology:** `data/family.ofn`, the complete sample ontology from the W3C
  [OWL 2 Primer](https://www.w3.org/TR/owl2-primer/) (Appendix 13), copied verbatim. It uses nearly every OWL 2
  construct: 110 axioms of 32 kinds. It has no `rdfs:label`s.
- **Timing ontology:** Uberon release 2026-11-05 (`uberon.owl`, RDF/XML, 96 MB): 391,300 axioms (98,186 logical),
  26,739 classes.

Timings are wall-clock in one process, after a JIT warm-up of 200 pages; percentiles are over 2,000 randomly sampled
classes (seed 1). A "page" is a class's annotation assertions plus its logical axioms (`getAxioms(cls)`), each rendered
to Manchester syntax and turned into HTML with links and keyword markup.

## Answers

### 1. Starting the JVM and loading all three formats

Works. `jpype.startJVM(classpath=[robot.jar])` exposes the OWLAPI classes; a Python class can implement a Java interface
with `@JImplements(..., deferred=True)` (deferred, because the JVM isn't running at import time).

| Uberon as… | Load time | Heap retained |
| --- | --- | --- |
| RDF/XML (`.owl`) | 4.8–5.5 s | ~180 MB |
| Turtle | 6.0 s | ~185 MB |
| OWL Functional (`.ofn`) | 2.2 s | ~150 MB |

- Loading needs about twice the retained heap at peak: Uberon loads with `-Xmx384m` but not `-Xmx256m`. Roughly
  0.5 KB per axiom retained, 1 KB at peak. S-3 should check whether that holds at N-1 scale.
- Collecting every `rdfs:label` (for display names) took 1.6 s.

**→ design (§2.4): imports that can't be fetched.** By default the OWLAPI (and ROBOT) refuse to load an ontology whose
`owl:imports` target can't be fetched (`UnloadableImportException`); the Primer's ontology imports a made-up IRI, and
uploads will too. With `MissingImportHandlingStrategy.SILENT` in the loader configuration, the ontology loads without
the import. browl should load that way, and report the missing imports on the upload page. The same applies to the
ROBOT reasoning step (S-2, P-7), which fails outright on a missing import.

**→ design (§2.4): running out of heap.** An `OutOfMemoryError` during loading reaches Python as a catchable
`java.lang.OutOfMemoryError`, and the JVM can still load another ontology afterwards. But the same error killed JPype's
"Python Reference Queue" thread, which releases Java objects that Python has dropped, so the process would slowly leak
afterwards. After a heap failure, browl should record "memory limit" and restart its process (the container restarts
it), rather than carry on.

### 2. Manchester rendering with link tokens

Works for every axiom kind except one. All 110 axioms of the Primer's ontology render; links, keywords and HTML
escaping come out right (a label of `<b>Person & co</b>` is escaped).

Two ways of marking entities for links were compared:

- **A (design §2.8):** a Python `ShortFormProvider` returns a private-use token per entity; Python swaps tokens for
  links after rendering.
- **B:** OWLAPI's `DefaultPrefixManager`, with its prefixes cleared, renders every entity as `<full IRI>`; Python turns
  each `&lt;IRI&gt;` (after escaping) into a link. No Python code runs inside the renderer.

They produced identical HTML for 109 of 110 axioms. The difference: the renderer prints IRIs that aren't entities, such
as an annotation assertion's subject, directly as `<IRI>` without asking the short-form provider, so A leaves them
unlinked and B links them.

**→ design (§2.8): use B.** It is simpler (no Java interface implemented in Python), about 1.5× faster (below), and also
links IRIs. Its one risk is a literal that contains `<http://…>` text; linking only IRIs that are declared entities
(looked up in the `entity` table) avoids that.

**→ design (§2.8): two rendering gaps.**

- `DatatypeDefinition` axioms render as an empty string in OWLAPI 4's Manchester renderer; browl needs a fallback (e.g.
  functional syntax) for any axiom that renders empty.
- Built-in datatypes render as entities (`XMLSchema#integer`), so they need the CURIE display names the design already
  plans (`xsd:integer`).

The renderer object keeps state between calls, so it isn't shared between threads; creating one per page costs
nothing measurable.

### 3. Referencing axioms (Usage tab)

`ontology.getReferencingAxioms(entity, Imports.INCLUDED)` returns them from the OWLAPI's own index. For the most
referenced classes in Uberon (up to 2,589 referencing axioms), getting the count and rendering the first 50 took about
1 ms. Across the sample, a Usage page of 50 took p95 0.25 ms.

This suggests the precomputed `usage` table (§2.5, P-27) may not be needed for counts or paging: the OWLAPI index
already gives both. Paging would need a stable order (the index returns an unordered set), and grouping by referring
entity costs a sort per page. S-3 should confirm the timing at N-1 scale before P-27 decides.

### 4. Render time, alone and concurrently

| Work | p50 | p95 | max | Pages/s |
| --- | --- | --- | --- | --- |
| Class page, A (Python provider), serial | 0.22 ms | 0.52 ms | 1.2 ms | ~4,000 |
| Class page, B (IRI provider), serial | 0.15 ms | 0.34 ms | 0.8 ms | ~5,900 |
| Class page, B, 4 threads | 1.14 ms | 2.6 ms | 7.4 ms | ~3,100 |
| Class page, B, 8 threads | 2.34 ms | 5.7 ms | 12.2 ms | ~3,000 |
| Usage page (first 50), B, serial | 0.08 ms | 0.25 ms | 1.7 ms | ~10,000 |

(A with 4 and 8 threads: ~2,000 pages/s, similar pattern.)

- **Fast enough:** p95 is about 0.3–0.5 ms per page against the N-3 target of 300 ms. Rendering won't be the
  bottleneck; N-1 scale doesn't change per-page work much, since lookups are indexed.
- **Threads hurt throughput.** Each page alternates Java calls (which release Python's GIL) and Python work (escaping,
  regexes) that needs it, so threads mostly wait on each other: throughput halves and latency rises.
- **Thread safety:** concurrent renders over a manager from `createConcurrentOWLOntologyManager()` gave output
  identical to serial renders (300 pages, 4 and 8 threads). No errors.

**→ design (§2.1, §2.14): serialize OWLAPI work through one thread.** One worker thread for all OWLAPI calls is both
the fastest option and the simplest: it removes the thread-safety question, and at ~0.3 ms per page it serves thousands
of pages per second. Keep `createConcurrentOWLOntologyManager()` anyway, since the load job and page rendering can
overlap during a reload.

### 5. py-horned-owl

| Uberon as… | Load time | Process memory growth |
| --- | --- | --- |
| RDF/XML | 2.3 s | ~855 MB |
| Turtle | 2.1 s | ~890 MB |
| `.ofn` | 2.7 s | ~1,090 MB |

- **Loads faster** than the OWLAPI for RDF/XML and Turtle, about the same for `.ofn`. Version 2.0 reads all three
  formats.
- **Uses more memory, not less:** roughly 5× the OWLAPI's retained heap for Uberon (process growth vs. JVM heap, so not
  exactly comparable, but the gap is large). The design's risk table assumed the opposite.
- **Stricter parsing:** it rejects the Primer's `.ofn`, which uses `rdfs:` without declaring it; the OWLAPI accepts it.
- **Manchester output:** whole-ontology only (`save_to_string("omn")`), with no per-axiom rendering and no hook for
  link tokens. It has gaps: a datatype defined by a complement renders empty, and a general class axiom renders as
  `Class: <A> and (…)`, which isn't valid Manchester syntax.

**→ design (§2.14):** py-horned-owl is not a good fallback for memory, and would still need browl's own Manchester
renderer. If the OWLAPI's heap turns out to be the problem at N-1 scale (S-3), the remaining fallback is the OWLAPI in a
separate process.

## Reproducing

```sh
cd research/S-1
./fetch.sh                                   # robot.jar and Uberon into lib/ and data/downloads/
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p data/generated && grep -v "^ *Import(" data/family.ofn > data/generated/family.ofn  # drop its unresolvable import
for f in ttl owl; do java -jar lib/robot.jar convert -i data/generated/family.ofn -o data/generated/family.$f; done
for f in ofn ttl; do java -jar lib/robot.jar convert -i data/downloads/uberon.owl -o data/generated/uberon.$f; done
export JAVA_HOME=$(/usr/libexec/java_home -v 17)
.venv/bin/python spike.py coverage                               # formats, imports, rendering, escaping
.venv/bin/python spike.py timing data/downloads/uberon.owl 2000 4,8   # load, heap, render and usage timing
.venv/bin/python spike.py horned uberon.ttl                      # py-horned-owl (paths relative to data/generated)
```

Heap limits were found by loading with decreasing `-Xmx` in separate processes.
