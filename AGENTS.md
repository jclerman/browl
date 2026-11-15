# AGENTS.md

Instructions for coding agents working on browl.

## Project

browl hosts an OWL ontology as a browsable website: every declared entity gets a page at a URL implied by its IRI.

- **`docs/DESIGN.md`** is the source of truth for requirements, technical decisions and the implementation plan. Read the relevant sections before starting a work item.
- **`claude/DESIGN-SESSION-STATUS.md`** is the design's historical question log. Don't update it; where it conflicts with `docs/DESIGN.md`, the design doc wins.
- A planned coding-standards doc will override this file where they conflict.

Key decisions, detailed in the design doc:
- FastAPI and Jinja2, with htmx for tree expansion, tabs and autocomplete. No SPA framework or front-end build step.
- The OWLAPI, loaded in-process from `robot.jar` via JPype, holds the ontology in memory and renders Manchester syntax.
- Reasoning (ELK, HermiT) runs ROBOT as a subprocess.
- Load-time computed data and the search index live in SQLite. No triple store.

## Layout and tooling

- The Python package is `browl/` at the repo root, not a `src/` layout.
- **Poetry ≥ 2.5.1.** Dependencies go under `[tool.poetry.dependencies]` (`[project]` declares `dynamic = ["dependencies"]`).
- **Python ≥ 3.12.**
- Commit `poetry.lock`. After changing `pyproject.toml`, run `poetry lock` and `poetry check`.
- Agent shells may lack the Poetry venv, so run tools and commits via `poetry run` (e.g. `poetry run git commit`); the local mypy pre-commit hook needs `mypy` on PATH.

## Code

- **Reuse before writing.** Use the standard library or a well-maintained package where one does the job. Write your own only for a good reason (e.g. performance, quality, trustworthiness), and say why in the PR.
- **Typing:** checked with mypy. Fully type every function and method signature. Elsewhere, annotate where it isn't redundant or unidiomatic (e.g. not `x = 0`).
- **Tests:** pytest, in `tests/`. Use plain `assert`s and fixtures, not `unittest`-style classes.
- **Docstrings:** PEP 257-compliant, Google style (`Args:`, `Returns:`, `Raises:`).
- **Keep internal documentation terse.** Don't document what the code makes obvious, or could with better naming or structure; improve the code instead. Comments explain why, not what.

## Git and PRs

- Never commit to `main`; make each change on its own branch and merge it via PR.
- Before merging, rebase onto `main` (don't merge `main` in) and push with `git push --force-with-lease`.
- PRs are squash-merged, so the PR title becomes the commit subject on `main`; write it as one.
- Don't add attribution or co-author lines (e.g. `Co-Authored-By:`) for the agent to commit messages.
- Cite design-doc IDs in PRs and commits where relevant: requirements (`U-3`, `A-6`), work items (`P-15`), spikes (`S-1`).
- When a change alters a design decision, update `docs/DESIGN.md` in the same PR.
