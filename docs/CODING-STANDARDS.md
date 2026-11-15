# Coding standards

How code is written and changed in `browl`. This document governs code style and project policy; `AGENTS.md` adds
instructions specific to coding agents. Requirements and architecture are in [`DESIGN.md`](DESIGN.md).

## Python and tooling

- **Python ≥ 3.12.** CI tests 3.12 and 3.14. Move the newer version up once the dependencies support it.
- **Poetry ≥ 2.5.1.** Dependencies go under `[tool.poetry.dependencies]` (`[project]` declares
  `dynamic = ["dependencies"]`), with caret ranges (`^1.2`). Commit `poetry.lock`; after changing `pyproject.toml`, run
  `poetry lock` and `poetry check`.
- **Layout:** the package is `browl/` at the repo root, not a `src/` layout. Tests are in `tests/`.
- **Make targets:** `make install`, `make lint`, `make test`, and `make ci` (lint then test, as CI runs them).
- **Reuse before writing.** Use the standard library or a well-maintained package where one does the job. Write your
  own only for a good reason (e.g. performance, quality, trustworthiness), and say why in the PR.

## Linting and formatting

`pre-commit` runs all of these; CI runs `pre-commit` on every file.

- **ruff** for linting and formatting, line length 120. Beyond ruff's defaults it enables pycodestyle (`E`, `W`), import
  sorting (`I`), pyupgrade (`UP`), bugbear (`B`), comprehensions (`C4`), no `print` (`T20`), docstrings (`D`),
  ruff's own rules (`RUF`), simplifications (`SIM`, except `SIM108`), and PEP 8 naming (`N`).
  - Python methods that implement a Java interface through JPype must use the Java method names. Mark each with
    `# noqa: N802`.
- **mypy** for type checking (see [Typing](#typing)).
- **yamllint** (strict, line length 120) and **actionlint** for YAML and GitHub Actions workflows.
- **markdownlint-cli2** for Markdown, line length 120 (tables exempt).
- HTML templates, CSS and JavaScript aren't linted yet. Add linters for them once there's more than a little of each.

## Typing

- Checked with mypy (`disallow_untyped_defs`); `tests/` is not checked.
- Fully type every function and method signature. Elsewhere, annotate where it isn't redundant or unidiomatic (e.g. not
  `x = 0`).
- Java objects reached through JPype are `Any` to mypy. Keep them inside the bridge modules, and give the functions that
  wrap them Python types.

## Tests

- pytest, in `tests/`. Use plain `assert`s and fixtures, not `unittest`-style classes.
- Test data, such as small ontologies, goes in `tests/data/`. Keep it small; large-scale checks belong to the spikes and
  P-24, not the test suite.
- Mark tests that need a JVM and `robot.jar` with `@pytest.mark.jvm`. CI runs every test; locally, `jvm` tests are
  skipped when Java isn't available. (The skip logic and Java in CI arrive with P-4, the first such test.)
- **Coverage:** CI fails below 90% line coverage of the `browl` package.

## Documentation

- **Docstrings:** PEP 257-compliant, Google style (`Args:`, `Returns:`, `Raises:`).
- **Keep internal documentation terse.** Don't document what the code makes obvious, or could with better naming or
  structure; improve the code instead. Comments explain why, not what.
- When a change alters a design decision, update `docs/DESIGN.md` in the same PR.

## Runtime conventions

- **Logging:** the standard library's `logging`, configured in one place, with plain-text output. Modules get their
  logger with `logging.getLogger(__name__)`. Don't use `print`.
- **Async:** request handlers never block the event loop. Java (OWLAPI) and SQLite calls run in the thread pool.
- **SQL:** the standard library's `sqlite3` with hand-written SQL; no ORM. The derived database is rebuilt on every
  load and needs no migrations. The persistent database (job state, admins) records a schema version and migrates
  forward from it.

## Front end

- Jinja2 autoescaping stays on. Use `|safe` only on HTML that `browl` itself built and escaped.
- Plain CSS with custom properties; no CSS framework or build step.
- JavaScript is limited to htmx and small scripts (such as the hash-IRI script), served from static files, not inline.

## Git and pull requests

- Never commit to `main`; make each change on its own branch and merge it via PR.
- **Branch names:** short and descriptive, with dashes and no slashes (e.g. `coding-standards`, `p-4-owlapi-bridge`).
- **Commit messages:** an imperative subject of about 72 characters at most, without Conventional Commits prefixes; a
  body, when needed, says what changed and why.
- Before merging, rebase onto `main` (don't merge `main` in) and push with `git push --force-with-lease`.
- PRs are squash-merged, so the PR title becomes the commit subject on `main`; write it as one.
- Cite design-doc IDs in PRs and commits where relevant: requirements (`U-3`, `A-6`), work items (`P-15`), spikes
  (`S-1`).
