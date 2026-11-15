# browl Design

browl (OWL-Browse) hosts an OWL ontology as a browsable website. Every declared entity gets its own page at a URL implied by its IRI.

> **Status:** draft. References like _(Q1d)_ point to the question log in `claude/DESIGN-SESSION-STATUS.md`. They are temporary, so decisions can be traced and the two documents audited against each other. They will be removed once this document is the sole source of truth.

**Contents**
1. [Requirements](#1-requirements)
2. [Technical Spec](#2-technical-spec)
3. [Implementation Plan](#3-implementation-plan)

---

## 1. Requirements

Requirements have stable IDs so plan items and tests can refer to them. The prefix gives the persona: **U** (User), **A** (Admin), **O** (Operator), **N** (non-functional). Each one is marked **v1** or **Later**.

### 1.1 Personas

| Persona | Who they are |
|---|---|
| **User** | Anyone browsing the ontology. Anonymous; no sign-in. |
| **Admin** | Signs in to load or replace the ontology, choose reasoning, and manage who else is an admin. |
| **Operator** | Deploys and configures the service: base URL, limits, authentication, storage. |

### 1.2 User requirements

| Requirement | Priority |
|---|---|
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
| **U-26** · Each entity page has a **Usage** tab, like Protégé Desktop's Usage tab, not shown by default: the axioms in which other entities refer to this entity (logical axioms and annotation assertions whose value is this entity), rendered in Manchester syntax and grouped by referring entity, with a total count and paging. Axioms not about a single entity (general class axioms, `DisjointClasses(A, B, C)`) are listed under every named entity they mention. | v1 |
| **U-27** · A persistent navigation bar on every page links to the ontology home page, the search, and the tree page for each entity type. | v1 |
| **U-28** · Page-specific content is organized in tabs within the page (e.g. an entity's Overview and Usage; for classes, later, Individuals). The tabs are visually distinct from the navigation bar. | v1 |
| **U-29** · The layout makes good use of space and is as readable as possible without looking harsh. | v1 |
| **U-18** · Named individuals have pages, and each class page lists its individuals in an Individuals tab, with paging. _(Q17)_ | Later |
| **U-19** · Autocomplete also matches from the start of any word. _(Q8)_ | Later |
| **U-20** · Autocomplete tolerates typos. _(Q8)_ | Later |
| **U-21** · Autocomplete can optionally match IRIs and CURIEs. Never on by default. _(Q12)_ | Later |
| **U-22** · Imported entities can link out to their real IRI where it is hosted. _(Q3)_ | Later |
| **U-23** · Content negotiation: an entity URL returns RDF (e.g. Turtle) when asked for it with the HTTP `Accept` header. _(Q4)_ | Later |
| **U-24** · Optionally hide results in less-preferred languages entirely. _(Q10)_ | Later |
| **U-25** · Optional ranking by entity importance, only if it adds little complexity. _(Q9)_ | Later |

### 1.3 Admin requirements

| Requirement | Priority |
|---|---|
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
|---|---|
| **O-1** · The service runs as a small set of containers (app and Fuseki) started with one command. | v1 |
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
|---|---|
| **N-1** · Scale: millions of entities with several annotations each, and also about 10K entities with 10–20 annotations each. _(Q5)_ | v1 |
| **N-2** · Autocomplete responds fast enough for type-ahead. Proposed target: p95 under 100 ms server-side at N-1 scale. | v1 |
| **N-3** · An uncached entity page renders fast. Proposed target: p95 under 300 ms at N-1 scale, so a small cache is enough. _(Q13)_ | v1 |
| **N-4** · Nothing that needs the whole ontology (closures, counts, the search index) is computed per request. _(Q5)_ | v1 |
| **N-5** · The search engine sits behind an interface, so replacing it changes only that module. _(Q11)_ | v1 |

---

## 2. Technical Spec

Tooling (Python version, package manager, linting, tests, typing) is left to the coding-standards document. Choices here marked _provisional_ are placeholders until then.

### 2.1 Architecture overview

```
           browser
              │  HTML (Jinja) + htmx
              ▼
   ┌─────────────────────────┐   SPARQL (HTTP)   ┌──────────────────┐
   │  browl app (FastAPI)    │ ────────────────▶ │ Apache Jena      │
   │  - routing / IRI map    │                   │ Fuseki (TDB2)    │
   │  - page rendering       │                   │ RDFS inference   │
   │  - load pipeline job    │                   └──────────────────┘
   │  - search shim          │
   └──────┬──────────┬───────┘
          │          │ subprocess
          ▼          ▼
   derived SQLite   ROBOT (JVM): format conversion, ELK/HermiT reasoning
   (pre-cached data,
    search index,
    admin list, job state)
```

- **Fuseki** holds the ontology as RDF and answers per-entity queries (annotations, axioms). Its inference is limited to RDFS. _(Q7)_
- **Derived SQLite database** holds everything computed once at load time: entity list, display names, hierarchy and counts, hash prefixes, equivalence groups, prefixes and the search index. Page rendering reads mostly from here. _(Q5)_
- **ROBOT** is invoked as a subprocess for format conversion and optional reasoning. _(Q7c, Q15)_
- **Owlready2 is not used.** _(Q6)_

### 2.2 Libraries and components

| Concern | Choice | Notes |
|---|---|---|
| Web framework | FastAPI, Uvicorn | |
| Templates | Jinja2 | |
| Front-end behavior | htmx, served from the app's static files (no CDN) | No SPA framework and no front-end build step. _(Q20)_ |
| Data models | Pydantic | Entities, settings, job state. |
| Configuration | pydantic-settings | Defaults in code; environment variables override. _(Q13)_ |
| Triple store | Apache Jena Fuseki, TDB2 dataset | |
| Fuseki client | httpx (async), SPARQL JSON results | No RDF library needed for bulk data. |
| Small RDF graphs | rdflib | Only for one entity's axiom structure when rendering Manchester syntax. |
| Derived store and search (v1) | SQLite (Python standard library), queried in a thread pool | FTS5 later for any-word search. _(Q8, Q11)_ |
| Reasoning and conversion | ROBOT (Java) | ELK and HermiT. |
| Authentication | Authlib (OpenID Connect with Google), Starlette signed-cookie sessions | |
| Deployment | Docker Compose: `app` and `fuseki` services, named volumes | The app image includes a Java runtime and the ROBOT jar. |

### 2.3 IRI → URL mapping

Settings: `BASE_URL` and `RESERVED_PREFIX` (default `_browl`). _(Q1, Q1c, Q1d)_

For a declared entity IRI, in order:

1. **Native:** the IRI starts with `BASE_URL`, ignoring `http` vs `https`, and its remaining path does not start with `/{RESERVED_PREFIX}`. URL = the IRI's path, e.g. `http://example.com/onto/Foo` → `/onto/Foo`.
2. **Proxied:** the IRI is a plain `http(s)` IRI (no query string, no port, no user info). URL = `/{host}{path}`, e.g. `http://example.org/ONTO/x` → `/example.org/ONTO/x`. The scheme is dropped. _(Q1a)_
3. **Fallback:** anything else (`urn:`, `tag:`, query strings, ports, IRIs whose native path would hit the reserved prefix). URL = `/{RESERVED_PREFIX}/iri?u={percent-encoded IRI}`. _(Q1b)_

For a hash IRI, the rules apply to the part before `#`, and the fragment is kept on the URL: `http://example.org/onto#Foo` → `/example.org/onto#Foo`.

**Why this can't clash with the service's own pages:** all of them live under `/{RESERVED_PREFIX}/`. Proxied URLs start with a hostname, and hostnames can't contain `_`, hence the leading-`_` rule on the prefix (checked at startup). Native IRIs that would collide get the fallback, and the pre-caching stage logs a warning listing them.

**Lookup:** pre-caching stores, for every declared entity, its URL path and fragment. A request is resolved by looking up the path:

| The path matches… | Response |
|---|---|
| the ontology IRI | Home page. If hash entities share this path, the page also includes the hash script below. |
| one entity (non-hash) | That entity's page. |
| two entities (only `http` vs `https` differs) | Disambiguation page. _(Q1a)_ |
| a hash prefix only | A small page with a script that reads the fragment and loads that entity, or shows "not found" when there is no fragment or no such entity. HTTP status 200, since the server can't see the fragment. _(Q2a)_ |
| nothing | HTTP 404. |

A path can be both an ordinary entity and a hash prefix; then the entity page is served with the hash script included.

### 2.4 Load pipeline

One background job at a time, run in the app process. Its state (stage, start time, error) is stored in SQLite, so every server worker reports it consistently. _(Q7b, Q14a)_

| Stage | What happens |
|---|---|
| **uploading** | The file is streamed to the data volume. The site switches to the "loading" page (U-16) and the page cache is cleared. |
| **reasoning** (optional) | A build script runs ROBOT: convert the format if needed, then `reason` with ELK or HermiT. The built output replaces the uploaded file as the ontology that gets served. _(Q7a)_ |
| **loading** | The Fuseki dataset is cleared and the built ontology is loaded into it. Formats Jena can't parse (`.ofn`) are converted with ROBOT first, even with no reasoner. _(Q15)_ |
| **pre-caching** | The derived SQLite database is built from Fuseki ([2.5](#25-derived-data-pre-caching)), as a new file then swapped in atomically. |
| **ready** | The new generation number is recorded; pages are served again. |

**Reasoner limits** _(Q7)_:
- Time: the ROBOT subprocess runs with a timeout and is killed when it's reached; the job records "time limit".
- Memory: the JVM heap is capped (`-Xmx`, set via `ROBOT_JAVA_ARGS`), and the job records "memory limit" when it finds `OutOfMemoryError` in ROBOT's output. To be confirmed by spike S-2.

On failure the job records the stage and reason (A-6), and the site stays on a "not available" page until an admin uploads again. Downtime during a replace is accepted. _(Q14a)_

**Loading method:** v1 uses the Fuseki HTTP interface (Graph Store Protocol) with a streamed body. Fuseki's offline bulk loader (`tdb2.tdbloader`) is the fallback if spike S-3 shows HTTP loading is too slow at N-1 scale.

### 2.5 Derived data (pre-caching)

Built once per load, stored in the derived SQLite database. Proposed tables:

| Table | Contents |
|---|---|
| `entity` | IRI, entity type, URL path, fragment, display name, CURIE |
| `name_value` | entity, annotation property, value, normalized value, language, `direct` flag (one row per direct and per RDFS-entailed property) |
| `edge` | parent → child per hierarchy (classes; each property type), between equivalence-group representatives |
| `equiv_group` | entity → group ID (union-find over `owl:equivalentClass` between named classes) |
| `counts` | per node: number of immediate children, number of distinct descendants |
| `hash_prefix` | distinct URL paths that have hash entities under them |
| `prefix` | CURIE prefix → namespace (ontology's own, built-ins, configured overrides) |
| `meta` | ontology IRI, version IRI, reasoner used, load time, generation number |
| `usage` | referenced entity → referring entity, axiom kind (one row per pair and kind; built by walking each axiom's blank-node structure for named entities; an axiom with no single subject, such as a general class axiom or `owl:AllDisjointClasses`, gets a row for every named entity it mentions) |

**Hierarchies.** Named direct parents, taken from Fuseki (asserted, or inferred if a reasoner ran). Roots are nodes with no named parent. For classes this includes children of `owl:Thing`.

**Distinct descendant counts.** These need a transitive closure, not a sum over edges. _(Q19a)_ The plan is to compute them in a bottom-up pass in topological order, merging descendant sets. Compact integer sets (e.g. Python's `int` used as a bitset, or a roaring-bitmap library) keep this practical. Only the counts are stored, not the full closure. Memory use and run time at N-1 scale are what spike S-3 must measure. If they're too high, the fallback is a closure table built in SQLite.

**Entailed name values.** For each name property in the configured list, its sub-properties (via `rdfs:subPropertyOf`, transitively) are resolved, and values of a sub-property are also stored under the super-property with `direct = false`. _(Q8)_

### 2.6 Search

A `SearchBackend` interface with methods for indexing during pre-caching and for queries. Only its module knows the engine. _(Q11, N-5)_

**v1 implementation:** the `name_value` table in SQLite with an index on `normalized value`. A query is a range scan (`value >= q AND value < q + '￿'`) filtered by entity type, property and `direct`. _(Q8)_

**Ranking** _(Q9, Q10)_, in order:
1. Exact match before prefix match.
2. Position of the property in the configured name list.
3. Position of the language in the language ranking. Untagged values come last by default (O-5).
4. Shorter value.
5. Alphabetical.

Results are grouped per entity: an entity appears once, with its best-matching value shown.

**Normalization:** Unicode NFKD, accents removed, case folded. The same function is used at index and query time.

**Later:** FTS5 for any-word matching (U-19); an external engine such as Typesense for typo tolerance (U-20), swapped in behind the same interface.

### 2.7 Display names

One ordered list of name annotation properties (default `rdfs:label` first) drives both display names and search ranking. _(Q18, Q18a)_ For each entity, the display name is the first value found by:

1. property position in the list,
2. then language ranking,
3. then shortest value,
4. then alphabetical,
5. with the CURIE as the fallback, then the full IRI.

It is computed during pre-caching, so link text costs nothing at render time.

**CURIE prefixes:** the prefixes declared in the ontology file, plus a built-in set (`rdf`, `rdfs`, `owl`, `xsd`, `skos`, `obo`, `dc`, `dcterms`, …), with configured mappings overriding both (O-11). An IRI with no matching prefix has no CURIE. _(Q18b)_

### 2.8 Entity pages

Header, above the tabs (U-5):
- Display name as the title, and the entity type.
- A `<details>` element with the full IRI and CURIE.

Tabs (U-28; see [2.9](#29-page-layout)):
- **Overview** (default; U-3, U-9): immediate parents and equivalent IRIs (from the derived database), annotation values, and logical axioms in Manchester syntax (from Fuseki and the renderer).
- **Usage** (U-26), labeled with its count from the `usage` table, e.g. "Usage (128)": a page of referring entities (e.g. 50 per page), each with its axioms that mention this entity, rendered on demand by the Manchester renderer. Since the tab isn't shown by default, its cost is only paid when someone opens it.
- **Individuals** (classes only; Later, U-18): the class's named individuals, paged.

**Manchester renderer** _(Q6)_: a Python module that, for one entity:
1. Fetches the triples about it, including the blank-node structures of its class expressions.
2. Rebuilds OWL class expressions following the W3C "OWL 2 to RDF mapping" in reverse.
3. Renders them in Manchester syntax, with named entities rendered as links using their display names.

Scope for v1: `SubClassOf`, `EquivalentTo`, `DisjointWith`, property characteristics, domain and range, and sub-property axioms. Constructs it doesn't recognize are shown as raw Turtle rather than failing. Feasibility is spike S-1.

### 2.9 Page layout

Every page has the same frame (U-27, U-28, U-29):

```
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
- Styled as underlined text tabs, not as a second bar, so they don't look like part of the navigation. The navigation bar has its own tinted background; the tabs sit on the page background.
- Each tab has its own URL (`?tab=usage`). Without JS the tabs are ordinary links and the server renders the chosen tab. With htmx, only the tab panel is fetched and swapped, and the URL updated. Only the visible tab's content is fetched.
- They follow the WAI-ARIA tabs pattern.
- For hash IRIs, the query string comes before the fragment (`/example.org/onto?tab=usage#Foo`). The hash-IRI script keeps it when loading the entity.
- The page cache key includes the tab.

**Visual design** (U-29):
- Readable text measure: prose such as annotation values is capped at roughly 70–80 characters per line. Trees, tables and Manchester axioms may use the full width.
- Soft contrast: dark gray text on an off-white background rather than pure black on pure white, while meeting WCAG AA contrast. Muted accent colors.
- A system font stack for text and a monospace font for IRIs and CURIEs. Manchester keywords (`some`, `and`, `SubClassOf`) are styled lightly to set them apart from entity names.
- A dark theme that follows the browser setting (`prefers-color-scheme`).
- Generous but not wasteful spacing; works from phone width up.
- Plain CSS with custom properties for the colors, so the palette can be tuned in one place. No CSS framework.

### 2.10 Page cache

An in-memory LRU of rendered HTML, keyed by `(generation, URL path)`, with a configurable size (default 100). _(Q13)_ Including the generation number in the key means a reload invalidates entries in every worker without coordination. The cache wraps a plain function that stores rendered strings. `functools.lru_cache` is not applied to `async` handlers, because it would store unrun coroutine objects rather than pages.

### 2.11 Authentication and admins

- Google sign-in via OpenID Connect (Authlib); the session is a signed cookie. Only routes under `/{RESERVED_PREFIX}/admin` require it. _(Q16)_
- An admin is a signed-in user whose verified email is in the seed list (from configuration) or in the `admin` table in a persistent SQLite file. Seed admins can't be removed in the UI. Whole-domain rules are not supported. _(Q16a)_
- `AUTH_DISABLED=true` turns authentication off for development. It logs a prominent warning at startup and is never the default. _(Q16)_

### 2.12 Configuration

Defaults are set in code, and each can be overridden by an environment variable. _(Q13)_

| Setting | Default | Notes |
|---|---|---|
| `BASE_URL` | none (required) | O-2 |
| `RESERVED_PREFIX` | `_browl` | must start with `_`; O-3 |
| `NAME_PROPERTIES` | `[rdfs:label]` | ordered; O-4 |
| `LANGUAGE_RANKING` | `[en]` | Untagged is a reserved entry (e.g. `none`), added at the end if not listed; O-5 |
| `EXTRA_PREFIXES` | `{}` | O-11 |
| `PAGE_CACHE_SIZE` | `100` | O-10 |
| `REASONER_TIMEOUT_S`, `REASONER_MAX_HEAP` | to be decided after spike S-2 | O-8 |
| `FUSEKI_URL`, `FUSEKI_DATASET` | compose defaults | |
| `DATA_DIR` | `/data` | mounted volume; O-9 |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `SEED_ADMINS` | none | O-6 |
| `AUTH_DISABLED` | `false` | O-7 |

### 2.13 URL space for the service's own pages

Everything under `/{RESERVED_PREFIX}/` (e.g. `/_browl/`):

| Path | Purpose |
|---|---|
| `search` | Autocomplete endpoint (htmx fragment) and a full results page |
| `tree/{hierarchy}` | Tree root page; `?node=` returns one node's children (htmx fragment) |
| `iri?u=…` | Fallback entity URLs (Q1b) |
| `static/…` | CSS, htmx, hash-IRI script |
| `admin/…` | Upload, status, admin list, sign-in/out |
| `health` | Liveness check for the container |

### 2.14 Risks

| Risk | Mitigation |
|---|---|
| Hand-written Manchester renderer is larger than expected | Spike S-1; v1 scope limited; raw-Turtle fallback; OWLAPI via ROBOT as a last resort (Q6). |
| Memory limit can't be reported reliably | Spike S-2; at worst report "reasoner failed" with ROBOT's output. |
| Descendant counts too slow or memory-hungry at millions of classes | Spike S-3; bitset approach, SQLite closure fallback. |
| Loading millions of entities over HTTP is too slow | Spike S-3; offline bulk loader fallback. |
| Fuseki's RDFS inference over TDB2 is slow at scale | Little depends on it: entailment for search and counts is precomputed in SQLite. Measure in S-3. |

---

## 3. Implementation Plan

Work items are sized to be one issue or PR each, roughly. The **Tracking** column is for issue and PR links once the repo has a remote. **Delivers** lists the requirement IDs.

### Phase 0: Spikes

| ID | Work item | Delivers | Depends on | Tracking |
|---|---|---|---|---|
| S-1 | Manchester renderer prototype: parse and render class expressions for a few real ontologies (e.g. GO, an OBO ontology with rich axioms); measure coverage | de-risks U-3 | | |
| S-2 | ROBOT reasoning with time and heap limits: confirm a memory breach can be detected and told apart from other failures | de-risks A-6, O-8 | | |
| S-3 | Scale test with a large ontology (e.g. NCBITaxon or ChEBI): Fuseki load time over HTTP vs bulk loader; descendant-count computation time and memory; `usage` table build time and size; derived database size | de-risks N-1–N-4 | | |

### Phase 1: Foundation

| ID | Work item | Delivers | Depends on | Tracking |
|---|---|---|---|---|
| P-1 | Project scaffold: `browl` package, tooling per the coding-standards doc, CI | | | |
| P-2 | Settings module with validation (reserved-prefix rule, `AUTH_DISABLED` warning) | O-2–O-8, O-10, O-11 | P-1 | |
| P-3 | Docker Compose: app (with Java and ROBOT) + Fuseki, volumes, health check | O-1, O-9 | P-1 | |
| P-4 | Fuseki client (async SPARQL over HTTP) | | P-3 | |

### Phase 2: Loading

| ID | Work item | Delivers | Depends on | Tracking |
|---|---|---|---|---|
| P-5 | Job runner and job state in SQLite; "loading" / "not available" site pages | U-16, A-5 | P-2 | |
| P-6 | Upload page and streaming upload (auth not yet enforced) | A-2, A-4 | P-5 | |
| P-7 | ROBOT build script: format conversion and optional ELK/HermiT reasoning, with limits | A-3, A-6, O-8 | P-6, S-2 | |
| P-8 | Load into Fuseki (clear dataset, stream) | A-4 | P-4, P-7, S-3 | |

### Phase 3: Derived data

| ID | Work item | Delivers | Depends on | Tracking |
|---|---|---|---|---|
| P-9 | Derived database schema; atomic swap; generation number | N-4 | P-8 | |
| P-10 | Entities, IRI → URL mapping, hash prefixes, collision warnings | U-2, U-13, U-15, O-3 | P-9 | |
| P-11 | Name values (direct and entailed), display names, CURIE prefixes | U-6, O-4, O-5, O-11 | P-9 | |
| P-12 | Hierarchies, equivalence groups, distinct descendant counts | U-7, U-8, U-9 | P-9, S-3 | |

### Phase 4: Browsing

| ID | Work item | Delivers | Depends on | Tracking |
|---|---|---|---|---|
| P-13 | Routing: path lookup, 404, disambiguation page, hash-prefix page and script | U-2, U-13, U-14, U-15 | P-10 | |
| P-14 | Base templates and page frame: navigation bar, page header, tab component, stylesheet and themes; links rendered from display names; IRI `<details>` | U-4, U-5, U-6, U-17, U-27, U-28, U-29 | P-11, P-13 | |
| P-15 | Manchester renderer (from S-1) | U-3 | S-1, P-14 | |
| P-16 | Entity page: parents, annotations, axioms, equivalents | U-3, U-9 | P-12, P-15 | |
| P-17 | Ontology home page | U-1 | P-14 | |
| P-18 | Hierarchy trees with lazy loading (htmx) and keyboard support | U-7, U-17 | P-12, P-14 | |
| P-27 | Usage index (`usage` table) built during pre-caching; Usage tab on entity pages, with paging | U-26 | P-12, P-16, S-3 | |

### Phase 5: Search, caching, auth

| ID | Work item | Delivers | Depends on | Tracking |
|---|---|---|---|---|
| P-19 | `SearchBackend` interface and SQLite implementation; ranking | U-10, U-11, U-12, N-5 | P-11 | |
| P-20 | Autocomplete UI (accessible combobox) with type and property filters | U-10, U-17 | P-19, P-14 | |
| P-21 | Page cache keyed by generation | O-10, N-3 | P-16 | |
| P-22 | Google sign-in, admin-route protection, dev no-auth mode | A-1, O-6, O-7 | P-6 | |
| P-23 | Admin list management page and persistence | A-7 | P-22 | |

### Phase 6: Hardening

| ID | Work item | Delivers | Depends on | Tracking |
|---|---|---|---|---|
| P-24 | Performance check against N-1–N-3 targets at scale | N-1–N-3 | all above | |
| P-25 | Accessibility pass (trees, combobox, no-JS pages) | U-17 | P-18, P-20 | |
| P-26 | Update README: remove Owlready2; replace "cached once built" with the bounded cache | | | |

### Later

Requirements marked **Later** (U-18–U-25, A-8, A-9) are not yet broken into work items.
