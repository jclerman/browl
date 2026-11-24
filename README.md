# OWL-Browse

![browl](browl/static/browl-wordmark.svg)

OWL-Browse (`browl`) hosts an OWL ontology as a browsable website. Every declared entity gets its own page, at a URL
implied by its IRI.

**Status:** usable, but short of v1. What's left is tracked in the implementation plan in
[`docs/DESIGN.md`](docs/DESIGN.md#3-implementation-plan).

## Features (planned for v1)

- A home page with the ontology's metadata: IRI, version, annotations, imports, entity counts, and the reasoner applied.
- A page for each entity: its annotations, and its logical axioms in Manchester syntax, with every entity mentioned
  shown as a link. A Usage tab lists the axioms in which other entities refer to it.
- Collapsible hierarchy trees for classes and for each property type, with counts of immediate children and of all
  descendants.
- Autocomplete search over entity names, filterable by entity type and by name property, optionally including values
  entailed through sub-properties.
- An admin page for uploading the ontology (RDF/XML, Turtle or OWL Functional Syntax), optionally classified first with
  the [ELK](https://github.com/liveontologies/elk-reasoner) or [HermiT](https://github.com/owlcs/hermit-reasoner)
  reasoner. Admins sign in with Google.

## Architecture

- A Python web app ([FastAPI](https://github.com/fastapi/fastapi), Jinja2 templates, htmx), with no front-end build
  step.
- The ontology is stored in [Apache Jena Fuseki](https://jena.apache.org/documentation/fuseki2/) (TDB2) and queried
  with SPARQL when a page is requested.
- Manchester syntax is rendered by the [OWLAPI](https://github.com/owlcs/owlapi), which runs in the app's process
  through [JPype](https://github.com/jpype-project/jpype).
- Reasoning runs [ROBOT](https://github.com/ontodev/robot) as a separate process, with time and memory limits, in a
  sandbox container with no network and none of the app's other files.
- Data that needs the whole ontology (names, hierarchy counts, the search index) is computed once per load and stored in
  SQLite. Rendered pages are kept in a small cache.
- Deployed with Docker Compose, as three containers: the app, Fuseki and the sandbox.

## Development

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the tools you need and how to set up, and
[`AGENTS.md`](AGENTS.md) for coding agents.

## License

browl is licensed under the [Apache License 2.0](LICENSE). Redistributions and derivative works must carry the
attribution in [`NOTICE`](NOTICE) (section 4(d) of the license).

## Citing browl

If you use browl in your work, please cite it: [`CITATION.cff`](CITATION.cff) has the details, and GitHub's "Cite this
repository" button formats them.
