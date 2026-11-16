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
| **U-1** · The ontology home page shows the ontology's metadata: ontology IRI, version IRI, annotations, imports, entity counts, and whether a reasoner was applied and which one. _(Q7c)_ | v1 |
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
| **O-1** · The service starts with one command (`docker compose up`). It runs as two containers: the app and Fuseki. _(S-4; was a single container)_ | v1 |
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
   loaded ontology    search index,
                      admins, job state)
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
  ([2.8](#28-entity-pages)). _(post-log)_
- **ROBOT as a subprocess** runs the optional reasoning, in its own JVM so time and memory limits can be enforced and a
  runaway run killed. Jena's TDB2 loader also runs as a subprocess, building the store for each load. _(Q7c)_
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
| Deployment | Docker Compose: `app` and `fuseki` services, named volumes | The app image includes Java 21, `robot.jar` and Jena's command-line tools (for the TDB2 loader). _(S-4)_ |

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

### 2.4 Load pipeline

One background job at a time, run in the app process. Its state (stage, start time, error) is stored in SQLite, so every
server worker reports it consistently. _(Q7b, Q14a)_

| Stage | What happens |
| --- | --- |
| **uploading** | The file is streamed to the data volume. The site switches to the "loading" page (U-16) and the page cache is cleared. |
| **imports** | Imported ontologies are fetched (see Imports, below). |
| **reasoning** (optional) | A build script runs ROBOT `reason` with ELK or HermiT. Its output, written as Turtle, replaces the uploaded file as the ontology that gets served. Without reasoning, an `.ofn` upload, which the TDB2 loader can't read (it reads only RDF formats), is converted to Turtle by ROBOT; RDF/XML and Turtle uploads load as they are. Turtle is tentative: P-8 measures which RDF format ROBOT can write that loads fastest. _(Q7a)_ |
| **loading** | Jena's TDB2 bulk loader builds a new store from the built ontology, in a new directory on the data volume. Fuseki is then switched to it and the previous store deleted. Uberon loads in 4–5 s, NCBITaxon in about 3 min ([S-4 findings](../research/S-4/README.md)). _(Q15)_ |
| **pre-caching** | A few bulk SPARQL queries fetch the whole-ontology data, and the derived SQLite database is built from them ([2.5](#25-derived-data-pre-caching)), as a new file then swapped in atomically. |
| **ready** | The new generation number is recorded; pages are served again. |

Uberon goes from upload file to ready in 7 s without reasoning, measured end to end; NCBITaxon is estimated at about
5 minutes ([S-4 findings](../research/S-4/README.md)).

**Reasoner limits** _(Q7)_:

- Time: the ROBOT subprocess runs with a timeout and is killed when it's reached; the job records "time limit".
- Memory: the JVM heap is capped (`-Xmx`, set via `ROBOT_JAVA_ARGS`), and the job records "memory limit" when it finds
  `OutOfMemoryError` in ROBOT's output. To be confirmed by spike S-2.

On failure the job records the stage and reason (A-6), and the site stays on a "not available" page until an admin
uploads again. Downtime during a replace is accepted. _(Q14a)_

**App restart:** the store and the derived database stay on the data volume. Fuseki reopens the store in under a
second, and the app reuses the derived database when its generation number matches, skipping pre-caching. Restarting
either container doesn't reload the ontology.

**Switching Fuseki to a new store** happens at the end of every loading stage, including the first. The TDB2 loader
can only write a new store, not one Fuseki has open, so Fuseki keeps serving the previous store (behind the "loading"
page) until the new one is built. The app then switches Fuseki over with **Fuseki's admin protocol** (`/$/datasets`).
The app and Fuseki are separate containers, so this avoids the app restarting Fuseki. Tried in P-3 with Fuseki 6.2.0:

- Fuseki 6 refuses a dataset description sent to the admin protocol, so a dataset is registered by name only
  (`dbName=gen-N`, `dbType=tdb2`), and Fuseki's template puts its store at `{FUSEKI_BASE}/databases/gen-N`. The loader
  therefore builds each new store at that path, on the Fuseki volume that the app also mounts; registering the name
  opens it.
- Removing the old dataset (`DELETE /$/datasets/gen-N`) also deletes its store.
- Fuseki records registered datasets in its base directory, on the same volume, so the current one survives restarts.
- The admin endpoints are open to any client that reaches Fuseki (its default allows only localhost, and the app is
  another container), so Fuseki's port is not published outside the Compose network.

Fuseki didn't leak across 60 switches of a small store, each followed by queries: its container's memory levelled off
at 220–236 MiB after the first 10, with 55 open files and 38 threads at the end. Had it leaked, the fallback was a fixed
store path and a Fuseki restart, which takes under a second.

**Imports:** browl follows `owl:imports` where it can, and warns where it can't. The TDB2 loader doesn't follow
imports, and by default ROBOT refuses an ontology whose imports can't be fetched
([S-1 findings](../research/S-1/README.md)), so the pipeline resolves them itself:

1. Fetch each imported ontology, recursively, into the data volume. An import that can't be fetched is dropped from
   the `owl:imports` list, and the upload page lists it as a warning.
2. Reasoning, if any, gets the fetched files through a catalog file, so ROBOT doesn't fetch them again.
3. The loading stage loads the ontology into the store's default graph, and each import into its own named graph. Each
   triple's source stays known, e.g. to mark imported entities; merging into one file first (ROBOT `merge`) would lose
   that.
4. When the ontology has imports, queries that should see them name a dataset that merges every graph:
   `FROM <urn:x-arq:DefaultGraph> FROM <urn:x-arq:UnionGraph>`. The merge is a set, so a triple that the ontology and
   an import both state counts once. Without imports, queries have no `FROM` clause.

P-8 confirms the details.

**Why the ontology goes in the default graph** (measured in P-3, NCBITaxon loaded with `tdb2.xloader` in the app
container, Fuseki with a 2 GB heap): TDB2 indexes named graphs separately, so a named graph costs about 2.2× in store
size and load time (5.2 GB and 358 s, against 2.4 GB and 165 s in the default graph); page queries ran at the same speed
on both. Keeping only the imports in named graphs means an ontology without imports pays nothing. Adding Uberon as an
import (11 s; the store grew to 3.2 GB), the `FROM` merge cost about 1 ms per page at one client (median 4.0 against
2.8 ms) and nothing measurable at 16 (682 against 641 pages a second). An explicit `{ … } UNION { GRAPH ?g { … } }`
was no faster, and returns a triple once per graph that states it. Fuseki's alternative, `tdb:unionDefaultGraph`,
would put every triple in a named graph and hide the default graph.

**Heap sizes:** the app's JVM only renders per-page fragments, so it needs a small heap (`APP_JVM_MAX_HEAP`).

Fuseki's heap defaults to 1 GB (`FUSEKI_HEAP` in `.env`, read by `docker-compose.yml`). Measured in P-3 on NCBITaxon,
with the bulk pre-caching queries ([2.5](#25-derived-data-pre-caching)) and then page queries from 1 and 16 clients:
the heap held 430–490 MiB after garbage collection at its peak, during the bulk name-dictionary query, so 1 GB leaves
about 2× headroom. 512 MB was too tight (constant collection; median page time at 16 clients rose from about 22 to
92 ms), and at 256 MB the dictionary query ran out of memory. Page throughput didn't differ measurably between 1, 2 and
4 GB; runs varied more with how much of the store the OS had cached. TDB2's store is memory-mapped, so memory not given
to the heap caches the store. Raise the heap for ontologies larger than NCBITaxon or with large imports, whose bulk
queries go through the `FROM` merge (not measured). On `OutOfMemoryError` Fuseki exits and Compose restarts it; without
that, it hung after one and answered nothing.

After an app heap failure the app restarts its process instead of carrying on, because the error can also kill JPype's internal
reference-cleanup thread ([S-1 findings](../research/S-1/README.md)).

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
| `REASONER_TIMEOUT_S`, `REASONER_MAX_HEAP` | `3600`, `8g`: placeholders until spike S-2 | O-8 |
| `APP_JVM_MAX_HEAP` | `1g`, to be confirmed in P-4 | heap for the app's in-process JVM, which only renders per-page fragments (S-4) |
| `FUSEKI_URL` | `http://fuseki:3030` | Fuseki's address on the Compose network |
| `ROBOT_JAR` | `/opt/robot/robot.jar`, where the app image installs it | used both on the app's classpath and for reasoning subprocesses |
| `JENA_HOME` | `/opt/jena`, where the app image installs it | Jena's command-line tools, for the TDB2 loader |
| `DATA_DIR` | `/data` | mounted volume; O-9 |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `SEED_ADMINS` | none | required unless `AUTH_DISABLED`; O-6 |
| `AUTH_DISABLED` | `false` | O-7 |

### 2.13 URL space for the service's own pages

Everything under `/{RESERVED_PREFIX}/` (e.g. `/_browl/`):

| Path | Purpose |
| --- | --- |
| `search` | Autocomplete endpoint (htmx fragment) and a full results page |
| `tree/{hierarchy}` | Tree root page; `?node=` returns one node's children (htmx fragment) |
| `iri?u=…` | Fallback entity URLs (Q1b) |
| `static/…` | CSS, htmx, hash-IRI script |
| `admin/…` | Upload, status, admin list, sign-in/out |
| `health` | Liveness check for the container; also reports whether Fuseki answers |

### 2.14 Risks

| Risk | Mitigation |
| --- | --- |
| Fuseki needs too much memory while serving the largest ontologies | Measured by P-3: NCBITaxon needs about 500 MiB of heap; the default is 1 GB (2.4). |
| A bulk query that fails partway returns a truncated result with HTTP 200 | Seen in P-3: Fuseki streams results, so when it ran out of memory partway through the name dictionary, the status had already been sent. P-8 checks that each bulk result is complete (e.g. a final count, or the TSV ends cleanly) before building the derived database. |
| Fuseki pauses for full garbage collections under page load | Seen in P-3: hundreds of `System.gc()` calls, each pausing 70–275 ms, made by the JDK when Fuseki's direct (I/O) buffers reach their limit, which defaults to the heap size. Raising that limit made throughput worse. P-24 finds what allocates the buffers if page latency needs it. |
| JPype quirks: thread safety of concurrent OWLAPI calls; Python–Java call overhead | Resolved by S-1: concurrent calls were safe but gave no extra throughput, so OWLAPI calls run on one thread (2.1). Call overhead is small ([S-1 findings](../research/S-1/README.md)). |
| Memory limit can't be reported reliably | Spike S-2; at worst report "reasoner failed" with ROBOT's output. |
| Descendant counts too slow or memory-hungry at millions of classes | Resolved by S-3: roaring bitmaps, 4.2 s for NCBITaxon ([S-3 findings](../research/S-3/README.md)). |
| Loading a very large ontology takes minutes | Accepted for uploads (Q14a), shown on the status page. Restarts don't reload (2.4). NCBITaxon: about 5 minutes, estimated; Uberon: 7 s, measured ([S-4 findings](../research/S-4/README.md)). P-24 checks whether extracting the dictionary and edges by streaming the upload file, in parallel with the TDB2 load, is worth it. |
| Fragment rendering misses axioms a full-ontology render would show | 300 of 300 Uberon classes matched (S-4); P-15 adds n-ary disjointness and general class axioms, and tests against full-ontology rendering. |
| Usage misses references inside class expressions | P-27 tests walking from the blank node up to the owning axiom before building the tab. |

---

## 3. Implementation Plan

Work items are sized to be one issue or PR each, roughly. **Delivers** lists the requirement IDs. **Tracking** links
each item's GitHub issue, or the PRs that did it. Each issue states its goal, entry criteria and acceptance criteria;
GitHub's "blocked by" links and sub-issues mirror **Depends on**, and milestones mirror the phases.

### Phase 0: Spikes

Spikes are time-boxed investigations. Their code lives in `research/S-n/`, outside the `browl` package; their findings
go into this document.

| ID | Work item | Delivers | Depends on | Tracking |
| --- | --- | --- | --- | --- |
| S-1 | OWLAPI via JPype: start the JVM with `robot.jar`, load an ontology, render axioms in Manchester syntax with link tokens, list referencing axioms; measure per-entity render time, including concurrent requests. Briefly compare py-horned-owl's load time, memory and Manchester output. | de-risks U-3, U-26, N-3 | | [#8](https://github.com/jclerman/browl/issues/8) |
| S-2 | ROBOT reasoning with time and heap limits: confirm a memory breach can be detected and told apart from other failures | de-risks A-6, O-8 | | [#12](https://github.com/jclerman/browl/issues/12) |
| S-3 | Scale test with a large ontology (e.g. NCBITaxon or ChEBI): OWLAPI load time and heap; descendant-count computation time and memory; `usage` table build time and size; derived database size | de-risks N-1–N-4 | S-1 | [#13](https://github.com/jclerman/browl/issues/13) |
| S-4 | Serving without an in-process OWLAPI: Fuseki/TDB2 queried on demand (aggregates fetched up front with single SPARQL queries), or pre-rendered SQLite. Measure bulk load, restart readiness, page-query latency, Manchester rendering. | de-risks N-1–N-4, U-16 | S-3 | [#47](https://github.com/jclerman/browl/issues/47) |

### Phase 1: Foundation

| ID | Work item | Delivers | Depends on | Tracking |
| --- | --- | --- | --- | --- |
| P-1 | Project scaffold: `browl` package, tooling per the coding-standards doc, CI | | | done: [#2](https://github.com/jclerman/browl/pull/2), [#4](https://github.com/jclerman/browl/pull/4), [#5](https://github.com/jclerman/browl/pull/5), [#6](https://github.com/jclerman/browl/pull/6) |
| P-28 | Coding-standards doc; settle the choices marked provisional | | | [#10](https://github.com/jclerman/browl/issues/10) |
| P-2 | Settings module with validation (reserved-prefix rule, `AUTH_DISABLED` warning) | O-2–O-8, O-10, O-11 | P-1 | done: [#51](https://github.com/jclerman/browl/pull/51) |
| P-3 | Docker Compose: `app` (Java 21, `robot.jar`, Jena's TDB2 loader) and `fuseki` services, volumes, health checks; switching Fuseki to a new store through its admin protocol; Fuseki's memory while serving | O-1, O-9 | P-1, S-4 | [#11](https://github.com/jclerman/browl/issues/11) |
| P-4 | Fuseki client (async SPARQL over HTTP) and OWLAPI bridge: start the JVM via JPype, parse fetched triples as a small ontology, run Java calls on one dedicated thread | | P-2, P-3, S-1, S-4 | [#15](https://github.com/jclerman/browl/issues/15) |

### Phase 2: Loading

| ID | Work item | Delivers | Depends on | Tracking |
| --- | --- | --- | --- | --- |
| P-5 | Job runner and job state in SQLite; "loading" / "not available" site pages | U-16, A-5 | P-2 | [#14](https://github.com/jclerman/browl/issues/14) |
| P-6 | Upload page and streaming upload (auth not yet enforced) | A-2, A-4 | P-5 | [#16](https://github.com/jclerman/browl/issues/16) |
| P-7 | ROBOT build script: optional ELK/HermiT reasoning, with limits | A-3, A-6, O-8 | P-6, S-2 | [#18](https://github.com/jclerman/browl/issues/18) |
| P-8 | Fetch imports (warn on failures); bulk-load the ontology and imports into a new TDB2 store and switch Fuseki to it; no reload on restart | A-4 | P-4, P-7, S-4 | [#20](https://github.com/jclerman/browl/issues/20) |

### Phase 3: Derived data

| ID | Work item | Delivers | Depends on | Tracking |
| --- | --- | --- | --- | --- |
| P-9 | Derived database schema; atomic swap; generation number; reuse on restart when current | N-4 | P-8 | [#21](https://github.com/jclerman/browl/issues/21) |
| P-10 | Entities (entity-list query), IRI → URL mapping, hash prefixes, collision warnings | U-2, U-13, U-15, O-3 | P-9 | [#22](https://github.com/jclerman/browl/issues/22) |
| P-11 | Name values, direct and entailed through sub-properties (first decide: property path or Fuseki-configured RDFS), display names, CURIE prefixes | U-6, O-4, O-5, O-11 | P-10 | [#23](https://github.com/jclerman/browl/issues/23) |
| P-12 | Hierarchies (edges query), equivalence groups, distinct descendant counts | U-7, U-8, U-9 | P-10, S-3 | [#24](https://github.com/jclerman/browl/issues/24) |

### Phase 4: Browsing

| ID | Work item | Delivers | Depends on | Tracking |
| --- | --- | --- | --- | --- |
| P-13 | Routing: path lookup, 404, disambiguation page, hash-prefix page and script | U-2, U-13, U-14, U-15 | P-10 | [#25](https://github.com/jclerman/browl/issues/25) |
| P-14 | Base templates and page frame: navigation bar, page header, tab component, stylesheet and themes; links rendered from display names; IRI `<details>` | U-4, U-5, U-6, U-17, U-27, U-28, U-29 | P-11, P-13 | [#27](https://github.com/jclerman/browl/issues/27) (sub-issues [#28](https://github.com/jclerman/browl/issues/28), [#33](https://github.com/jclerman/browl/issues/33), [#34](https://github.com/jclerman/browl/issues/34)) |
| P-15 | Manchester rendering via the OWLAPI from fetched fragments, including n-ary disjointness and general class axioms; link substitution and keyword styling | U-3 | S-1, S-4, P-14 | [#36](https://github.com/jclerman/browl/issues/36) |
| P-16 | Entity page Overview tab: annotations panel and logical-axioms panel (parents, equivalents, property domains and ranges), compact layout | U-3, U-9, U-30, U-31, U-32 | P-12, P-15 | [#37](https://github.com/jclerman/browl/issues/37) (sub-issues [#29](https://github.com/jclerman/browl/issues/29), [#38](https://github.com/jclerman/browl/issues/38), [#39](https://github.com/jclerman/browl/issues/39)) |
| P-17 | Ontology home page | U-1 | P-14 | [#30](https://github.com/jclerman/browl/issues/30) |
| P-18 | Hierarchy trees with lazy loading (htmx) and keyboard support | U-7, U-17 | P-12, P-14 | [#31](https://github.com/jclerman/browl/issues/31) |
| P-27 | Usage tab on entity pages, from Fuseki, with paging (first decide: references inside class expressions; counts on first view or up front; any `usage` table) | U-26 | P-9, P-16, S-4 | [#41](https://github.com/jclerman/browl/issues/41) |

### Phase 5: Search, caching, auth

| ID | Work item | Delivers | Depends on | Tracking |
| --- | --- | --- | --- | --- |
| P-19 | `SearchBackend` interface and SQLite implementation; ranking | U-10, U-11, U-12, N-5 | P-11 | [#26](https://github.com/jclerman/browl/issues/26) |
| P-20 | Autocomplete UI (accessible combobox) with type and property filters | U-10, U-17 | P-19, P-14 | [#32](https://github.com/jclerman/browl/issues/32) |
| P-21 | Page cache keyed by generation | O-10, N-3 | P-16 | [#40](https://github.com/jclerman/browl/issues/40) |
| P-22 | Google sign-in, admin-route protection, dev no-auth mode | A-1, O-6, O-7 | P-6 | [#17](https://github.com/jclerman/browl/issues/17) |
| P-23 | Admin list management page and persistence | A-7 | P-22 | [#19](https://github.com/jclerman/browl/issues/19) |

### Phase 6: Hardening

| ID | Work item | Delivers | Depends on | Tracking |
| --- | --- | --- | --- | --- |
| P-24 | Performance check against N-1–N-3 targets at scale; whether streaming extraction alongside the TDB2 load is worth it | N-1–N-3 | all above | [#42](https://github.com/jclerman/browl/issues/42) |
| P-25 | Accessibility pass (trees, combobox, no-JS pages) | U-17 | P-18, P-20 | [#35](https://github.com/jclerman/browl/issues/35) |
| P-26 | Update README: remove Owlready2 and Fuseki (now the OWLAPI via JPype); replace "cached once built" with the bounded cache | | | done: [#7](https://github.com/jclerman/browl/pull/7) |

### Later

Requirements marked **Later** (U-18–U-25, A-8, A-9) are not yet broken into work items.
