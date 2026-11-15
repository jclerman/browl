# AGENTS.md

Instructions for coding agents working on browl.

## Project

browl hosts an OWL ontology as a browsable website: every declared entity gets a page at a URL implied by its IRI.

- **`docs/DESIGN.md`** is the source of truth for requirements, technical decisions and the implementation plan. Read the relevant sections before starting a work item.
- **`claude/DESIGN-SESSION-STATUS.md`** is the question log the design came from. It is a historical record: don't update it, and where it conflicts with `docs/DESIGN.md`, the design doc wins.
- A coding-standards doc is planned. Until it exists, follow this file; where they conflict later, the standards doc wins.

Key decisions, detailed in the design doc:
- FastAPI and Jinja2, with htmx for tree expansion, tabs and autocomplete. No SPA framework and no front-end build step.
- The ontology is held in memory by the OWLAPI, loaded from `robot.jar` into the app's process through JPype. The OWLAPI also renders Manchester syntax.
- Reasoning (ELK, HermiT) runs ROBOT as a subprocess.
- Data computed at load time, and the search index, live in SQLite.
- There is no triple store.

## Layout and tooling

- The Python package lives at the repo root, in `browl/`. Don't use a `src/` layout.
- **Poetry ≥ 2.5.1.** Dependencies go under `[tool.poetry.dependencies]` (`[project]` declares `dynamic = ["dependencies"]`).
- **Python ≥ 3.12.**
- Commit `poetry.lock`. After changing `pyproject.toml`, run `poetry lock` and `poetry check`.

## Code

- **Reuse before writing.** Before implementing something, check whether a well-maintained library or a standard-library feature already does it, and use that. Write your own only for a good reason, such as performance, quality or trustworthiness, and say why in the PR.
- **Typing:** the project is checked with mypy.
  - Every function and method signature is fully typed, parameters and return.
  - Elsewhere, add explicit types wherever they aren't redundant or against common practice. Don't annotate what mypy already infers obviously, e.g. `x = 0`.
- **Docstrings:** PEP 257-compliant, Google style (`Args:`, `Returns:`, `Raises:`).
- **Keep internal documentation terse.** Don't document what the code makes obvious, or could be made to by better naming or structure; improve the code instead. Comments explain why, not what.

## Git and PRs

- Never commit to `main`. Make each change on its own branch and merge it through a pull request.
- Refer to design-doc IDs in PRs and commit messages where they apply: requirements (`U-3`, `A-6`), work items (`P-15`) and spikes (`S-1`).
- When a change alters a design decision, update `docs/DESIGN.md` in the same PR.
