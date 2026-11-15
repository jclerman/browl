# Design Session Status

Open design questions for browl (OWL-Browse), with the answers given so far.
**Status** is one of `ANSWERED`, `PARTIAL` (answered, follow-ups pending) or `OPEN`.

Last updated: 2026-11-14

---

## 1. IRI → URL mapping

### Q1. Do you control the domain the IRIs use? — `ANSWERED`

> For "page as implied by its IRI" to work literally, the IRI's domain has to resolve to this service (DNS, reverse
> proxy or PURL redirect). Otherwise you need a mapping such as `/entity?iri=…` or a configurable `IRI prefix → local
> path` rule.

**Answer:** Arbitrary ontologies are supported, so we can't assume we control the domain. The service should be
configurable with where it is served:

- **Native mode:** when the service is served at, say, `http://example.com` and that is also the root of the ontology's
  IRIs, use a 1:1 IRI → URL mapping.
- **Proxied mode:** otherwise, and always for imported content from other namespaces, mirror the IRI under the service
  root. For example, `http://browl.com` serves `http://example.org/ONTO/xxx_yyy` at
  `http://browl.com/example.org/ONTO/xxx_yyy`.

**Follow-ups:**

- **Q1a.** The proxied form drops the scheme, so `http://example.org/X` and `https://example.org/X` would map to the
  same URL. Is that acceptable (treat them as the same, and fall back to a disambiguation page on a real collision)? Or
  should the scheme be in the path, e.g. `/http/example.org/X`?
  **Answer:** That’s acceptable - treat them as the same, with a disambiguation-page upon true collision.
- **Q1b.** What about IRIs that don't map cleanly to a path: non-HTTP schemes (`urn:`, `tag:`), IRIs with query strings,
  ports, or characters that need percent-encoding? Proposal: use the path form when the IRI is a plain `http(s)` IRI,
  and fall back to `/iri?u=<encoded IRI>` otherwise, so every entity is always reachable.
  **Answer:** That proposal is acceptable. I expect such IRIs to be a rare corner-case.
- **Q1c.** How should native mode be configured? Should it be set per deployment (environment or config file), or per
  ontology in the admin page?
  **Revised proposal:** a single per-deployment `BASE_URL` setting, with no separate list of native IRI prefixes. An IRI
  that starts with `BASE_URL` (ignoring http vs https, per Q1a) is served at its native path:
  `http://example.com/onto/Foo` → `/onto/Foo`. Everything else uses the proxied form, or `/iri?u=…` per Q1b. A list of
  prefixes was meant for deployments whose IRI root differs from where the service runs. Those are already covered by
  pointing a redirect at the proxied form (`https://browl.example.com/purl.example.org/ONTO/...`), so a list could be
  added later if a real case appears.
  **Answer:** Confirmed: a single per-deployment `BASE_URL`, with no list of native prefixes.
- **Q1d.** In native mode, ontology paths share the URL space with the app's own routes (an entity at
  `http://example.com/search` would clash with the search page). Proposal: put all app routes under a reserved prefix
  such as `/_browl/` (search, admin, static files, tree data), leaving every other path to the ontology. Acceptable, and
  is `/_browl/` a good name?
  **Answer:** Yes, with the prefix configurable in code. A guarantee of no conflicts is nice to have but not required;
  ontology IRI path segments starting with `_` are bad practice anyway.
  **Design note (how the no-conflict guarantee works):**
  - No choice of string can rule out a clash by itself. Characters that aren't allowed in IRIs (`|`, `{`, `^`, …) still
    reach the server percent-encoded, and an IRI can legally contain the same percent-encoding.
  - Instead, the IRI → URL mapping enforces it. Any native IRI whose path would start with the reserved prefix is not
    given its native path; it falls back to `/_browl/iri?u=…` (the Q1b fallback, also under the prefix). A warning is
    logged at load time listing affected IRIs.
  - Proxied paths can't clash, because their first segment is a hostname, and hostnames can't contain `_` (RFC
    952/1123). So the prefix must start with `_`, and the config should check this.
  - Result: app routes never clash with entity URLs. The only cost is that a rare, badly named IRI loses its 1:1 URL.

### Q2. Hash IRIs (`…/onto#Foo`)? — `ANSWERED`

> Browsers never send the fragment to the server, so every `#` entity resolves to the same URL. Supporting them needs
> client-side JS that reads the fragment and loads that entity.

**Answer:** Yes, support hash IRIs.

**Follow-up:**

- **Q2a.** What should the fragment-less URL (e.g. `/example.org/onto`) show with no `#…`? If it is the ontology IRI,
  the ontology home page is the obvious choice. Otherwise, should it show a list of the entities in that namespace?
  **Answer:** Fragment-less prefixes get no special treatment of their own; a bare prefix is "not found". The rule is:
  **declared entities get a page, and nothing else does.** The loaded ontology counts as a declared entity: its own IRI
  shows the ontology home page. When an ontology's hash prefix equals its ontology IRI (e.g. `http://example.org/onto`
  with entities `http://example.org/onto#Foo`), that URL shows the home page because it *is* the ontology IRI, not
  because it is a prefix. Imported prefixes (e.g. `http://importedontology.org` from
  `http://importedontology.org#external_iri_123`) get no page, even when named in `owl:imports`. Non-entity pages
  (search, admin, etc.) live under the configured reserved prefix (Q1d).
  **Implementation note (mechanism, not behavior):** browsers never send the `#…` part, so a request for
  `/importedontology.org` is indistinguishable from one for `/importedontology.org#external_iri_123`. For any URL under
  which hash entities exist, the server therefore returns a small page, and JS in the browser loads the entity named
  after the `#`, or shows "not found" when there is none. A real HTTP 404 status is only possible for URLs with no hash
  entities under them. To know which URLs those are, record at load time the distinct fragment-less prefixes of all
  entity IRIs containing `#`, and look the requested URL up in that set.

### Q3. More than one namespace? — `ANSWERED`

> That includes imported terms (BFO, RO, etc.). Do they get local pages, link out to their real IRIs, or both?

**Answer:** Multiple namespaces. Imported terms are shown as they appear in the ontology file and get local pages.
**Future enhancement:** optionally link out to the real IRI when it is hosted somewhere.

### Q4. Content negotiation? — `ANSWERED`

> Should the same URL return Turtle or RDF/XML when asked with `Accept: text/turtle`, the way Linked Data expects?

**Answer:** Very nice to have, but not required for v1. A placeholder is fine if it's nontrivial.
**Design note:** keep it in mind in the routing layer from the start: one route per entity, with the representation
chosen by the `Accept` header. Adding it later is then trivial.

---

## 2. Scale and storage

### Q5. Largest expected ontology? — `ANSWERED`

> In triples and in entities.

**Answer:** It must scale to millions of entities with several annotations each, and also to ontologies of about 10K
entities with 10–20 annotations each on average.
**Design implications:**

- Loading has to stream and run as a background job. For large files, use Fuseki/TDB2's bulk loader rather than HTTP
  upload.
- Anything that needs the whole ontology (closures, counts, the search index) is computed once at load time, never per
  request.

### Q6. Why both Fuseki and Owlready2? — `ANSWERED`

> That keeps two copies of the ontology to sync. Owlready2 is slow and memory-hungry at millions of entities, and its
> `str()` output looks like Python, not Manchester syntax.

**Answer:** Open to dropping Owlready2. Use it only if it helps.
**Tentative decision:** drop Owlready2. Write a Manchester-syntax renderer in Python that works directly on the RDF
graph structure (from Fuseki), following the OWL 2 → RDF mapping. Revisit if that turns out harder than expected; the
fallback is an OWLAPI renderer via a JVM sidecar or a ROBOT export at load time.

### Q7. Asserted or inferred hierarchy? — `ANSWERED`

> If inferred, which reasoner (ELK, HermiT), and is running it once at upload time acceptable?

**Answer:** Fuseki handles RDFS inference only; it doesn't cope well with more complex reasoners. An optional EL or DL
reasoner may be offered to infer subclasses etc., but it must stay optional, so already-built ontologies can be uploaded
and hosted without waiting for an OWL reasoner.

**Follow-ups:**

- **Q7a.** When the optional reasoner is run, should its results go into a separate named graph, so the UI can show both
  asserted and inferred hierarchies (with a toggle, or by marking inferred edges)? Or should they simply replace the
  asserted hierarchy?
  **Answer:** Replace.
- **Q7b.** Should the reasoner run asynchronously after the ontology is already hosted (serving the asserted view first,
  then switching over), or as an optional step before it goes live?
  **Answer:** Synchronous, and the upload page should reflect the current status (e.g. “reasoning”, “loading”, “ready”).
  Stretch goal: Also show verbose running logs for each step (could be on a separate page linked from the upload page,
  or could be inlined).
- **Q7c.** Which reasoner(s)? ELK (fast, EL only) and HermiT (full DL, slow at scale) are the usual pair. Both need the
  JVM, likely via ROBOT.
  **Answer:** Both; user-selectable. Do it via ROBOT. Keep a record of whether a reasoner was applied, and which one, so
  that can be shown as a detail on the ontology home-page.

**Clarifications (from later discussion):**

- **"Synchronous" means a sequential pipeline of stages**, run as a background job, with the current stage shown on the
  upload page:
  1. **uploading**: the file is being received and stored.
  2. **reasoning** (optional): starts only once uploading has finished.
  3. **loading**: into Fuseki.
  4. **pre-caching**: the service queries Fuseki to prepare data that pages need, e.g. the list of classes. This is
     presumably also where hierarchy closures and counts and the search index get built (Q5).
  5. **ready**: ontology pages accept requests.
- **Reasoning via a ROBOT build script.** Reasoning may simply be a build script that wraps ROBOT and produces a built
  version of the ontology. That built version is what gets loaded and served. "Replace" (Q7a) therefore falls out
  naturally: the served ontology *is* the built output.
- **Resource limits.** The reasoning step gets a time limit and a memory limit. A failure should report which limit was
  breached, where possible.
  - *Implementation note:* the time limit is a subprocess timeout; the service kills the process and knows the timeout
    was hit. The memory limit is probably easier than it looks, because ROBOT runs on the JVM: cap the heap (`-Xmx`,
    e.g. via ROBOT's `ROBOT_JAVA_ARGS`) and detect `OutOfMemoryError` in its output. To be verified when implementing.

---

## 3. Autocomplete

Proposed pattern: at upload time, build a dedicated search index with one document per `(entity IRI, entity type,
annotation property, value, language)`. Resolve "indirect values per RDFS entailment" when the index is built: if
`skos:prefLabel ⊑ rdfs:label`, the row is tagged with both properties. Each query is a single filtered prefix lookup,
never SPARQL per keystroke.

### Q8. Matching behavior? — `ANSWERED`

> Match only from the start of the label, from the start of any word ("heart" finds "congenital heart defect"), or any
> substring? Typo tolerance?

**Answer:** We can start with matching just from whole-string start (I’m not saying “label”) because I want search to be
over a configurable set of annotations that serve as name-labels). I’d like to support any-word start in the future.
Typo-tolerance can also be a future item. I do not foresee a need to match strings that start in the middle of a word.
**Design note:** matching only from the start of the whole string needs no full-text engine. A normal B-tree index on a
normalized value column (lowercased, accents removed) answers each prefix query with a range scan, in milliseconds over
millions of rows, in plain SQLite. FTS5 only becomes needed for any-word-start matching. Both can sit behind the search
shim from Q11, so v1 can start with the plain index.

### Q9. Ranking? — `ANSWERED`

> Exact matches first, then labels before synonyms, then shorter matches? Is there any notion of how important a term
> is?

**Answer:** That ranking sounds right to me. I do not foreseeing needing to rank on the basis of
classes/concept/entities (though having that option could be handy at some point - but it focusing on it now should only
be done if it doesn’t add substantial complexity).

### Q10. Languages? — `ANSWERED`

> Do labels have multiple language tags? Should a preferred language be chosen per user or per deployment?

**Answer:** Labels may have multiple lang-tags. Preferred language, or language-ranking, can be per-deployment. Terms
with more preferred languages would rank higher; less-preferred language terms could either be ranked lower or (stretch
goal) not show up at all.

**Follow-up:**

- **Q10a.** Where do literals with no language tag rank? Many ontologies leave labels untagged. Proposal: treat untagged
  as a configurable slot in the language ranking, defaulting to just after the most-preferred language. With a ranking
  of `en`, untagged, others, an `en` label wins, then an untagged one.
  **Answer:** Rank untagged literals at the bottom for now, below every language. Make that default easy to change,
  and/or allow "no language" to be placed explicitly in the ranking through configuration later.
  **Design note:** represent "untagged" as a reserved entry in the configured language ranking list (e.g. `""` or
  `"none"`), appended at the end when the list doesn't mention it. Moving it later is then a config change, not a code
  change.

### Q11. Search engine and operations? — `ANSWERED`

> Options, in order of how much extra infrastructure they need:
>
> - **SQLite FTS5** (embedded, nothing extra to run): prefix search over millions of rows in milliseconds, filters are
>   just columns, no typo tolerance.
> - **Jena-text** (Lucene built into Fuseki): no extra service, but configuration is fiddly and ranking control is
>   limited.
> - **Typesense / Meilisearch** (one extra container): typo tolerance, good ranking and type-ahead out of the box,
>   handle millions of documents comfortably.
> - **OpenSearch / Elasticsearch**: overkill unless you already run it.
>
> Suggestion: start with SQLite FTS5 behind a small search interface, so Typesense can be swapped in later if typo
> tolerance is wanted.

**Answer:** The suggestion sounds good. We should have a shim that can be swapped to allow replacement of the search
engine, so that code-changes needed for the swap are clearly isolated.

### Q12. Search IRIs and CURIEs too? — `ANSWERED`

> For example, should typing `GO:0008` find a match?

**Answer:** No need for that now, and it should not be the default behavior. Could add it later but not a priority at
all.

---

## 4. Caching and lifecycle

### Q13. Where does the page cache live? — `ANSWERED`

> In memory, on disk, or a reverse proxy? Is invalidating everything on a new upload enough? At millions of pages,
> should there be an option to pre-generate a static site?

**Answer:** The page-cache should be limited in size. The design should allow for fast rebuilding of pages, so we don’t
need a cache that’s more than, say, 100 pages (size should be configurable but that configuration can be inside the
codebase; we can always read from an env-var later). Therefore, it can live in memory behind something like Python’s
usual cache-wrappers (literally that unless we need to do it outside of Python).
**Design notes:**

- **Async gotcha:** `functools.lru_cache` on an `async def` function caches the coroutine object, not its result, and
  breaks on the second call. Cache the rendered HTML in a small LRU keyed by IRI (or use an async-aware cache) rather
  than decorating async route handlers directly.
- **Re-upload:** clear the cache when a new ontology starts loading (Q14a).
- **Per process:** with several server worker processes, each has its own cache. That's fine at this size.
- **Pre-generating a static site:** not needed, given fast rebuilding.
- **README:** "cached once built" should be reworded later to match: a small bounded cache, not every page.

### Q14. One ontology or several? — `ANSWERED`

> One ontology at a time, or several side by side? Does a re-upload replace the old one in place, or are versions kept
> (`/v2025-01/…`)?

**Answer:** For now, one ontology, and a re-upload simply replaces it.
**Future enhancement:** have several ontologies loaded and ready to serve, and decide then whether to serve one at a
time or many at once.

**Follow-up:**

- **Q14a.** What does the site serve while a re-upload is going through the pipeline (Q7)? Options: (1) keep serving the
  old ontology until the new one reaches "ready", then switch over at once; or (2) take the site down, showing a
  "loading" page, from the start of the re-upload. Option 1 means a failed reasoning step or load leaves the old
  ontology live. It needs space for two copies in Fuseki (e.g. load into a new dataset or named graph, then switch).
  Option 2 is simpler.
  **Answer:** Option 2 is fine.

### Q15. Upload formats and loading? — `ANSWERED`

> Jena doesn't parse OWL Functional Syntax or OWL/XML. Are those needed? (Easy fix: convert at upload time with ROBOT or
> the OWLAPI.) Large uploads also need to load in a background job with a progress display. Acceptable?

**Answer:** I do want to support *.ofn files. OWL/XML is not needed, but RDF/XML and TTL should also be supported. Yes,
we should use ROBOT to convert file-formats that it understands but Jena doesn’t.

### Q16. Admin authentication? — `ANSWERED`

> What is enough: a single shared token, HTTP basic auth behind a proxy, or SSO? Is the site public or internal?

**Answer:** I would love to support OAuth2 so we can grant admin privileges to Google accounts. For development, we can
have an option that disables auth altogether, so long as it can be slotted in later when we want the site to be public.
**Design note:** "sign in with Google" is OAuth2 plus OpenID Connect. Only admin routes need it; browsing stays
anonymous. The no-auth development option should refuse to start unless it is explicitly enabled, so it can't be
switched on in production by accident.

**Follow-up:**

- **Q16a.** Which Google accounts get admin rights? Proposal: a configured allowlist of email addresses, optionally plus
  whole domains (e.g. `@example.org`).
  **Answer:** Configured allow-list, yes. Admins should be able to edit that list, but the service should start with a
  “seed” list of allowed addresses. The updated list should ideally be stored in a DB or outside-of-container file so it
  survives container restarts. Whole-domain support is not necessary.
  **Design note (proposed; say if wrong):** the seed list comes from configuration and its addresses are always admins.
  They can't be removed through the UI, which prevents locking every admin out. Addresses added through the UI live in a
  small SQLite database on a mounted volume (alongside the search index), so they survive container restarts.

---

## 5. Display details

### Q17. Individuals and datatypes? — `ANSWERED`

> The README lists classes and the three property types. Do named individuals get pages and a list? Do datatypes?

**Answer:** Focus on classes and properties (and the ontology itself) first. Named individuals should be supported
later, and individuals of classes should be reachable from the class pages. We can skip datatypes.
**Design note:** a class can have very many individuals, so the list on its class page will need paging.

### Q18. Link text and prefixes? — `ANSWERED`

> Should links show the label, a CURIE, or both? What's the fallback with no label? Do prefix mappings come from the
> ontology's own prefixes, or from configuration?

**Answer:** We’ll need a ranking algorithm  to decide what string to show on links. `rdfs:label` values should be ranked
highest. Then, a configured list of annotation-properties. The page-title for a given entity should reflect the same
string. Within a given annotation-property, rank languages using the same ranking as I mentioned earlier for
autocomplete. Break ties by showing the shortest item, then alpha-sort. CURIEs are the final fallback. Ties ought to be
rare since I expect to have a label for most items, and exactly one English label for those items.

**Follow-ups:**

- **Q18a.** Is the configured list of display annotation properties the same list as Q8's "configurable set of
  annotations that serve as name-labels" for search? Proposal: one shared, ordered list. Its order drives display
  ranking (Q18) and match ranking (Q9), with `rdfs:label` first by default.
  **Answer:** Yes, one ordered list for both.
  **Clarification:** `rdfs:label` is first by default but configurable like the rest of the list, not fixed in first
  place. This supersedes "`rdfs:label` values should be ranked highest" in the Q18 answer.
- **Q18b.** For the CURIE fallback, where do the prefix mappings come from? Proposal: the prefixes declared in the
  ontology file, plus a small built-in set (`rdfs`, `owl`, `skos`, `obo`, …), with configured mappings overriding both.
  IRIs without a matching prefix show as full IRIs.
  **Answer:** Proposal accepted. In addition, full IRIs should be determinable from each entity’s page, near the top, in
  a foldaway form to save space.
  **Design note:** an HTML `<details>` element gives the foldaway with no JS. It shows the full IRI plus the CURIE when
  one exists.

### Q19. Descendant counts with multiple inheritance? — `ANSWERED`

> Assumed: each descendant is counted once. Should equivalent classes be merged into one tree node?

**Answer:** If a descendant has multiple direct parents it should be counted under each of them. Obviously that’s even
more relevant for indirect ancestors. Equivalent classes should be merged (for counting and for URL-determination
purposes).

**Follow-ups:**

- **Q19a.** What about a "diamond"? Say `D` is a child of both `B` and `C`, and `B` and `C` are both children of `A`.
  `D` counts under `B` and under `C`, per your answer. Under `A`, does `D` count once (distinct descendants: the usual
  meaning of "all descendants") or twice (once per path)? Proposal: once.
  **Answer:** Once.
  **Design note:** distinct counts need the transitive closure (which descendants sit under each class), not just
  per-edge sums. Compute it once at load time (the pre-caching stage, Q7) and store it as a closure table or as
  per-class counts. For a deep hierarchy of millions of classes that is tens of millions of rows, which is fine for
  SQLite but makes this the most expensive part of pre-caching.
- **Q19b.** With equivalent classes merged into one node, which IRI is canonical? Proposal: each IRI that is declared in
  the file keeps its own page, since every declared entity gets a page (Q2a), and that page notes the equivalence. The
  tree shows the merged node once, labeled by the class whose label ranks best (Q18), listing the other IRIs. Or should
  non-canonical IRIs redirect to one canonical page? If so, chosen how?
  **Answer:** Each IRI having its own page is fine, and cleaner than trying to come up with a foolproof system for
  determine which IRI is canonical.
  **Design note:** in the tree, the merged node is labeled by the best-ranked label (Q18) and links to that class's
  page. Every page in the equivalence set lists the others as links.

### Q20. Front-end approach? — `ANSWERED`

> Server-rendered Jinja plus a little htmx or vanilla JS for tree expansion and autocomplete? Or a framework? Tree
> children should load lazily on expand; full trees won't render at this scale.

**Answer:** I do not have a preference here - do what is most likely to be performant and compliant with best practices.
If more specific questions come up, ask me.
**Decision:** server-rendered Jinja pages, with htmx for lazy tree expansion and the autocomplete dropdown. No SPA
framework and no front-end build step. Every page also renders fully without JS, except where hash IRIs require it
(Q2a). Accessibility follows the WAI-ARIA patterns for the tree and the autocomplete combobox.

---

## Minor

### Q21. Package name? — `ANSWERED`

> The repo is named `browl`, but the README says "OWL-Browse". Which name should the package use?

**Answer:** Use `browl` for the package.
