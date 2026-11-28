# browl manual

browl hosts one OWL ontology as a browsable website: every declared entity gets a page at a URL implied by its IRI.
This manual covers browl 0.1.0. It has a part for each of the three kinds of people who deal with a browl site:

| Part | For | Covers |
| --- | --- | --- |
| [Operating browl](operator.md) | **Operators**, who deploy and configure the service | Requirements, deploying with Docker Compose or Kubernetes, settings, HTTPS, upgrades, backups, troubleshooting |
| [Administering browl](admin.md) | **Admins**, who sign in to manage the ontology | Signing in, uploading an ontology, reasoning, following a load, imports, managing admins |
| [Using browl](user.md) | **Users**, who browse the ontology | Entity pages, hierarchy trees, search, URLs |

Users need no account. Admins sign in with a Google account that an operator or another admin has listed. One person
can of course be all three.

Other documents:

- [`AUTHENTICATION.md`](../AUTHENTICATION.md): registering browl's Google sign-in, step by step. Operators need it.
- [`DESIGN.md`](../DESIGN.md): requirements, technical decisions and the plan. The manual describes what browl does;
  the design doc says why, and what's still to come.
- [`CONTRIBUTING.md`](../../CONTRIBUTING.md): setting up to develop browl.
