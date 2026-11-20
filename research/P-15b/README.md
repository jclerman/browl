# P-15b: axioms as a typed tree — measurements

Measurements behind P-15b ([#98](https://github.com/jclerman/browl/issues/98)): whether the axioms rendered from
`browl.owl`'s tree match the OWLAPI's Manchester rendering, and what building and rendering the tree cost. The
decisions are in [DESIGN.md §2.8](../../docs/DESIGN.md#28-entity-pages).

## Setup

- Apple M3 Pro (12 cores, 36 GB RAM), on the Mac, not in containers. Measured 2026-11-19.
- `compare.py` loads a whole ontology into the OWLAPI and, for every axiom but declarations and annotation
  assertions (without annotations), compares the OWLAPI's rendering with the tree's text, normalized as
  `tests/test_axioms.py` does (single spaces, none before a comma or closing parenthesis). It times converting and
  rendering each axiom.
- Per page: P-15's `bench.py` (updated for the nodes), against S-4's TDB2 stores in Jena Fuseki 6.2.0, as in
  [P-15's findings](../P-15/README.md).

```sh
poetry run python research/P-15b/compare.py research/S-1/data/downloads/uberon.owl --heap 6g
poetry run python research/P-15b/compare.py research/S-3/data/ncbitaxon.owl --heap 14g
poetry run python research/P-15/bench.py http://localhost:3030 ds --heap 4g \
  --iri http://purl.obolibrary.org/obo/GO_0065007 --iri http://purl.obolibrary.org/obo/NCBITaxon_9606
```

## Results

### The text matches the OWLAPI's

| Ontology | Axioms | Identical | Different by design | Unexpected |
| --- | --- | --- | --- | --- |
| Uberon | 98,296 | 98,293 | 3 SWRL rules (functional syntax, `OtherAxiom`) | 0 |
| NCBITaxon | 3,024,993 | 3,024,993 | 0 | 0 |
| Test ontology and synthetic axioms (`tests/test_axioms.py`) | about 170 | all but those below | `HasKey`, `DatatypeDefinition`, a rule | 0 |

Uberon's axioms are 79 K `SubClassOf`, 13 K `EquivalentClasses`, 5 K `DisjointClasses`, and a few hundred property
axioms; NCBITaxon's are all `SubClassOf` but 15. The test ontology and the synthetic axioms cover every node type.

Found while matching the OWLAPI's parentheses: it parenthesizes a class expression nested in another unless it's a
class, but never a `value` restriction's individual or literal, and a data range only under `not` (unless it's a
datatype), where an intersection or union, which it always writes in parentheses, gets a second pair:
`not ((<DT> and <xsd:integer>))`. A data property's equivalence is always a list (`EquivalentProperties: <a>, <b>`),
and its range takes a colon (`Range:`), unlike an object property's.

### Time

Converting an axiom into nodes and rendering its text, per axiom, in µs (whole ontology, warm):

| Ontology | p50 | p95 | OWLAPI's renderer, p50 |
| --- | --- | --- | --- |
| Uberon | 56 | 104 | 4.3 |
| NCBITaxon | 30 | 33 | — |

About three quarters of it is calls into Java (getters, IRIs); Pydantic's validation is about an eighth.

Per page, in ms (store sample, after a warm-up):

| Run | Entities | Fetch p50 / p95 | Parse and convert p50 / p95 | Total p50 / p95 | Slowest |
| --- | --- | --- | --- | --- | --- |
| Uberon | 452 | 4.7 / 9.1 | 1.8 / 3.1 | 6.6 / 13.0 | 150: GO_0065007, 147 of it fetching |
| NCBITaxon | 333 | 4.9 / 7.2 | 1.7 / 2.5 | 6.7 / 9.8 | 266–320: NCBITaxon_500585, all but 2 fetching |

- **Parsing and converting cost about 0.3–0.7 ms more than P-15's parsing and rendering** at p50 (1.4 ms on `main`
  in the same session, against 1.7 ms here, NCBITaxon). The largest page, Uberon's NCBITaxon_9606 (93 axioms), takes
  21 ms to parse and convert, 77 ms in all.
- **NCBITaxon_500585 (47.8 K subclasses) fetched in 264–318 ms** in this session, on `main` as on this branch (280
  ms), against the 44–57 ms P-15 recorded. Fetching doesn't change in P-15b; the difference is in the fetch, and is
  left to whoever revisits the lookup of blank-node roots (DESIGN.md §2.8).
