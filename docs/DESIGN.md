# browl Design

browl (OWL-Browse) hosts an OWL ontology as a browsable website. Every declared entity gets its own page at a URL
implied by its IRI.

> **Status:** draft. References like _(Q1d)_ point to the question log in `claude/DESIGN-SESSION-STATUS.md`. They are
> temporary, so decisions can be traced and the two documents audited against each other. They will be removed once this
> document is the sole source of truth.
>
> Decisions made after the question log supersede parts of it. They are marked _(post-log)_ or with the spike that
> settled them:
>
> - **The OWLAPI renders Manchester syntax** (supersedes Q6's tentative decision to write our own renderer).
> - **Fuseki/TDB2 holds the ontology and is queried on demand** ([S-4](../research/S-4/README.md)). This supersedes an
>   interim decision to drop Fuseki and hold the whole ontology in an in-process OWLAPI, which S-3 showed takes about
>   70 s to become ready after every restart at NCBITaxon scale. Fuseki also provides the RDFS entailment of Q7.

**Contents:**

1. [Requirements](#1-requirements)
2. [Technical Spec](#2-technical-spec)
3. [Implementation Plan](#3-implementation-plan)

---

## 1. Requirements

Requirements have stable IDs so plan items and tests can refer to them. The prefix gives the persona: **U** (User),
**A** (Admin), **O** (Operator), **N** (non-functional). Each one is marked **v1** or **Later**.

### 1.1 Personas

| Persona | Who they are |
| --- | --- |
| **User** | Anyone browsing the ontology. Anonymous; no sign-in. |
| **Admin** | Signs in to load or replace the ontology, choose reasoning, and manage who else is an admin. |
| **Operator** | Deploys and configures the service: base URL, limits, authentication, storage. |

### 1.2 User requirements

| Requirement | Priority |
| --- | --- |
| **U-1** · The ontology home page shows the ontology's metadata: ontology IRI, version IRI, annotations, imports, entity and triple counts, and whether a reasoner was applied and which one. Imports that couldn't be loaded are listed in a prominent warning (in red), with why. _(Q7c; P-8)_ | v1 |
| **U-2** · Every declared entity has a page at a URL implied by its IRI (see [2.3](#23-iri--url-mapping)). The ontology itself counts as a declared entity, and its page is the home page. No other entity-specific pages exist. _(Q1, Q2a)_ | v1 |
| **U-3** · An entity page shows its immediate parents, its annotation values, and its logical axioms in Manchester syntax. | v1 |
| **U-4** · Every entity mentioned on any page is a link to that entity's page. | v1 |
| **U-5** · Each entity page shows its full IRI near the top, in a collapsed (fold-away) section, plus its CURIE when one exists. _(Q18b)_ | v1 |
| **U-6** · Entity names in links and page titles use the same display name, chosen by the ranking in [2.7](#27-display-names). _(Q18)_ | v1 |
| **U-7** · Collapsible hierarchy trees, one each for classes, object properties, data properties and annotation properties. Each node shows its number of immediate children and of all descendants. Children load when a node is expanded. _(Q20)_ | v1 |
| **U-8** · Descendant counts count each distinct descendant once per ancestor. A class with two parents counts under both; in a "diamond" it counts once under the shared ancestor. _(Q19, Q19a)_ | v1 |
| **U-9** · Equivalent classes appear as one node in the trees and are counted once. Each equivalent IRI keeps its own page, which links to the others. _(Q19, Q19b)_ | v1 |
| **U-10** · Autocomplete search over all entities. Results can be filtered by entity type and by which name annotation properties to search, using either direct values only or also values entailed under RDFS (sub-properties). | v1 |
| **U-11** · Autocomplete matches from the start of the whole value, ignoring case and accents. _(Q8)_ | v1 |
| **U-12** · Autocomplete ranks exact matches first, then values of earlier properties in the configured name list, then more-preferred languages, then shorter values. _(Q9, Q10)_ | v1 |
| **U-13** · Hash IRIs (`…/onto#Foo`) are supported: visiting the hash IRI's URL shows that entity. _(Q2)_ | v1 |
| **U-14** · A URL that isn't a declared entity shows "not found". That includes the bare part of a hash IRI before the `#`, unless it is the ontology IRI. _(Q2a)_ | v1 |
| **U-15** · If `http://` and `https://` forms of the same IRI are both declared, their shared URL shows a page for choosing between them. _(Q1a)_ | v1 |
| **U-16** · While an ontology is being loaded or replaced, the site shows a "loading" page. _(Q14a)_ | v1 |
| **U-17** · Pages work without JavaScript, except for hash IRIs, which need it. Trees and autocomplete are keyboard-accessible. _(Q20)_ | v1 |
| **U-26** · Each entity page has a **Usage** tab (inspired by Protégé's), not shown by default: the axioms in which other entities refer to this entity (logical axioms and annotation assertions whose value is this entity), rendered in Manchester syntax and grouped by referring entity, with a total count and paging. Axioms not about a single entity (general class axioms, `DisjointClasses(A, B, C)`) are listed under every named entity they mention. | v1 |
| **U-27** · A persistent navigation bar on every page links to the ontology home page, the search, and the tree page for each entity type. | v1 |
| **U-28** · Page-specific content is organized in tabs within the page (e.g. an entity's Overview and Usage; for classes, later, Individuals). The tabs are visually distinct from the navigation bar. | v1 |
| **U-29** · The layout makes good use of space and is as readable as possible without looking harsh. | v1 |
| **U-30** · An entity's Overview tab shows annotations in one panel and logical axioms in another. | v1 |
| **U-31** · Property pages show the property's domains and ranges, if any, in the logical-axioms panel. | v1 |
| **U-32** · The panels are compact: each annotation value or axiom takes one line, wrapping only when it doesn't fit. A property with several values (e.g. 10 synonyms) is named once, with one line per value. | v1 |
| **U-18** · Named individuals have pages, and each class page lists its individuals in an Individuals tab, with paging. _(Q17)_ | Later |
| **U-19** · Autocomplete also matches from the start of any word. _(Q8)_ | Later |
| **U-20** · Autocomplete tolerates typos. _(Q8)_ | Later |
| **U-21** · Autocomplete can optionally match IRIs and CURIEs. Never on by default. _(Q12)_ | Later |
| **U-22** · Imported entities can link out to their real IRI where it is hosted. _(Q3)_ | Later |
| **U-23** · Content negotiation: an entity URL returns RDF (e.g. Turtle) when asked for it with the HTTP `Accept` header. _(Q4)_ | Later |
| **U-24** · Optionally hide results in less-preferred languages entirely. _(Q10)_ | Later |
| **U-25** · Optional ranking by entity importance, only if it adds little complexity. _(Q9)_ | Later |
| **U-33** · An annotation property with many values shows them in multiple columns where the page width allows. | Later |
| **U-34** · The Usage tab (U-26) can be filtered by axiom type (SubClass Of, Equivalent To, domain or range, annotation value, …) and by property. | Later |
| **U-35** · Axiom annotations (e.g. cross-references on a synonym or definition) are shown under the annotation value or axiom they annotate. | Later |
| **U-36** · A read-only SPARQL endpoint over the served ontology, with a query time limit. | Later |
| **U-37** · The served ontology (after reasoning, if a reasoner ran) can be downloaded from the home page. | Later |
| **U-38** · Relation trees: besides the subclass tree, a tree for each configured object property (O-12), e.g. `part_of`. A class's parents in it are the classes `D` in its `SubClassOf(P some D)` axioms, including those implied by its equivalence definitions. Counts follow U-8. | Later |
| **U-39** · Where a reasoner ran, inferred parents and axioms are marked as inferred, on entity pages, in the Usage tab and in the trees. | Later |
| **U-40** · An inferred parent or axiom has a "Why?" link that shows its explanation: the asserted axioms that entail it, in Manchester syntax. | Later |
| **U-41** · Each class page can list similar classes, ranked by similarity computed from the ontology's structure (graph embeddings of its axioms and hierarchies), at least as an option alongside any similarity from label text. | Later |

### 1.3 Admin requirements

| Requirement | Priority |
| --- | --- |
| **A-1** · Admins sign in with a Google account. Only admin pages need sign-in. _(Q16)_ | v1 |
| **A-2** · An admin can upload an ontology in RDF/XML, Turtle or OWL Functional Syntax (`.ofn`). OWL/XML is not required. _(Q15)_ | v1 |
| **A-3** · When uploading, an admin chooses no reasoner, ELK, or HermiT. _(Q7c)_ | v1 |
| **A-4** · Uploading replaces the current ontology. One ontology is served at a time. _(Q14)_ | v1 |
| **A-5** · The upload page shows the current stage: uploading, reasoning, loading, pre-caching, ready. _(Q7b)_ | v1 |
| **A-6** · If a stage fails, the upload page says which stage failed and why. For a reasoner failure caused by a limit, it says which limit (time or memory) was hit, where possible. _(Q7)_ | v1 |
| **A-7** · Admins can add and remove admin email addresses. The seed admins (O-6) can't be removed through the UI. _(Q16a)_ | v1 |
| **A-8** · Detailed logs for each stage, inline or on a linked page. _(Q7b)_ | Later |
| **A-9** · Several ontologies loaded and ready to serve. Whether one or many are served at a time is to be decided. _(Q14)_ | Later |

### 1.4 Operator requirements

| Requirement | Priority |
| --- | --- |
| **O-1** · The service starts with one command (`docker compose up`). It runs as three containers: the app, Fuseki, and the sandbox that runs ROBOT (P-30). _(S-4; was a single container)_ | v1 |
| **O-2** · `BASE_URL` (where the service is served) is set per deployment. IRIs under it get their native paths. _(Q1c)_ | v1 |
| **O-3** · The reserved prefix for the service's own pages is configurable, defaults to `_browl`, and must start with `_`. _(Q1d)_ | v1 |
| **O-4** · The ordered list of name annotation properties (default: `rdfs:label` first) is configurable and is used for both display names and search. _(Q18a)_ | v1 |
| **O-5** · The language ranking is configurable. Untagged values rank last by default; their position can be set explicitly in the ranking. _(Q10, Q10a)_ | v1 |
| **O-6** · Google OAuth client settings and a seed list of admin email addresses are configurable. _(Q16, Q16a)_ | v1 |
| **O-7** · A development mode turns authentication off. It must be enabled explicitly and is never the default. _(Q16)_ | v1 |
| **O-8** · Reasoner time and memory limits are configurable. _(Q7)_ | v1 |
| **O-9** · Data that must survive restarts (the loaded ontology, derived data, the admin list) lives on mounted volumes. _(Q16a)_ | v1 |
| **O-10** · The page cache size is configurable (default 100 pages). _(Q13)_ | v1 |
| **O-11** · Extra CURIE prefix mappings are configurable and override the ontology's own and the built-in ones. _(Q18b)_ | v1 |
| **O-12** · The object properties that get relation trees (U-38) are configurable. | Later |

### 1.5 Non-functional requirements

| Requirement | Priority |
| --- | --- |
| **N-1** · Scale: millions of entities with several annotations each, and also about 10K entities with 10–20 annotations each. _(Q5)_ | v1 |
| **N-2** · Autocomplete responds fast enough for type-ahead. Proposed target: p95 under 100 ms server-side at N-1 scale. | v1 |
| **N-3** · An uncached entity page renders fast. Proposed target: p95 under 300 ms at N-1 scale, so a small cache is enough. _(Q13)_ | v1 |
| **N-4** · Nothing that needs the whole ontology (closures, counts, the search index) is computed per request. _(Q5)_ | v1 |
| **N-5** · The search engine sits behind an interface, so replacing it changes only that module. _(Q11)_ | v1 |

---

## 2. Technical Spec

Tooling and code conventions are in [`CODING-STANDARDS.md`](CODING-STANDARDS.md).

### 2.1 Architecture overview

```text
           browser
              │  HTML (Jinja) + htmx
              ▼
   ┌───────────────────────────────────────────────┐
   │  browl app (FastAPI, one worker process)      │
   │  - routing / IRI map     - load pipeline job  │
   │  - page rendering        - search shim        │
   │  - SPARQL client                              │
   │                                               │
   │  ┌─────────────────────────────────────────┐  │
   │  │ in-process JVM (JPype, robot.jar)       │  │
   │  │ OWLAPI: Manchester rendering of the     │  │
   │  │ triples fetched for one page            │  │
   │  └─────────────────────────────────────────┘  │
   └──────┬───────────────┬───────────────┬────────┘
          │ SPARQL (HTTP) │               │ subprocesses
          ▼               ▼               ▼
   Fuseki container   derived SQLite   ROBOT (reasoning),
   TDB2 store: the    (aggregates,     Jena TDB2 loader
   loaded ontology    search index)
                      persistent SQLite
                      (admins, job state)
```

- **Fuseki with a TDB2 store** holds the loaded ontology, in its own container. The store persists on disk, so after a
  restart Fuseki is ready in under a second, whatever the ontology's size. Entity pages query it when requested: an
  entity's own triples, its children, its references; 2–4 ms per query on NCBITaxon
  ([S-4 findings](../research/S-4/README.md)).
- **Derived SQLite database** holds whatever needs the whole ontology, computed once per load from a few bulk SPARQL
  queries: entity list, display names, hierarchy edges and counts, RDFS-entailed name values, hash prefixes, equivalence
  groups, prefixes and the search index. _(Q5, N-4)_
- **OWLAPI in an in-process JVM**, reached through JPype from the classes in `robot.jar`, renders Manchester syntax. For
  each page it parses only the triples fetched for that entity, so it needs a small heap and no whole-ontology load
  ([2.8](#28-entity-pages)). _(post-log)_ The JVM starts at app startup on the main thread: started from another
  thread, JPype's shutdown hangs at exit. It starts with `-Xrs`, which leaves SIGINT and SIGTERM to Python, so Uvicorn
  shuts down gracefully instead of the JVM ending the process at once (P-4). A fragment's imports are kept as
  declarations but never loaded: each fragment's OWLAPI manager has one ontology factory, which refuses to load any
  document but the fragment, so no import's IRI is ever opened, however the triple is written (P-4).
- **ROBOT as a subprocess** runs the optional reasoning, in its own JVM so time and memory limits can be enforced and a
  runaway run killed; in the Compose deployment, in the sandbox container (2.4, P-30). Jena's TDB2 loader also runs as
  a subprocess, building the store for each load. _(Q7c)_
- **One server worker process.** Concurrency comes from async request handling: SPARQL calls use an async HTTP client,
  and all OWLAPI calls run on one dedicated thread. Parallel threads gave no extra throughput, because each render
  alternates Java and Python work, and Python's GIL serializes the Python part
  ([S-1 findings](../research/S-1/README.md)).
- **Owlready2 is not used.** _(Q6)_

### 2.2 Libraries and components

| Concern | Choice | Notes |
| --- | --- | --- |
| Web framework | FastAPI, Uvicorn | |
| Templates | Jinja2 | |
| Front-end behavior | htmx, served from the app's static files (no CDN) | No SPA framework and no front-end build step. _(Q20)_ |
| Data models | Pydantic | Entities, settings, job state. |
| Configuration | pydantic-settings | Defaults in code; environment variables override. _(Q13)_ |
| Ontology store | Apache Jena Fuseki with TDB2, in its own container | Queried with SPARQL over HTTP. Jena 6 needs Java 21. _(S-4)_ |
| SPARQL client | HTTPX2 (async) | Pydantic's maintained continuation of HTTPX, which Starlette's test client also prefers. Plain SPARQL protocol; results read as TSV, fragments as N-Triples. |
| Manchester rendering | OWLAPI's Manchester syntax renderer, from the classes bundled in `robot.jar` | Renders per-page fragments; see [2.8](#28-entity-pages). _(post-log)_ |
| Python ↔ Java bridge | JPype | One JVM per process, started at app startup. _(post-log)_ |
| Derived store and search (v1) | SQLite (Python standard library), queried in a thread pool | FTS5 later for any-word search. _(Q8, Q11)_ |
| Reasoning | ROBOT (Java), as a subprocess | ELK and HermiT. |
| Authentication | Authlib (OpenID Connect with Google), Starlette signed-cookie sessions | |
| Deployment | Docker Compose: `app`, `fuseki` and `sandbox` services, named volumes (2.12) | The app image includes Java 21, `robot.jar` and Jena's command-line tools (for the TDB2 loader). _(S-4)_ |

### 2.3 IRI → URL mapping

Settings: `BASE_URL` and `RESERVED_PREFIX` (default `_browl`). _(Q1, Q1c, Q1d)_

For a declared entity IRI, in order:

1. **Native:** the IRI starts with `BASE_URL`, ignoring `http` vs `https`, and its remaining path does not start with
   `/{RESERVED_PREFIX}`. URL = the IRI's path, e.g. `http://example.com/onto/Foo` → `/onto/Foo`.
2. **Proxied:** the IRI is a plain `http(s)` IRI (no query string, no port, no user info). URL = `/{host}{path}`, e.g.
   `http://example.org/ONTO/x` → `/example.org/ONTO/x`. The scheme is dropped. _(Q1a)_
3. **Fallback:** anything else (`urn:`, `tag:`, query strings, ports, IRIs whose native path would hit the reserved
   prefix). URL = `/{RESERVED_PREFIX}/iri?u={percent-encoded IRI}`. _(Q1b)_

**Comparing IRIs with `BASE_URL`:** `BASE_URL` is stored as a normalized URL (scheme and host lowercased, default port
dropped, a non-ASCII host in its ASCII "punycode" form, non-ASCII path characters percent-encoded). IRIs, which RDF
compares as plain strings, can contain Unicode. So each IRI is mapped to its URL form first (RFC 3987 §3.1), then
compared with `BASE_URL`: scheme and host case-insensitively, the path exactly.

For a hash IRI, the rules apply to the part before `#`, and the fragment is kept on the URL:
`http://example.org/onto#Foo` → `/example.org/onto#Foo`.

**Why this can't clash with the service's own pages:** all of them live under `/{RESERVED_PREFIX}/`. Proxied URLs start
with a hostname, and hostnames can't contain `_`, hence the leading-`_` rule on the prefix (checked at startup). Native
IRIs that would collide get the fallback, and the pre-caching stage logs a warning listing them.

**Lookup:** pre-caching stores, for every declared entity, its URL path and fragment. A request is resolved by looking
up the path:

| The path matches… | Response |
| --- | --- |
| the ontology IRI | Home page. If hash entities share this path, the page also includes the hash script below. |
| one entity (non-hash) | That entity's page. |
| two entities (only `http` vs `https` differs) | Disambiguation page. _(Q1a)_ |
| a hash prefix only | A small page with a script that reads the fragment and loads that entity, or shows "not found" when there is no fragment or no such entity. HTTP status 200, since the server can't see the fragment. _(Q2a)_ |
| nothing | HTTP 404. |

A path can be both an ordinary entity and a hash prefix; then the entity page is served with the hash script included.

**Reserved paths:** the lookup route (P-13) answers 404 for any path under `/{RESERVED_PREFIX}/`, and a test checks it.
The site gate ([2.13](#213-url-space-for-the-services-own-pages)) exempts paths by prefix without normalizing them, so
`/_browl/admin/../../x` (sent as `/_browl/admin%2F..%2F..%2Fx`) passes it while a load runs or has failed, and must not
reach a lookup (P-5).

### 2.4 Load pipeline

One background job at a time, run in the app process. Its state (stage, start time, error) is stored in SQLite, so every
server worker reports it consistently. _(Q7b, Q14a)_ It's kept in `DATA_DIR/state.sqlite3`, a persistent database
separate from the derived one, which records its schema version and migrates forward from it; the `admin` table
([2.11](#211-authentication-and-admins)) goes there too. Each job gets the next generation number, and the database
also records the generation of the last job that reached ready. No job survives a restart: at startup, a job recorded
as running is marked failed ("interrupted by restart"). The upload handler starts the job and streams the file within
the uploading stage; the remaining stages then run in the background (P-5).

The database is the job state's source of truth, so nothing here ties browl to one worker (P-5):

- **Reading:** the site gate and the status route read the state at most once a second per worker (a read costs about
  0.1 ms in the thread pool, on every request otherwise); a worker's own changes show in its responses at once.
- **Claiming:** a job starts by taking an exclusive `flock` on `DATA_DIR/job.lock`, without waiting, then recording the
  next generation in one SQLite transaction. The worker running the job holds the lock until the job's end is stored,
  and the OS frees it if the worker dies. So a second job is refused in any worker, and a job recorded as running while
  nobody holds the lock was interrupted: startup marks it failed only then, never a job another live worker runs.
- **Still needed for several workers:** the workers must share one host (`flock` on a local volume). A job whose worker
  dies, or whose end the database couldn't store, stays recorded as running: the other workers show "loading" until a
  worker starts (and marks it failed) or the next upload replaces it; in the second case its own worker shows its end.

| Stage | What happens |
| --- | --- |
| **uploading** | The file is streamed to the jobs volume, at `DATA_DIR/uploads/{generation}/ontology.{ttl,rdf,ofn}`, the suffix naming its format (an `.owl` file is taken to be RDF/XML); earlier uploads are deleted. A failed upload (e.g. the disk is full, or the client disconnects) deletes its partial file. An upload larger than `MAX_UPLOAD_BYTES`, or than the free space less a margin (from `Content-Length`), is refused before the job starts; one that receives nothing for a minute fails. The form's reasoner choice is stored with the job, as are the file's name, syntax and size, recorded as it grows so the status shows progress. The site switches to the "loading" page (U-16) and the page cache is cleared (P-6). |
| **imports** | Imported ontologies are fetched, and a catalog written for ROBOT (see Imports, below). An `.ofn` upload, which the TDB2 loader can't read (it reads only RDF formats), is converted to Turtle here, as `converted.ttl`, by the same OWLAPI parse that lists its imports; the Turtle stands in for it from then on. |
| **reasoning** (optional) | One ROBOT run (see Reasoning, below) classifies the upload with ELK or HermiT. Its output, `reasoned.ttl` beside the upload, replaces the uploaded file as the ontology that gets served; the upload is kept (P-7). (Later requirements U-39 and U-40 need the asserted axioms kept too; see [Later](#later).) It reasons over the upload, or over `converted.ttl` for an `.ofn` upload. Without reasoning, the upload, or `converted.ttl`, is loaded as it is. Of the RDF syntaxes ROBOT writes, Turtle and RDF/XML load equally fast, and Turtle is the smaller file, a quarter smaller on NCBITaxon (P-8, [findings](../research/P-8/README.md)). _(Q7a)_ |
| **loading** | Jena's `tdb2.tdbloader` builds a new store from the built ontology, in the default graph, and loads each fetched import into a named graph, in a new directory on the Fuseki volume. The store's triples are counted with `tdb2.tdbquery`; Fuseki is then switched to it, the triples counted again through Fuseki, which must match, and the previous store deleted. The counts are kept with the job, for the status and the home page. Uberon loads in about 5 s, NCBITaxon in about 65 s ([P-8 findings](../research/P-8/README.md)). _(Q15)_ |
| **pre-caching** | A few bulk SPARQL queries fetch the whole-ontology data, and the derived SQLite database is built from them ([2.5](#25-derived-data-pre-caching)), as a new file then swapped in atomically. |
| **ready** | The new generation number is recorded; pages are served again. |

Uberon goes from upload file to ready in 7 s without reasoning, measured end to end; NCBITaxon is estimated at about
5 minutes ([S-4 findings](../research/S-4/README.md)).

**Reasoning** (P-7) chains three ROBOT commands in one run, so the ontology is parsed once:

1. `reason --axiom-generators "SubClass EquivalentClass ClassAssertion"`, with ROBOT's default
   `--equivalent-classes-allowed all`, since browl shows equivalence groups (U-9). ClassAssertion gives individuals
   their inferred types. Without EquivalentClass, `reason` drops a subclass cycle's axioms as redundant, losing the
   classes' equivalence.
2. `materialize --term P` for each property in `MATERIALIZE_PROPERTIES` (none by default, which skips it): asserts the
   inferred `SubClassOf(C, P some D)`, e.g. from equivalence definitions.
3. `query --update remove-superclass-shortcuts.rq` (`browl/sparql/`) deletes shortcut `rdfs:subClassOf` links: `X ⊑ Z`
   where another parent `Y` of `X` already gives `Z` (`Y ⊑ Z`, `Y ⊑` the same `P some T` restriction, or for a
   transitive `P`, `Y = P some W` with `W ⊑ P some T`). It relies on `reason` having removed subclass cycles first,
   and skips a `W` from which `X` can be reached back, where shortcuts in a `P some` cycle would justify each other.

On Uberon with ELK and an 8 GB heap, `reason` alone took 17 s; with `materialize` of `part_of`, 24 s (it added nothing:
Uberon's release is already materialized); with the shortcut query too, 43–45 s, removing 6,175 of 80,598
`SubClassOf` axioms (without the cycle check, it wrongly removed 21 more). The query itself takes about 5 s of that (8 s
before its restriction cases became joins); converting the ontology to Jena's in-memory model and back takes about
20 s, and needs heap of its own.

**Reasoner limits** _(Q7)_, which cover the whole ROBOT run:

- ROBOT runs as `java -Xmx{REASONER_MAX_HEAP} -XX:+ExitOnOutOfMemoryError -jar robot.jar -vv reason …`, in its own
  process group. It is a single JVM, so there is no wrapper process to outlive it. The group is also stopped if the job
  is cancelled.
- Time: when `REASONER_TIMEOUT_S` is reached, the process group gets SIGTERM, then SIGKILL once ROBOT exits or after a
  few seconds, for anything left; the JVM exits on SIGTERM and leaves nothing behind. The job records "time limit".
- Disk: the output can be far larger than the upload (e.g. EquivalentClass and ClassAssertion axioms among many
  equivalent classes), so while ROBOT runs, browl checks the jobs volume's free space every second, and stops ROBOT
  once it's below the upload's margin (1 GB). The job records "disk space".
- Memory: `-XX:+ExitOnOutOfMemoryError` makes any thread's `OutOfMemoryError` end the JVM with status 3 and the line
  `Terminating due to java.lang.OutOfMemoryError`; the job records "memory limit". Without the flag, an ELK worker's
  breach shows only as a generic failure. A SIGKILL at no limit of browl's, as from the sandbox's container memory
  limit (`SANDBOX_MEM_LIMIT`), gives no such line; the job records "killed", most likely out of memory.
- Other failures are told apart by ROBOT's log lines, checked in this order: `StackOverflowError` ("stack overflow"),
  `The ontology is inconsistent` ("inconsistent ontology"), `There are N unsatisfiable classes` ("unsatisfiable
  classes", with the classes it lists), `INVALID ONTOLOGY FILE ERROR` ("parse error"); anything else is "reasoner
  failed" with ROBOT's last lines.
- ROBOT writes its output only after reasoning succeeds; browl still writes it to a temporary path and renames it.

Measured in S-2 ([findings](../research/S-2/README.md)): ELK classifies Uberon in 13 s within 1 GB, and NCBITaxon in
about 6.5 min, needing a 16 GB heap. HermiT can't classify Uberon: it ran out of 16 GB after 8 minutes (and HermiT
with individual reuse, the change in a pending ROBOT PR, hit a 1-hour limit), so the limits are what stop it.

On failure the job records the stage and reason (A-6), and the site stays on a "not available" page until an admin
uploads again. Downtime during a replace is accepted. _(Q14a)_ A timeout's reason names what timed out and the limit
(e.g. "Fuseki HTTP request POST /$/datasets timed out after 60 s"); a job cancelled at shutdown records "interrupted by
shutdown", any other cancellation "cancelled". A job that stops without recording why is still marked failed, so the
site never stays on "loading" (P-5).

**App restart:** the store and the derived database stay on the data volume. Fuseki reopens the store in under a
second, and the app reuses the derived database when its generation number matches, skipping pre-caching. Restarting
either container doesn't reload the ontology.

**Choosing the loader** (P-8): `tdb2.tdbloader` loaded NCBITaxon in 66 s in the app container (an 8 GB Docker VM, 2 GB
heap), against 150 s for `tdb2.xloader`, and Uberon in 5 against 11 s, so browl uses it for every load. Its store is
larger: 3.7 against 2.4 GB for NCBITaxon. P-24 checks that pages are as fast on it. It also loads into a named graph
(`--graph`), which `xloader` can't.

**Switching Fuseki to a new store** happens at the end of every loading stage, including the first. The TDB2 loader
can only write a new store, not one Fuseki has open, so Fuseki keeps serving the previous store (behind the "loading"
page) until the new one is built. The app then switches Fuseki over with **Fuseki's admin protocol** (`/$/datasets`).
The app and Fuseki are separate containers, so this avoids the app restarting Fuseki. Tried in P-3 with Fuseki 6.2.0:

- Fuseki 6 refuses a dataset description sent to the admin protocol, so a dataset is registered by name only
  (`dbName=gen-N`, `dbType=tdb2`), and Fuseki's template puts its store at `{FUSEKI_BASE}/databases/gen-N`. The loader
  therefore builds each new store at that path, on the Fuseki volume that the app also mounts; registering the name
  opens it.
- Removing the old dataset (`DELETE /$/datasets/gen-N`) also deletes its store. Once the new one is registered and its
  counts match, every other `gen-N` dataset is removed. Before a load, store directories that no registered dataset uses
  (left by a load that stopped) are deleted, and so is a dataset already registered by the new name, as one would be if
  the job state started again while Fuseki's volume was kept (P-8).
- Fuseki records registered datasets in its base directory, on the same volume, so the current one survives restarts.
- The admin endpoints are open to any client that reaches Fuseki (its default allows only localhost, and the app is
  another container), so Fuseki's port is not published outside the Compose network.

Fuseki didn't leak across 60 switches of a small store, each followed by queries: its container's memory levelled off
at 220–236 MiB after the first 10, with 55 open files and 38 threads at the end. Had it leaked, the fallback was a fixed
store path and a Fuseki restart, which takes under a second.

**Imports:** browl follows `owl:imports` where it can, and warns where it can't. The TDB2 loader doesn't follow
imports, and ROBOT would follow them itself, reading local files (`file:`) or sending requests from the app container,
so the pipeline resolves them itself (P-8). [LOAD-FLOW.md](LOAD-FLOW.md) charts the flow for each syntax, with and
without a reasoner, as revised in P-8's review (the OWLAPI lists OFN imports; ROBOT runs in a sandbox, P-30):

1. List every import with the parser that reads the document next, so the list holds what that parser will try to
   load. RDF goes through Jena's `riot`, which streams it as N-Triples (about 25 s for NCBITaxon's 2 GB, against 65 s
   to load it), from the whole file, keeping the objects of `owl:imports` triples; the loader is Jena too. OWL
   Functional Syntax is read only by the OWLAPI, so it lists the imports, in a separate process (`browl.ofn`) that
   refuses every document load but the file's own and, in the same parse, converts the file to Turtle, which stands in
   for it from then on. A relative import, which can't be fetched, is left out of that Turtle, whose base would
   resolve it to another IRI. A regex scan of OFN was replaced after P-8's review, which found crafted files that the
   OWLAPI read differently. RDF is still listed by `riot` when ROBOT will read it: a crafted RDF file can still differ,
   so ROBOT runs in a sandbox (P-30), and on its default, which fails the job on an import missing from the catalog,
   naming it. [LOAD-FLOW.md](LOAD-FLOW.md) charts this.
2. Fetch each import, recursively, into `imports/` beside the upload. Only `http` and `https` IRIs are fetched, never
   from loopback, private, link-local or other non-global addresses, nor from a port browsers block (the Fetch
   Standard's bad ports, such as mail's): the address is checked on each connection, so on each redirect too, and the
   connection made to the very address checked. Each fetch is limited to an hour; all of
   them together to `MAX_UPLOAD_BYTES` and two hours, and to 100 attempts, failed ones included. An import must be
   RDF/XML, Turtle or OWL Functional Syntax (an OFN import is converted to Turtle as it's listed). An import that can't
   be fetched or read, or isn't allowed, or is past a limit, is dropped, and its file deleted: its `owl:imports`
   declaration stays in the ontology as written, but nothing is loaded for it. A load whose documents list more than
   1000 distinct imports fails, since each needs a catalog entry and no real ontology has that many.
   An import of the ontology's own IRI (or version IRI, for OFN), by a cycle back to it, isn't fetched: its published
   version may differ from the upload, which ROBOT already holds. For RDF, the ontology's IRIs are taken to be those
   of the subjects of its `owl:imports` triples, as only an ontology that imports something can be in a cycle.
   A relative import in RDF resolves against the file it's in, to a `file:` IRI, so it's dropped; in a fetched import
   too, not resolved against the URL fetched, since ROBOT reads the import from the file the catalog names and
   resolves it the same way.
3. Write a catalog file mapping every import listed: to its fetched file, or to an empty ontology if it was dropped.
   An IRI with a character XML can't hold, such as a control character, is left out (it can't be fetched either), so
   it fails only a reasoning run that reaches it, not every load. ROBOT's catalog accepts only exact names, so for a
   file IRI it lists every spelling the OWLAPI may request: `file:/…`, `file:///…` and `file://localhost/…` (the
   OWLAPI and Jena resolve a relative RDF import against the document differently). ROBOT's JVM also sends every
   HTTP(S) request to a closed port, defense in depth behind the sandbox, and every tool gets only a few of the
   environment variables of the app or the sandbox (`browl.process.ENVIRONMENT`).
4. The loading stage loads the ontology into the store's default graph, and each fetched import into its own named
   graph, named by the IRI it's imported by. Each triple's source stays known, e.g. to mark imported entities; merging
   into one file first (ROBOT `merge`) would lose that.
5. When the ontology has imports, queries that should see them name a dataset that merges every graph
   (`browl.fuseki.UNION_FROM`): `FROM <urn:x-arq:DefaultGraph> FROM <urn:x-arq:UnionGraph>`. The merge is a set, so a
   triple that the ontology and an import both state counts once. Without imports, queries have no `FROM` clause.

The job state keeps every import, whether it was loaded (with its triple count) or dropped (with why), and who imports
it. The status page lists them, and says in red how many were dropped; the ontology's home page warns the same way
(P-17). Neither Jena nor the OWLAPI expands an RDF/XML upload's external XML entities (each reads one as empty), so a
`DOCTYPE` can't read local files either.

**The sandbox** (P-30): ROBOT and the OFN lister (`browl.ofn`) run in the `sandbox` container, so an import that gets
past the listing reaches no network and nothing worth reading; the job fails, naming it.

- The app connects to `browl.sandbox` on a Unix socket in a shared volume (mode 0600; both run as the image's user, uid
  10001), sends the command, and reads its output lines and exit status. The time limit, the free-space watch and
  cancellation stay in the app: it closes the connection, and the runner stops the process group. That stop is best
  effort: if the runner's status doesn't come within 10 s, the app logs a warning and the stage goes on. The app never
  gets the Docker socket. Without `SANDBOX_SOCKET` (tests, development), the tools run in the app's container.
- The container is the app's image with no network (`network_mode: none`, so no DNS either), a read-only file system
  with a 256 MB tmpfs `/tmp`, no capabilities, `no-new-privileges`, caps on processes and open files, the image's user
  set explicitly, and only the image's environment (no `.env`). Of the app's volumes it mounts only the socket's and the
  jobs volume, the latter at `DATA_DIR/uploads` as in the app, so the paths the app sends hold there too. Each upload
  removes the earlier jobs' files, so it holds only the current job's: the upload, fetched imports, catalog and outputs.
  Not the data volume (job database, admins, derived database) nor Fuseki's.
- Checked in CI's Compose job (`docker/sandbox-check.py`): an import missing from the catalog, by `http:` from the
  app's container and by `file:` of the job database, fails the run naming it, with no request received and the file
  not found, with or without the JVM's no-network options. Run in the app's container instead, ROBOT read the file,
  and without those options fetched the URL.
- A container rather than Landlock in the app's container, which would be lighter: Landlock is unverified on Docker
  Desktop's kernel and under Docker's default seccomp profile, and Linux-only, while a container gives the same
  isolation wherever Compose runs.

**Why the ontology goes in the default graph** (measured in P-3, NCBITaxon loaded with `tdb2.xloader` in the app
container, Fuseki with a 2 GB heap): TDB2 indexes named graphs separately, so a named graph costs about 2.2× in store
size and load time (5.2 GB and 358 s, against 2.4 GB and 165 s in the default graph); page queries ran at the same speed
on both. Keeping only the imports in named graphs means an ontology without imports pays nothing. Adding Uberon as an
import (11 s; the store grew to 3.2 GB), the `FROM` merge cost about 1 ms per page at one client (median 4.0 against
2.8 ms) and nothing measurable at 16 (682 against 641 pages a second). An explicit `{ … } UNION { GRAPH ?g { … } }`
was no faster, and returns a triple once per graph that states it. Fuseki's alternative, `tdb2:unionDefaultGraph`,
would put every triple in a named graph and hide the default graph.

**Heap sizes:** the app's JVM only renders per-page fragments, so it needs a small heap (`APP_JVM_MAX_HEAP`). After a
heap failure the app restarts its process instead of carrying on, because the error can also kill JPype's internal
reference-cleanup thread ([S-1 findings](../research/S-1/README.md)): the JVM runs with `-XX:+ExitOnOutOfMemoryError`,
which ends the process (exit code 3), and Compose restarts the container. The default of 1 GB was confirmed in P-4
([P-4 findings](../research/P-4/README.md)): through browl's bridge, all of Uberon as N-Triples (1.2 M triples, 391 k
axioms) parsed at 384 MB but not 256 MB, and a page's fragment is at most a few thousand triples. A heap failure
would recur on every retry of the same page, so the Fuseki client refuses a fragment larger than 64 MiB before the
OWLAPI sees it (`CONSTRUCT_MAX_BYTES`), and any result with a line longer than 16 Mi characters.

Fuseki's heap defaults to 1 GB (`FUSEKI_HEAP` in `.env`, read by `docker-compose.yml`). Measured in P-3 on NCBITaxon,
with the bulk pre-caching queries ([2.5](#25-derived-data-pre-caching)) and then page queries from 1 and 16 clients:
the heap held 430–490 MiB after garbage collection at its peak, during the bulk name-dictionary query, so 1 GB leaves
about 2× headroom. 512 MB was too tight (constant collection; median page time at 16 clients rose from about 22 to
92 ms), and at 256 MB the dictionary query ran out of memory. Page throughput didn't differ measurably between 1, 2 and
4 GB; runs varied more with how much of the store the OS had cached. TDB2's store is memory-mapped, so memory not given
to the heap caches the store. Raise the heap for ontologies larger than NCBITaxon or with large imports, whose bulk
queries go through the `FROM` merge (not measured). On `OutOfMemoryError` Fuseki exits and Compose restarts it; without
that, it hung after one and answered nothing.

**Fuseki's container memory** is the heap plus about 0.5 GiB: plan 1.5 GiB for it at the default heap
([S-6 findings](../research/S-6/README.md)). Fuseki 6.2.0 sets Jetty's HTTP output buffer to 5 MB, which Jetty's
buffer pool doesn't keep, so every response allocated a new 5 MB direct buffer that only garbage collection freed
(Jena's [GH-4084](https://github.com/apache/jena/issues/4084), fixed after 6.2.0). Under page load the JDK called
`System.gc()` about once per 1 GB of buffers, glibc kept much of the freed memory, and the container grew to 2.6 GiB
of anonymous memory (3.9 GiB in `docker stats`, with the store's cached pages). Capping the direct buffers alone cost
up to three quarters of the page throughput. So the Fuseki image starts it with a `jetty.xml` (`docker/fuseki/jetty.xml`)
that does what Jena's fix does: a 2 MB output buffer, from a buffer pool on the connector that keeps buffers that large
(at most 64 MB of them). Otherwise it sets up the server as Fuseki does (port, header sizes, thread pool of 2–20, error
handler). Jetty's default 32 KB buffer, unpooled, measured about the same, but a larger buffer keeps a real error status
for more of the responses that fail partway ([2.14](#214-risks)). Direct buffers are capped at 128 MB
(`-XX:MaxDirectMemorySize=128m`), above the pool's 64 MB plus 20 requests' buffers in use; NMT measured 14–21 MiB.
Should the cap be reached, the request fails with `OutOfMemoryError: Cannot reserve … direct buffer memory`, which
`-XX:+ExitOnOutOfMemoryError` doesn't catch (Java code throws it, not the JVM), so Fuseki keeps running: S-6 saw this
with a 64 MB cap and Fuseki's own buffers. On NCBITaxon,
Fuseki's anonymous memory then stays under 450 MiB under 16-client load and peaks at about 1 GiB during the bulk
queries (the heap at 650–790 MiB committed, plus about 140 MiB of JVM overhead and pooled buffers). Pages are faster
than with Fuseki's own setup: 528 and 769 a second at 1 and 16 clients, against 302 and 573. Drop the `jetty.xml` once
browl moves to a Jena release with the fix, and remeasure.

`docker stats` counts the store's pages the kernel has cached for Fuseki, which it gives back when anything else needs
the memory. Without a limit that cache fills spare memory (about 1.6 GiB in `docker stats` under 16-client load on
NCBITaxon), so judge Fuseki's needs by the cgroup's anonymous memory, not by `docker stats`. A memory limit doesn't
lower what Fuseki needs; it contains a runaway Fuseki (a heap set too large, a leak), which is then killed and
restarted instead of squeezing the app or the host. So the limit is optional: `FUSEKI_MEM_LIMIT` in `.env`, read by
`docker-compose.yml`, with none by default, since a fixed one wouldn't follow `FUSEKI_HEAP`. Set it to the heap plus
0.5 GiB, and raise it with the heap. At 1.5 GiB, and even at 1 GiB, nothing was killed under 16-client load and the
bulk queries; the kernel evicted the store's cached pages instead, and the bulk queries slowed from 40 s to 45–50 s.
Below 1,792 MiB the JVM picks SerialGC instead of G1, which made no difference here. A 768 MB heap passed the bulk
queries but saved nothing.

### 2.5 Derived data (pre-caching)

Built once per load from bulk SPARQL queries, one per kind of data, and stored in the derived SQLite database. Bulk
results stream from Fuseki at about 100 K rows per second whatever the query looks like, so pre-caching NCBITaxon takes
about a minute ([S-4 findings](../research/S-4/README.md)). Proposed tables:

| Table | Contents |
| --- | --- |
| `entity` | IRI, entity type, URL path, fragment, display name, CURIE. From an entity-list query (every declared entity with its type), not yet measured. |
| `name_value` | entity, annotation property, value, normalized value, language, `direct` flag (one row per direct and per RDFS-entailed property). From the dictionary query: 41–48 s on NCBITaxon, 3.56 M rows. |
| `edge` | parent → child per hierarchy (classes; each property type), between equivalence-group representatives. From the edges query: 18–19 s on NCBITaxon. |
| `equiv_group` | entity → group ID (union-find over `owl:equivalentClass` between named classes) |
| `counts` | per node: number of immediate children, number of distinct descendants |
| `hash_prefix` | distinct URL paths that have hash entities under them |
| `prefix` | CURIE prefix → namespace (ontology's own, built-ins, configured overrides) |
| `meta` | ontology IRI, version IRI, reasoner used, load time, generation number |

An earlier design also had a `usage` table, because the OWLAPI can't find annotation assertions by value. Fuseki can
(`?s ?p <entity>`), so P-27 decides whether any part of it is still needed ([2.8](#28-entity-pages)).

**Hierarchies.** Named direct parents: `rdfs:subClassOf` and `rdfs:subPropertyOf` triples with an IRI on both sides
(asserted, or inferred if a reasoner ran). Roots are nodes with no named parent. For classes this includes children of `owl:Thing`.

**Distinct descendant counts.** These need a transitive closure, not a sum over edges. _(Q19a)_ The plan is to compute
them in a bottom-up pass in topological order, merging descendant sets. Each set is a
[roaring bitmap](https://roaringbitmap.org/) (a compressed integer set with fast unions, via `pyroaring`), freed once
all its parents have merged it. Only the counts are stored, not the full closure. On NCBITaxon (3.0 M classes) this
takes 4.2 s ([S-3 findings](../research/S-3/README.md)). Python `int` bitsets are unsuitable: their size grows with the
highest index.

**RDFS entailment** _(Q7)_. browl needs only a few RDFS entailment rules:

| Rule | What browl does with it |
| --- | --- |
| `rdfs:subPropertyOf` is transitive (rdfs5), and a value of a sub-property is also a value of its super-properties (rdfs7) | **Entailed name values:** for each property in the configured name list, values of its sub-properties are also stored under it, with `direct = false`. Jena computes them (below). _(Q8)_ |
| `rdfs:subClassOf` is transitive (rdfs11) | Covered by the descendant counts, computed in Python (above). |
| An instance of a class is an instance of its super-classes (rdfs9) | For the Individuals tab (Later, U-18), if it lists indirect members. |

Jena computes the sub-property closure in one of two ways; P-11 picks one before building the dictionary:

- a property path in the dictionary query (`?p rdfs:subPropertyOf* ?name`); S-4 measured it at little extra cost;
- an RDFS endpoint configured in Fuseki (`ja:DatasetRDFS`) over the same store; not tried yet. The plain endpoint
  stays too: under RDFS inference, direct subclass edges can't be told apart from entailed ones.

**Subclass closure, not stored.** Queries over `rdfs:subClassOf+` take up to 0.9 s on NCBITaxon, so pages never use them:
descendant counts are precomputed (above). If a later feature needs to list or count descendants on demand, the fallback
is to load the subclass closure as plain triples: 7–21 ms per query, but for NCBITaxon 59 M extra triples and about 12
more minutes of loading ([S-4 findings](../research/S-4/README.md)).

### 2.6 Search

A `SearchBackend` interface with methods for indexing during pre-caching and for queries. Only its module knows the
engine. _(Q11, N-5)_

**v1 implementation:** the `name_value` table in SQLite with an index on `normalized value`. A query is a range scan
(`value >= q AND value < q + '￿'`) filtered by entity type, property and `direct`. _(Q8)_

**Ranking** _(Q9, Q10)_, in order:

1. Exact match before prefix match.
2. Position of the property in the configured name list.
3. Position of the language in the language ranking. Untagged values come last by default (O-5).
4. Shorter value.
5. Alphabetical.

Results are grouped per entity: an entity appears once, with its best-matching value shown.

**Normalization:** Unicode NFKD, accents removed, case folded. The same function is used at index and query time.

**Later:** FTS5 for any-word matching (U-19); an external engine such as Typesense for typo tolerance (U-20), swapped in
behind the same interface.

### 2.7 Display names

One ordered list of name annotation properties (default `rdfs:label` first) drives both display names and search
ranking. _(Q18, Q18a)_ For each entity, the display name is the first value found by:

1. property position in the list,
2. then language ranking,
3. then shortest value,
4. then alphabetical,
5. with the CURIE as the fallback, then the full IRI.

It is computed during pre-caching, so link text costs nothing at render time.

**Language matching** (wherever tags are compared, including search ranking, [2.6](#26-search)):

- **Case:** RDF compares language tags case-insensitively, and `LANGUAGE_RANKING` is stored lowercased (P-2). So a
  literal's tag is lowercased before it's looked up (`@en-GB` matches `en-gb`).
- **Fallback** (RFC 4647 "lookup"): a tag with no entry of its own drops subtags from the end until one matches, so
  `@en-GB` ranks with `en` unless `en-gb` is listed; `@zh-Hant-TW` tries `zh-hant`, then `zh`.
- **Unmatched tags** rank after every listed language: just before `none` when it's last (the default), last when
  `none` is placed earlier.
- **Untagged** literals match the `none` entry.

**CURIE prefixes:** the prefixes declared in the ontology file, plus a built-in set (`rdf`, `rdfs`, `owl`, `xsd`,
`skos`, `obo`, `dc`, `dcterms`, …), with configured mappings overriding both (O-11). An IRI with no matching prefix has
no CURIE. _(Q18b)_

### 2.8 Entity pages

Header, above the tabs (U-5):

- Display name as the title, and the entity type.
- A `<details>` element with the full IRI and CURIE.

Tabs (U-28; see [2.9](#29-page-layout)):

- **Overview** (default; U-3, U-9, U-30–U-32): two panels, stacked, inspired by Protégé's description views but more
  compact. Details below.
- **Usage** (U-26), labeled with its count, e.g. "Usage (128)": referring entities a page at a time (e.g. 50), each
  with its axioms that mention this entity, rendered on demand. References come from Fuseki (`?s ?p <entity>`), paged
  in the store's index order, since sorting doesn't scale (15.5 s for 2.5 M references). Counting takes 2 ms for most
  entities, up to 0.4 s for one referenced millions of times. The tab isn't the default, so its cost is paid only when
  it's opened. P-27 first settles two questions: finding references inside class expressions, which reach the entity
  through blank nodes, and counting on first view (cached) or up front ([S-4 findings](../research/S-4/README.md)).
- **Individuals** (classes only; Later, U-18): the class's named individuals, paged.

**Overview tab:**

```text
 Annotations
   label            Heart
   exact synonym    cardiac muscle organ                  @en
                    heart organ                           @en
   definition       A hollow, muscular organ which, by contracting
                    rhythmically, keeps up the circulation of the blood.

 Logical axioms
   SubClass Of      Organ
                    part_of some 'circulatory system'
   Equivalent To    …
   Disjoint With    Lung
```

- **Annotations panel:** from the entity's own triples, grouped by annotation property, with the property's display
  name shown once and one line per value. Language tags are shown small and muted. IRI values are links. Long values,
  such as definitions, wrap within the readable text width ([2.9](#29-page-layout)). Property order: the configured
  name properties first, in their configured order, then the others alphabetically.
- **Logical-axioms panel:** grouped by axiom kind under familiar section names (SubClass Of, Equivalent To, …), one
  Manchester-syntax axiom per line. Empty groups are omitted.
  - **Classes:** SubClass Of (named parents first, then class expressions), Equivalent To (equivalent classes from the
    derived database, then expressions), Disjoint With, and General class axioms that mention the class.
  - **Properties** (U-31): Domains, Ranges, Characteristics (functional, transitive, …), SubProperty Of, Equivalent To,
    Inverse Of, Disjoint With, and property chains.
- **Compact layout** (U-32): label column and value column in a two-column grid; tight line spacing within a group and a
  little more between groups. Multi-column values for long lists are Later (U-33).

**Manchester rendering** _(post-log; supersedes Q6's hand-written renderer)_: the OWLAPI's own Manchester syntax
renderer, the one Protégé uses, covers all of OWL 2. It renders from a fragment of the ontology fetched for each page
([S-4 findings](../research/S-4/README.md)):

1. Fetch the entity's triples with their blank-node closure (`DESCRIBE`), the triples where it is the object of
   `owl:disjointWith` or `owl:equivalentClass`, and the types of every IRI they mention.
2. The OWLAPI parses that fragment as a small ontology. For 300 of 300 random Uberon classes, the logical axioms
   rendered from it were identical to those rendered from the full ontology. Not yet covered: n-ary
   `owl:AllDisjointClasses` axioms and general class axioms that mention the entity, which need one more lookup (P-15).
3. Fetching takes 4.8 ms at p50 and 6.4 ms at p95, parsing and rendering 2.0 and 3.5 ms: about 7–10 ms per page,
   against the N-3 target of 300 ms.

Entity names become links like this:

1. The renderer is given the OWLAPI's `DefaultPrefixManager` with no prefixes as its short-form provider, so every
   entity is rendered as its full IRI, `<…>`.
2. Python HTML-escapes the text, then replaces each `<IRI>` that is a declared entity with a link showing the entity's
   display name ([2.7](#27-display-names)). IRIs that aren't entities, and `<…>` inside literals, stay as text.
3. Manchester keywords are wrapped for light styling ([2.9](#29-page-layout)).
4. An axiom that renders as an empty string (OWLAPI 4 does this for `DatatypeDefinition`) is shown in functional
   syntax instead.

No Java code is needed, and no Python code runs inside the renderer. Spike S-1 also tried the earlier plan, a Python
short-form provider returning placeholder tokens: it worked but was slower, and missed IRIs the renderer prints without
asking the provider ([S-1 findings](../research/S-1/README.md)).

**Base directions** (RDF 1.2's `"…"@ar--rtl`): Fuseki stores and returns them, and browl's Fuseki client reads them as
`rdf:dirLangString` literals. The OWLAPI 4.5 predates RDF 1.2: its N-Triples parser accepts them but reads `ar--rtl`
as the whole language tag, so the Manchester rendering shows such a literal as written (P-4).

### 2.9 Page layout

Every page has the same frame (U-27, U-28, U-29):

```text
┌──────────────────────────────────────────────────────────────────────┐
│ browl · <ontology name>   [ Search… ]   Classes  Object props        │  navigation bar
│                                         Data props  Annotation props │
├──────────────────────────────────────────────────────────────────────┤
│  Heart                                            Class              │  page header
│  ▸ IRI                                                               │
│  ──────────────────────────────────────────────                      │
│  Overview   Usage (128)                                              │  page tabs
│  ═════════                                                           │
│  …tab content…                                                       │
└──────────────────────────────────────────────────────────────────────┘
```

**Navigation bar** (U-27): full width, on every page, stays at the top when scrolling.

- Contents: the ontology name (a link to the home page), the autocomplete search box, and links to the four tree pages.
- The full search page with type and property filters (U-10) is reached from the search box, e.g. "All results…".
- An admin link appears only for signed-in admins. Sign-in itself is a small link in the page footer.
- On narrow screens, the tree links collapse into a menu.

**Page tabs** (U-28): inside the content area, below the page header.

- Styled as underlined text tabs, not as a second bar, so they don't look like part of the navigation. The navigation
  bar has its own tinted background; the tabs sit on the page background.
- Each tab has its own URL (`?tab=usage`). Without JS the tabs are ordinary links and the server renders the chosen tab.
  With htmx, only the tab panel is fetched and swapped, and the URL updated. Only the visible tab's content is fetched.
- They follow the WAI-ARIA tabs pattern.
- For hash IRIs, the query string comes before the fragment (`/example.org/onto?tab=usage#Foo`). The hash-IRI script
  keeps it when loading the entity.
- The page cache key includes the tab.

**Visual design** (U-29):

- Readable text measure: prose such as annotation values is capped at roughly 70–80 characters per line. Trees, tables
  and Manchester axioms may use the full width.
- Soft contrast: dark gray text on an off-white background rather than pure black on pure white, while meeting WCAG AA
  contrast. Muted accent colors.
- A system font stack for text and a monospace font for IRIs and CURIEs. Manchester keywords (`some`, `and`,
  `SubClassOf`) are styled lightly to set them apart from entity names.
- A dark theme that follows the browser setting (`prefers-color-scheme`).
- Generous but not wasteful spacing; works from phone width up.
- Plain CSS with custom properties for the colors, so the palette can be tuned in one place. No CSS framework.

### 2.10 Page cache

An in-memory LRU of rendered HTML, keyed by `(generation, URL path, tab)`, with a configurable size (default 100).
_(Q13)_ Including the generation number in the key means a reload invalidates entries in every worker without
coordination. The cache wraps a plain function that stores rendered strings. `functools.lru_cache` is not applied to
`async` handlers, because it would store unrun coroutine objects rather than pages.

### 2.11 Authentication and admins

- Google sign-in via OpenID Connect (Authlib); the session is a signed cookie. Only routes under
  `/{RESERVED_PREFIX}/admin` require it. _(Q16)_
- An admin is a signed-in user whose verified email is in the seed list (from configuration) or in the `admin` table in
  a persistent SQLite file. Seed admins can't be removed in the UI. Whole-domain rules are not supported. _(Q16a)_
- `AUTH_DISABLED=true` turns authentication off for development. It logs a prominent warning at startup and is never the
  default. _(Q16)_

### 2.12 Configuration

Defaults are set in code, and each can be overridden by an environment variable named as below (uppercase only),
validated at startup (`browl/settings.py`). Lists and mappings are given as JSON, e.g.
`LANGUAGE_RANKING='["en", "fr", "none"]'`. _(Q13)_

| Setting | Default | Notes |
| --- | --- | --- |
| `BASE_URL` | none (required) | O-2 |
| `RESERVED_PREFIX` | `_browl` | must start with `_`; O-3 |
| `NAME_PROPERTIES` | `[rdfs:label]` | ordered; O-4 |
| `LANGUAGE_RANKING` | `[en]` | Untagged is the reserved entry `none`, added at the end if not listed; O-5 |
| `EXTRA_PREFIXES` | `{}` | O-11 |
| `PAGE_CACHE_SIZE` | `100` | `0` turns the cache off; O-10 |
| `REASONER_TIMEOUT_S`, `REASONER_MAX_HEAP` | `3600`, `8g` (S-2) | O-8; ELK needs 1 GB for Uberon and 16 GB for NCBITaxon, so set `16g` for ontologies of that size |
| `MATERIALIZE_PROPERTIES` | `[]` | object property IRIs, or CURIEs with ROBOT's default prefixes (e.g. `obo:BFO_0000050`), for which reasoning materializes `SubClassOf(C, P some D)` (2.4) |
| `APP_JVM_MAX_HEAP` | `1g`, confirmed in P-4 (2.4) | heap for the app's in-process JVM, which only renders per-page fragments (S-4) |
| `FUSEKI_URL` | `http://fuseki:3030` | Fuseki's address on the Compose network |
| `ROBOT_JAR` | `/opt/robot/robot.jar`, where the app image installs it | used both on the app's classpath and for reasoning subprocesses. With `SANDBOX_SOCKET`, the sandbox runs the command the app builds, so this path, and `JAVA_HOME`'s Java, must exist in the sandbox's image too; the app's image has both |
| `JENA_HOME` | `/opt/jena`, where the app image installs it | Jena's command-line tools: the TDB2 loader, and `riot` to list imports |
| `FUSEKI_BASE` | `/fuseki`, where `docker-compose.yml` mounts Fuseki's volume in the app | the loader builds each store under its `databases/`, where Fuseki opens it (2.4) |
| `DATA_DIR` | `/data` | mounted volume; O-9 |
| `MAX_UPLOAD_BYTES` | `20000000000` (20 GB) | the largest upload accepted; A-2 |
| `SANDBOX_SOCKET` | none; the Compose deployment sets it | the tool sandbox's Unix socket (`browl.sandbox`), where ROBOT and the OFN lister run (P-30); unset, they run in the app container, and startup logs a warning |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `SEED_ADMINS` | none | required unless `AUTH_DISABLED`; O-6 |
| `AUTH_DISABLED` | `false` | O-7 |

**The Compose deployment** (`docker-compose.yml`): three services, `app`, `fuseki` and `sandbox` (2.4), and four named
volumes: `data` (`DATA_DIR`: job state, admins, derived database), `jobs` (`DATA_DIR/uploads`: the current job's files,
shared with the sandbox), `fuseki` (Fuseki's base, shared with the app's loader) and `sandbox` (the runner's socket).
Compose reads a few variables of its own from `.env`, not browl settings: `BROWL_PORT`, the app's host port;
`FUSEKI_HEAP` and `FUSEKI_MEM_LIMIT` (2.4); and `SANDBOX_MEM_LIMIT`, the sandbox's memory limit, none by default, which
must hold `REASONER_MAX_HEAP` plus about 0.5 GiB. Upgrading a stack from before P-30 leaves its earlier uploads on the
`data` volume, hidden under the `jobs` mount; remove them with `docker run --rm -v browl_data:/old browl-app rm -rf
/old/uploads` (for the Compose project `browl`).

### 2.13 URL space for the service's own pages

Everything under `/{RESERVED_PREFIX}/` (e.g. `/_browl/`):

| Path | Purpose |
| --- | --- |
| `search` | Autocomplete endpoint (htmx fragment) and a full results page |
| `tree/{hierarchy}` | Tree root page; `?node=` returns one node's children (htmx fragment) |
| `iri?u=…` | Fallback entity URLs (Q1b) |
| `static/…` | CSS, htmx, hash-IRI script |
| `admin/…` | Upload, status, admin list, sign-in/out |
| `admin/upload` | The upload form (A-2, A-3) and the load job's status. The form is parsed as it streams in, the file written straight to the jobs volume, never held whole in memory or spooled to a temporary file; its reasoner field must come before its file. The job is claimed only once the file's name has been checked and its first 8 KiB found to look like the syntax the name says, so a malformed request or a mislabelled, OWL/XML or compressed file leaves the served ontology alone. A post a browser sent from another site's page (by `Sec-Fetch-Site`) is refused, against CSRF (P-6) |
| `admin/status` | The load job's stage, start time, uploaded file (name, syntax, size), and failed stage and reason (A-5, A-6): an HTML fragment for htmx requests (`HX-Request`), which polls itself while a job runs, else a page, which polls the same way, or refreshes itself while a job runs if JS is off (P-5) |
| `admin/docs`, `admin/openapi.json` | FastAPI's API docs (Swagger UI only, no ReDoc), as admin routes, so admin sign-in covers them; moved from FastAPI's default root paths, where they would shadow ontology entities. Swagger UI loads its assets from a CDN at an exact pinned version: an accepted exception to the no-CDN rule ([2.2](#22-libraries-and-components)), for admin pages only (P-5) |
| `health` | Liveness check for the container; also reports whether Fuseki answers |

While a load job runs, every other URL (the ontology's pages, the other routes above, and paths nothing serves) gets
the "loading" page; when no ontology is loaded, or the last load failed, it gets the "not available" page (U-16).
`health`, `static/…` and `admin/…` are exempt. Both pages are 503 Service Unavailable with `Cache-Control: no-store`,
so crawlers and caches treat the outage as temporary and keep the URLs. The loading page adds `Retry-After` and
refreshes itself at the same interval.
Neither page says why the site is unavailable; the failure reason is shown only on the admin status (P-5).

Pages link to the service's own routes by root-relative paths, never by URLs built from the request's scheme and
`Host`, which a proxy may change and a client may forge (P-5).

### 2.14 Risks

| Risk | Mitigation |
| --- | --- |
| Fuseki needs too much memory while serving the largest ontologies | Measured by P-3 and S-6: NCBITaxon needs about 500 MiB of heap; the default is 1 GB, and the container needs the heap plus about 0.5 GiB once Fuseki's output buffers are pooled through a `jetty.xml` (2.4). |
| A Fuseki response that fails partway returns a truncated result with HTTP 200 | Seen in P-3 and S-6: once Fuseki has sent the status, it appends the error as plain text and ends the response normally. It sends it as soon as a SPARQL JSON or TSV result is cut short (the writer flushes what it has), and once any other response outgrows the 2 MB output buffer (2.4). So browl treats any status other than 2xx as a failed query, and never discards the HTTP client's error for a body that ends early (Fuseki exiting or restarting mid-response drops the connection before the last chunk). It parses every Fuseki response strictly, as SPARQL JSON results or N-Triples, and parses each TSV cell as an RDF term, so the appended error text can't pass as a one-column row. N-Triples results may hold only triples, since Jena writes no comments or blank lines and the appended text can start with `#`, which N-Triples reads as a comment (P-4); a parse error is a failed query, never a short result. P-9, which runs the first bulk queries, checks that each bulk result is complete (e.g. a final count or sentinel), which also catches a result that breaks off cleanly at a row boundary, or an error with no text appended. The loading stage already checks that Fuseki serves as many triples as the store holds (P-8). |
| Fuseki pauses for full garbage collections under page load | Resolved by S-6: the `System.gc()` calls P-3 saw came from Fuseki 6.2.0 allocating an unpooled 5 MB direct output buffer per response (Jena GH-4084). Fuseki's `jetty.xml` pools a 2 MB buffer instead, which removed them; pages went from 302 to 528 a second at 1 client, and from 573 to 769 at 16 (2.4). |
| JPype quirks: thread safety of concurrent OWLAPI calls; Python–Java call overhead | Resolved by S-1: concurrent calls were safe but gave no extra throughput, so OWLAPI calls run on one thread (2.1). Call overhead is small ([S-1 findings](../research/S-1/README.md)). P-4 found three more, each handled: the JVM must start on the main thread, its signal handlers bypassed Uvicorn's graceful shutdown (`-Xrs`), and the OWLAPI fetched a fragment's `owl:imports` from their IRIs, web or `file:`, so the bridge refuses every document load but the fragment's own (2.1). |
| An upload's `owl:imports` make ROBOT read local files (`file:`) or send requests from the app container (`http:`), and its errors can quote what it read | Found in P-7's review; narrowed by P-8: browl lists every import, fetches only `http` and `https` from public addresses, and gives ROBOT a catalog covering every import (2.4). Tested with a `file:` import and one at a local address. P-8's review showed that a crafted file can get an import past the listing; P-30 runs ROBOT and the OFN lister in a sandbox with no network and nothing worth reading, so such an import fails the job (2.4, [load flow](LOAD-FLOW.md)), checked in CI. Not deployed before P-22 protects the upload. |
| Code run by a tool in the sandbox (through a JVM or OWLAPI flaw) acts as the runner's user | Accepted residual risk of P-30, beyond its threat (an import browl didn't list): the tools run as the runner's uid, so such code could replace the socket, kill the runner, or leave its process group, and forge later runs' output and status. It still has no network, none of the app's settings or volumes but the jobs volume, and the app loads only regular files on that volume. Running the tools under a second uid would close it. |
| Memory limit can't be reported reliably | Resolved by S-2: with `-XX:+ExitOnOutOfMemoryError`, a heap breach in any thread exits with status 3 and a fixed message; each other failure kind has its own ROBOT log line (2.4). |
| Descendant counts too slow or memory-hungry at millions of classes | Resolved by S-3: roaring bitmaps, 4.2 s for NCBITaxon ([S-3 findings](../research/S-3/README.md)). |
| Loading a very large ontology takes minutes | Accepted for uploads (Q14a), shown on the status page. Restarts don't reload (2.4). NCBITaxon: about 5 minutes, estimated; Uberon: 7 s, measured ([S-4 findings](../research/S-4/README.md)). P-24 checks whether extracting the dictionary and edges by streaming the upload file, in parallel with the TDB2 load, is worth it. |
| Fragment rendering misses axioms a full-ontology render would show | 300 of 300 Uberon classes matched (S-4); P-15 adds n-ary disjointness and general class axioms, and tests against full-ontology rendering. |
| Usage misses references inside class expressions | P-27 tests walking from the blank node up to the owning axiom before building the tab. |

---

## 3. Implementation Plan

Work items are sized to be one issue or PR each, roughly. **Delivers** lists the requirement IDs. **Status** is To do,
In progress or Done. **Tracking** links each item's GitHub issue, or the PRs that did it. Each issue states its goal,
entry criteria and acceptance criteria; GitHub's "blocked by" links and sub-issues mirror **Depends on**, and
milestones mirror the phases.

### Phase 0: Spikes

Spikes are time-boxed investigations. Their code lives in `research/S-n/`, outside the `browl` package; their findings
go into this document.

| ID | Work item | Delivers | Depends on | Status | Tracking |
| --- | --- | --- | --- | --- | --- |
| S-1 | OWLAPI via JPype: start the JVM with `robot.jar`, load an ontology, render axioms in Manchester syntax with link tokens, list referencing axioms; measure per-entity render time, including concurrent requests. Briefly compare py-horned-owl's load time, memory and Manchester output. | de-risks U-3, U-26, N-3 | | Done | [#8](https://github.com/jclerman/browl/issues/8) |
| S-2 | ROBOT reasoning with time and heap limits: confirm a memory breach can be detected and told apart from other failures | de-risks A-6, O-8 | | Done | [#12](https://github.com/jclerman/browl/issues/12) |
| S-3 | Scale test with a large ontology (e.g. NCBITaxon or ChEBI): OWLAPI load time and heap; descendant-count computation time and memory; `usage` table build time and size; derived database size | de-risks N-1–N-4 | S-1 | Done | [#13](https://github.com/jclerman/browl/issues/13) |
| S-4 | Serving without an in-process OWLAPI: Fuseki/TDB2 queried on demand (aggregates fetched up front with single SPARQL queries), or pre-rendered SQLite. Measure bulk load, restart readiness, page-query latency, Manchester rendering. | de-risks N-1–N-4, U-16 | S-3 | Done | [#47](https://github.com/jclerman/browl/issues/47) |
| S-5 | Compare browl with OLS4 on Uberon and NCBITaxon: load time, store size, memory, page speed, and how each shows an entity's usage and relation trees. S-5a: best effort now, browl's figures as lower bounds; S-5b: like for like once browl renders pages. | informs positioning and the Later requirements U-34–U-40 | P-3 | In progress (S-5a done) | [#54](https://github.com/jclerman/browl/issues/54) (S-5a [#55](https://github.com/jclerman/browl/issues/55), S-5b [#56](https://github.com/jclerman/browl/issues/56)) |
| S-6 | Keep Fuseki's container memory limited: measure container memory and page throughput against the direct-buffer limit, other JVM settings, heap size and a Compose memory limit; recommend Fuseki's JVM options and a container memory figure | de-risks O-1, N-1–N-4 | P-3 | Done | [#59](https://github.com/jclerman/browl/issues/59) |

### Phase 1: Foundation

| ID | Work item | Delivers | Depends on | Status | Tracking |
| --- | --- | --- | --- | --- | --- |
| P-1 | Project scaffold: `browl` package, tooling per the coding-standards doc, CI | | | Done | [#2](https://github.com/jclerman/browl/pull/2), [#4](https://github.com/jclerman/browl/pull/4), [#5](https://github.com/jclerman/browl/pull/5), [#6](https://github.com/jclerman/browl/pull/6) |
| P-28 | Coding-standards doc; settle the choices marked provisional | | | Done | [#10](https://github.com/jclerman/browl/issues/10) |
| P-2 | Settings module with validation (reserved-prefix rule, `AUTH_DISABLED` warning) | O-2–O-8, O-10, O-11 | P-1 | Done | [#51](https://github.com/jclerman/browl/pull/51) |
| P-3 | Docker Compose: `app` (Java 21, `robot.jar`, Jena's TDB2 loader) and `fuseki` services, volumes, health checks; switching Fuseki to a new store through its admin protocol; Fuseki's memory while serving | O-1, O-9 | P-1, S-4 | Done | [#11](https://github.com/jclerman/browl/issues/11) |
| P-4 | Fuseki client (async SPARQL over HTTP) and OWLAPI bridge: start the JVM via JPype, parse fetched triples as a small ontology, run Java calls on one dedicated thread | | P-2, P-3, S-1, S-4 | Done | [#15](https://github.com/jclerman/browl/issues/15) |

### Phase 2: Loading

| ID | Work item | Delivers | Depends on | Status | Tracking |
| --- | --- | --- | --- | --- | --- |
| P-5 | Job runner and job state in SQLite; "loading" / "not available" site pages | U-16, A-5 | P-2 | Done | [#14](https://github.com/jclerman/browl/issues/14) |
| P-6 | Upload page and streaming upload (auth not yet enforced) | A-2, A-4 | P-5 | Done | [#16](https://github.com/jclerman/browl/issues/16) |
| P-7 | Reasoning stage: ROBOT with optional ELK/HermiT reasoning, with limits | A-3, A-6, O-8 | P-6, S-2 | Done | [#18](https://github.com/jclerman/browl/issues/18) |
| P-8 | Fetch imports (warn on failures); bulk-load the ontology and imports into a new TDB2 store and switch Fuseki to it; no reload on restart | A-4 | P-4, P-7, S-4 | Done | [#20](https://github.com/jclerman/browl/issues/20) |
| P-30 | Run ROBOT and the OWLAPI import lister in a sandbox: no network, only the job's files, a minimal environment ([load flow](LOAD-FLOW.md)); before any deployment | A-3, A-4 | P-8 | Done | [#72](https://github.com/jclerman/browl/issues/72) |

### Phase 3: Derived data

| ID | Work item | Delivers | Depends on | Status | Tracking |
| --- | --- | --- | --- | --- | --- |
| P-9 | Derived database schema; atomic swap; generation number; reuse on restart when current; completeness check for bulk query results (2.14) | N-4 | P-8 | To do | [#21](https://github.com/jclerman/browl/issues/21) |
| P-10 | Entities (entity-list query), IRI → URL mapping, hash prefixes, collision warnings | U-2, U-13, U-15, O-3 | P-9 | To do | [#22](https://github.com/jclerman/browl/issues/22) |
| P-11 | Name values, direct and entailed through sub-properties (first decide: property path or Fuseki-configured RDFS), display names, CURIE prefixes | U-6, O-4, O-5, O-11 | P-10 | To do | [#23](https://github.com/jclerman/browl/issues/23) |
| P-12 | Hierarchies (edges query), equivalence groups, distinct descendant counts | U-7, U-8, U-9 | P-10, S-3 | To do | [#24](https://github.com/jclerman/browl/issues/24) |

### Phase 4: Browsing

| ID | Work item | Delivers | Depends on | Status | Tracking |
| --- | --- | --- | --- | --- | --- |
| P-13 | Routing: path lookup, 404, disambiguation page, hash-prefix page and script | U-2, U-13, U-14, U-15 | P-10 | To do | [#25](https://github.com/jclerman/browl/issues/25) |
| P-14 | Base templates and page frame: navigation bar, page header, tab component, stylesheet and themes; links rendered from display names; IRI `<details>` | U-4, U-5, U-6, U-17, U-27, U-28, U-29 | P-11, P-13 | To do | [#27](https://github.com/jclerman/browl/issues/27) (sub-issues [#28](https://github.com/jclerman/browl/issues/28), [#33](https://github.com/jclerman/browl/issues/33), [#34](https://github.com/jclerman/browl/issues/34)) |
| P-15 | Manchester rendering via the OWLAPI from fetched fragments, including n-ary disjointness and general class axioms; link substitution and keyword styling | U-3 | S-1, S-4, P-14 | To do | [#36](https://github.com/jclerman/browl/issues/36) |
| P-16 | Entity page Overview tab: annotations panel and logical-axioms panel (parents, equivalents, property domains and ranges), compact layout | U-3, U-9, U-30, U-31, U-32 | P-12, P-15 | To do | [#37](https://github.com/jclerman/browl/issues/37) (sub-issues [#29](https://github.com/jclerman/browl/issues/29), [#38](https://github.com/jclerman/browl/issues/38), [#39](https://github.com/jclerman/browl/issues/39)) |
| P-17 | Ontology home page, with the dropped-imports warning and the triple counts the job state keeps (P-8) | U-1 | P-14 | To do | [#30](https://github.com/jclerman/browl/issues/30) |
| P-18 | Hierarchy trees with lazy loading (htmx) and keyboard support | U-7, U-17 | P-12, P-14 | To do | [#31](https://github.com/jclerman/browl/issues/31) |
| P-27 | Usage tab on entity pages, from Fuseki, with paging (first decide: references inside class expressions; counts on first view or up front; any `usage` table) | U-26 | P-9, P-16, S-4 | To do | [#41](https://github.com/jclerman/browl/issues/41) |

### Phase 5: Search, caching, auth

| ID | Work item | Delivers | Depends on | Status | Tracking |
| --- | --- | --- | --- | --- | --- |
| P-19 | `SearchBackend` interface and SQLite implementation; ranking | U-10, U-11, U-12, N-5 | P-11 | To do | [#26](https://github.com/jclerman/browl/issues/26) |
| P-20 | Autocomplete UI (accessible combobox) with type and property filters | U-10, U-17 | P-19, P-14 | To do | [#32](https://github.com/jclerman/browl/issues/32) |
| P-21 | Page cache keyed by generation | O-10, N-3 | P-16 | To do | [#40](https://github.com/jclerman/browl/issues/40) |
| P-22 | Google sign-in, admin-route protection, dev no-auth mode | A-1, O-6, O-7 | P-6 | To do | [#17](https://github.com/jclerman/browl/issues/17) |
| P-23 | Admin list management page and persistence | A-7 | P-22 | To do | [#19](https://github.com/jclerman/browl/issues/19) |

### Phase 6: Hardening

| ID | Work item | Delivers | Depends on | Status | Tracking |
| --- | --- | --- | --- | --- | --- |
| P-24 | Performance check against N-1–N-3 targets at scale; whether streaming extraction alongside the TDB2 load is worth it | N-1–N-3 | all above | To do | [#42](https://github.com/jclerman/browl/issues/42) |
| P-25 | Accessibility pass (trees, combobox, no-JS pages) | U-17 | P-18, P-20 | To do | [#35](https://github.com/jclerman/browl/issues/35) |
| P-29 | Store the upload, fetched imports and ROBOT's output compressed at rest (gzip shrank Uberon 13–16×, P-8), and accept compressed uploads; measure the cost first | A-2 | P-8 | To do | [#71](https://github.com/jclerman/browl/issues/71) |
| P-31 | Show load progress: each stage's elapsed time, ROBOT's current step, triples loaded of the total, the import being handled | A-5 | P-8 | To do | [#73](https://github.com/jclerman/browl/issues/73) |
| P-32 | Show the reasoner's own progress (per task, as Protégé does), running ROBOT's reasoning through its Java API with a progress monitor | A-5 | P-31, P-30 | To do | [#74](https://github.com/jclerman/browl/issues/74) |
| P-26 | Update README: remove Owlready2 and Fuseki (now the OWLAPI via JPype); replace "cached once built" with the bounded cache | | | Done | [#7](https://github.com/jclerman/browl/pull/7) |

### Later

Requirements marked **Later** (U-18–U-25, U-33–U-41, A-8, A-9, O-12) are not yet broken into work items.

**Novel features** _(post-log)_. EBI's [OLS4](https://github.com/EBISPOT/ols4) already offers much of v1 for many
ontologies at once. It shows hierarchies along configured properties, but it does no reasoning, and its "Related from"
section lists only the entities whose `SubClassOf(P some C)` restrictions mention an entity, not the axioms. These
Later requirements add features not yet offered by existing ontology browsers, building on the OWLAPI, a reasoner in
the load pipeline, and serving one ontology at a time. In rough priority order:

1. Usage tab filters (U-34).
2. Axiom annotations (U-35).
3. SPARQL endpoint and download (U-36, U-37).
4. Relation trees (U-38, O-12). Unlike OLS4's, their edges also come from equivalence definitions (below).
5. Inferred axioms and their explanations (U-39, U-40).
6. Similar classes by ontology structure (U-41). OLS4 offers similarity from text embeddings of labels and synonyms
   (S-5); U-41 uses the ontology's graph instead.

**Relation-tree edges from equivalence definitions** (for U-38). `EquivalentClasses(C, A and (P some D))` gives `C` the
parent `D` in the tree for `P`. Tentatively, the edges query (P-12) finds these itself: it walks the `owl:intersectionOf`
list of each `owl:equivalentClass` on a named class (in either direction) and keeps the conjuncts that are
`owl:someValuesFrom` restrictions on a configured property with a named filler. That works; a faster or simpler way can
replace it later. It also adds no axioms to the store, keeping the asserted/inferred split (below) clean. Where reasoning
ran with `P` in `MATERIALIZE_PROPERTIES` ([2.4](#24-load-pipeline)), `materialize` has already asserted these edges, so
O-12's properties might default to that list.

browl avoids ROBOT `relax`, the usual tool for this. `relax` does add `SubClassOf(C, P some D)` for each such conjunct,
but it also turns every object cardinality restriction with a count above 0 into `some`, and no option turns that off.
That goes beyond what U-38 needs, and for `max` it is wrong: `P max 2 D` allows zero, so it doesn't imply `P some D`.
The code ([`RelaxOperation.java`](https://github.com/ontodev/robot/blob/master/robot-core/src/main/java/org/obolibrary/robot/RelaxOperation.java),
checked 2026-11-16) tests for `OWLObjectCardinalityRestriction`, which covers `min`, `exact` and `max`.

**Asserted and inferred kept apart** (for U-39, U-40). Today the reasoned output replaces the uploaded ontology
([2.4](#24-load-pipeline)), so what was asserted is lost to the store; P-7 keeps the upload on disk beside it, so the
split needs no change to reasoning. The reasoned output isn't a superset of the upload: reasoning removes redundant
and shortcut subclass axioms, so the asserted axioms come from the upload. Following §2.4's store layout (ontology in
the default graph, imports in named graphs, queries merging every graph with `FROM`), the asserted ontology would stay
in the default graph and only the inferred axioms would go in a named graph of their own. Queries already merge every
graph, so they see both unchanged. An axiom is inferred if its triples are in that graph and not in the default graph.
Inferences are usually far fewer than asserted axioms, so the extra load time and store size that a named graph costs
(about 2.2× on NCBITaxon, P-3) fall only on them. Explanations are computed on demand (ROBOT `explain` or the OWLAPI
explanation API) and cached.

Deferred with U-39 and U-40 (decided after P-8): until then, a load with reasoning serves the reasoned ontology alone,
and nothing is marked inferred. The split should cost little rework. The loading stage already loads files into graphs
by name and checks every graph's count, so it gains one more file and graph, plus computing the inferred triples (the
reasoned output's triples that aren't the upload's). Two things would change elsewhere: the `FROM` merge would apply
whenever the store has any named graph, not only imports (`browl.fuseki.dataset_clause`), and whatever marks imported
entities by graph must skip the inferred graph. One behavior to settle then: the merged view would bring back the
shortcut `rdfs:subClassOf` links that reasoning removes, since they're asserted, so trees (P-12) would need to drop
them again.

Out of scope for now: a quality report on upload (ROBOT `report`, which assumes OBO conventions that not every
ontology follows) and version history with diffs between loads.
