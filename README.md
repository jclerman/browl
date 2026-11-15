# OWL-Browse

OWL-Browse (`browl`) hosts an OWL ontology as a browsable website. Every declared entity gets its own page, at a URL implied by its IRI.

**Status:** early development. The design is in [`docs/DESIGN.md`](docs/DESIGN.md); the app is not usable yet.

## Features (planned for v1)

- A home page with the ontology's metadata: IRI, version, annotations, imports, entity counts, and the reasoner applied.
- A page for each entity: its annotations, and its logical axioms in Manchester syntax, with every entity mentioned shown as a link. A Usage tab lists the axioms in which other entities refer to it.
- Collapsible hierarchy trees for classes and for each property type, with counts of immediate children and of all descendants.
- Autocomplete search over entity names, filterable by entity type and by name property, optionally including values entailed through sub-properties.
- An admin page for uploading the ontology (RDF/XML, Turtle or OWL Functional Syntax), optionally classified first with the [ELK](https://github.com/liveontologies/elk-reasoner) or [HermiT](https://github.com/owlcs/hermit-reasoner) reasoner. Admins sign in with Google.

## Architecture

- A Python web app ([FastAPI](https://github.com/fastapi/fastapi), Jinja2 templates, htmx), with no front-end build step.
- The ontology is held in memory by the OWLAPI, which runs in the app's process through [JPype](https://github.com/jpype-project/jpype). The OWLAPI also renders Manchester syntax.
- Reasoning runs [ROBOT](https://github.com/ontodev/robot) as a separate process, with time and memory limits.
- Data that needs the whole ontology (names, hierarchy counts, the search index) is computed once per load and stored in SQLite. Rendered pages are kept in a small cache.
- Deployed as a single Docker container.

## Development

Requires Python 3.12+ and Poetry 2.5.1+.

```sh
make install   # install dependencies
make ci        # lint and test, as CI does
make help      # list all targets
```

Contributors and coding agents: see [`AGENTS.md`](AGENTS.md).
