# Load flow: imports, reasoning, conversion, loading

How a load job takes an upload to a served dataset, for each input syntax, with and without a reasoner. This is the
flow agreed in PR #70's review (P-8). It replaces the regex scan for OWL Functional Syntax (OFN) imports with the
OWLAPI, which converts the file to Turtle in the same parse, and adds a sandbox for ROBOT (P-30). The reasons are in
[DESIGN.md §2.4](DESIGN.md#24-load-pipeline) and §2.14.

Steps marked **[sandbox]** run in the ROBOT container planned by P-30: no network, only the current job's files, an
almost empty environment. Until P-30, they run in the app container, with ROBOT's network fetches refused (Java's proxy
settings pointing at a closed port) and an environment holding only what the tools need.

## Flow

```text
UPLOAD   streamed to disk; syntax sniffed: RDF/XML, Turtle, or OFN; anything else is rejected
  │
  ▼
IMPORTS STAGE: a breadth-first walk over the import closure, starting from the upload
  │
  │   for each document in the queue:
  │     LIST its imports
  │        RDF  →  riot, streaming (app container)
  │        OFN  →  OWLAPI with every load refused; the same parse writes Turtle,  [sandbox]
  │                which stands in for the OFN file from then on (the upload's: converted.ttl)
  │        can't be parsed ──► DROPPED  (the upload: the job FAILS)
  │     for each new import IRI but the upload's own   (≤ 100 attempts; deadline for the whole stage)
  │        POLICY   http/https only; public address; no port browsers block;
  │                 checked again on each redirect ────────────────────── no ──► DROPPED
  │        FETCH    size and time limits ───────────────────────────── fails ──► DROPPED
  │        SNIFF    RDF or OFN? ───────────────────────────────────── neither ──► DROPPED
  │        add to the queue
  │
  │   WRITE CATALOG    fetched IRI → its file;  dropped IRI → an empty ontology
  ▼
Reasoner chosen? ── yes ──►  REASON   ROBOT reason over the upload or converted.ttl,  ──► reasoned.ttl
  │ no                                with the catalog  [sandbox]
  ▼
the upload as it is, or converted.ttl for an OFN upload
  │
  ▼
LOAD STAGE
  │   tdbloader → a new store gen-N:  that file → default graph;  each fetched import → named graph <IRI>
  │   COUNT the triples per graph (tdbquery)
  │   SWITCH: register gen-N in Fuseki; count again through Fuseki; must match ── no ──► FAILED, gen-N removed
  │   remove the previous gen-* datasets
  ▼
PRE-CACHE   (P-9: derived data, search index)
  ▼
READY    homepage lists every import: loaded (triple count) or dropped (reason, in red)

In any ROBOT step: an import missing from the catalog fails the job, naming that import.
In the sandbox it can't reach the network or anything worth reading.
```

## The four paths

| | RDF, no reasoner | RDF, reasoner | OFN, no reasoner | OFN, reasoner |
| --- | --- | --- | --- | --- |
| Lists the upload's imports | riot | riot | OWLAPI [sandbox] | OWLAPI [sandbox] |
| Lists fetched imports | riot for RDF, OWLAPI for OFN | same | same | same |
| ROBOT run on the upload | none | reason | none | reason, over `converted.ttl` |
| Loaded into the default graph | the upload | `reasoned.ttl` | `converted.ttl` | `reasoned.ttl` |
| What follows `owl:imports` | nothing; only browl's fetcher fetches | ROBOT, through the catalog | nothing | ROBOT |
| A crafted listing mismatch | can't arise: riot and tdbloader are the same Jena parser | job fails, naming the import | can't arise: nothing follows imports | can't arise: ROBOT reads the Turtle the OWLAPI wrote |
| Whole ontology in memory | no¹ | ROBOT only | the lister only | lister, then ROBOT² |

1. Except a fetched OFN import, which the lister parses whole, one at a time.
2. One after the other, not together, so the peak is the same as ROBOT's alone; the parse time doubles.

## Why it's safe

- **Nothing but ROBOT follows imports.** Jena's `riot` and `tdbloader` read only the file they're given. They don't
  expand external XML entities, and don't fetch an external DTD or a parameter entity (checked in P-8 with a logging
  HTTP server). The app's own per-page OWLAPI parse refuses every document load but the fragment's
  (`browl.owlapi._OnlyDocumentFactory`).
- **Without a reasoner, an RDF upload is never read by the OWLAPI.** browl's fetcher, with its policy, is the only
  thing that fetches, so it's what blocks an import. `riot` lists with the same parser `tdbloader` loads with, so the
  homepage's import list matches the triples served.
- **The OFN lister is exact.** It's the OWLAPI from the same `robot.jar` that ROBOT runs, with every document load
  refused before anything is opened and before any parser runs. In P-8's review it listed every crafted case that got
  past the regex scan, and made no request and read no file; the OWLAPI's default, as ROBOT runs it, made seven
  requests and read a local file. ROBOT then reads the Turtle the lister wrote, not the OFN file. A relative import,
  which can't be fetched, is left out of that Turtle, whose base would resolve it differently.
- **A listing gap fails the job, naming the import, rather than leaking.** `riot` can't promise to list exactly what the
  OWLAPI will try for an RDF file crafted to differ. ROBOT is left on its default, which fails on an import it can't
  load. It reports only the first such import, and skipping silently would report none. In the sandbox, that attempt
  reaches no network and nothing worth reading.

## Steps beyond list, reason, convert and load

- **Syntax sniffing**, at upload and for each fetched import.
- **The fetch policy:** `http` and `https` only, public addresses, size and time limits. It's what blocks imports in the
  path with no reasoner.
- **The recursion:** each fetched import is listed in turn, so the whole closure is followed.
- **The catalog**, which gives ROBOT the fetched files and maps dropped imports to an empty ontology.
- **Converting OFN to Turtle**, even without a reasoner, since `tdbloader` reads only RDF. The lister does it in the
  same parse, for the upload and each fetched import.
- **Count, switch and verify:** the triple counts are checked through Fuseki before the old store is removed.
- **Pre-caching** (P-9).

There's no separate conversion step: the OFN lister writes Turtle in the same parse, and ROBOT's reasoning output is
Turtle.
