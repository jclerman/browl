# AGENTS.md

Instructions for coding agents working on browl.

## Project

browl hosts an OWL ontology as a browsable website: every declared entity gets a page at a URL implied by its IRI.

- **`docs/CODING-STANDARDS.md`** governs code style and project policy: tooling, linting, typing, tests, runtime
  conventions, and the git and PR workflow. Follow it; where it and this file conflict, it wins.
- **`docs/DESIGN.md`** is the source of truth for requirements, technical decisions and the implementation plan. Read
  the relevant sections before starting a work item.
- **`claude/DESIGN-SESSION-STATUS.md`** is the design's historical question log. Don't update its content (lint fixes
  that leave the rendered text unchanged are fine); where it conflicts with `docs/DESIGN.md`, the design doc wins.

Key decisions, detailed in the design doc:

- FastAPI and Jinja2, with htmx for tree expansion, tabs and autocomplete. No SPA framework or front-end build step.
- The OWLAPI, loaded in-process from `robot.jar` via JPype, holds the ontology in memory and renders Manchester syntax.
- Reasoning (ELK, HermiT) runs ROBOT as a subprocess.
- Load-time computed data and the search index live in SQLite. No triple store.

## Agent-specific instructions

- Agent shells may lack the Poetry venv, so run tools and commits via `poetry run` (e.g. `poetry run git commit`); the
  local mypy pre-commit hook needs `mypy` on PATH.
- Don't add attribution or co-author lines (e.g. `Co-Authored-By:`) for the agent to commit messages.
