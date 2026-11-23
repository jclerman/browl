# Coding standards

How code is written and changed in `browl`. This document governs code style and project policy; `AGENTS.md` adds
instructions specific to coding agents. Requirements and architecture are in [`DESIGN.md`](DESIGN.md).

## Python and tooling

- **Python ≥ 3.12.** CI tests 3.12 and 3.14. Move the newer version up once the dependencies support it.
- **Poetry ≥ 2.5.1.** Dependencies go under `[tool.poetry.dependencies]` (`[project]` declares
  `dynamic = ["dependencies"]`), with caret ranges (`^1.2`). Commit `poetry.lock`; after changing `pyproject.toml`, run
  `poetry lock` and `poetry check`.
- **Layout:** the package is `browl/` at the repo root, not a `src/` layout. Tests are in `tests/`.
- **Spike code** lives in `research/S-n/`. It is linted and formatted, but needs no docstrings, type checking or
  tests, and may `print`.
- **Make targets:** `make install`, `make doctor` (checks the tools development needs), `make lint`, `make test`,
  `make schema-docs` (regenerates the SQLite schema docs, [`SCHEMA.md`](SCHEMA.md)), `make check-schema-docs` (fails if
  they're out of date), and `make ci` (lint, the schema docs check, then test, as CI runs them).
  [`CONTRIBUTING.md`](../CONTRIBUTING.md) lists the tools.
- **Reuse before writing.** Use the standard library or a well-maintained package where one does the job. Write your
  own only for a good reason (e.g. performance, quality, trustworthiness), and say why in the PR.
- **Module constants** go together at the top of the module, after the imports and the logger: public ones first,
  then private ones, each with a docstring or a short comment. Bring an existing module into line when you next change
  its constants.

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
- **Data classes:** Pydantic checks types where data comes in from outside the code.
  - A record built by position from a database row or from a caller's input (e.g. `browl.derived.Entity`,
    `browl.search.SearchQuery`) is a frozen Pydantic dataclass (`pydantic.dataclasses.dataclass`). Its annotations,
    with bounds such as `Field(ge=0)`, then state and check what the code handles, instead of checks by hand.
  - A page's model is a frozen `BaseModel`.
  - A value built only from data already typed is a plain frozen `dataclass`.

## Tests

- pytest, in `tests/`. Use plain `assert`s and fixtures, not `unittest`-style classes.
- Test data, such as small ontologies, goes in `tests/data/`. Keep it small; large-scale checks belong to the spikes and
  P-24, not the test suite.
- Tests that need Java and `robot.jar` use the `java` fixture, e.g. to run ROBOT or the OFN conversion in a child
  process; tests that call the OWLAPI in the test process use `jvm`, which also starts the JVM. Tests that need Fuseki
  use the `fuseki_url` fixture: a Fuseki at `BROWL_TEST_FUSEKI_URL`, or one the tests start from `docker/fuseki` with
  Docker (built by the tests, unless `BROWL_TEST_FUSEKI_IMAGE` names an image already built, as CI does). `make test`
  downloads the pinned `robot.jar` once (`make robot-jar`), and on macOS the tests find Java 21 when `JAVA_HOME` is
  unset. Locally, these tests are skipped when their needs are missing; in CI they fail instead, so CI runs every test.
- Request a fixture as a parameter (`java: None`), even one used only for its side effect, so it's a reference that
  rename and navigation follow. Use `pytest.mark.usefixtures("…")` only for a whole module (`pytestmark`).
- Test what only a browser shows (scripts such as `static/hash.js`, htmx, `<noscript>` fallbacks) with Playwright
  (pytest-playwright): have such tests use the `page` fixture and the `live_server` fixture, which serves an app on a
  free local port, since a browser can't reach the `TestClient`. Hold or stub responses with `page.route`, so tests of
  ordering or failures don't depend on timing. `make test` installs the headless Chromium (`make browser`); these tests
  are skipped or fail without it, like those above.
- **Marks:** a test that uses the `jvm`, `fuseki_url` or `browser` fixture (which `page` uses), directly or through
  another fixture, gets the `jvm`, `fuseki` or `browser` mark for you, for `-m` to select by (e.g.
  `-m "not jvm and not fuseki"`). Don't write these marks by hand: collection fails on one without its fixture.
- **Parallel runs:** `make test` runs the tests in several processes (pytest-xdist), each in any order, so a test
  mustn't depend on another having run, or share state outside its process: use free ports (`port=0`), temporary
  directories and unique dataset names. Session fixtures run once per process.
- **Doctests:** examples in the package's docstrings run as tests (`--doctest-modules`; `testpaths` is `tests` and
  `browl`, so `research/`, whose scripts run when imported, isn't collected). Keep them short and working.
- **Coverage:** CI fails below 90% line coverage of the `browl` package.

## Documentation

- **Docstrings:** PEP 257-compliant, Google style (`Args:`, `Returns:`, `Raises:`). A function's summary line is
  imperative ("Return…", "Check that…"); ruff's D401 catches some violations.
  Document a class's attributes in an `Attributes:` section of its docstring, not in docstrings after each attribute.
- **Keep internal documentation terse.** Don't document what the code makes obvious, or could with better naming or
  structure; improve the code instead. Comments explain why, not what.
- When a change alters a design decision, update `docs/DESIGN.md` in the same PR.

## Runtime conventions

- **Logging:** the standard library's `logging`, configured in one place, with plain-text output. Modules get their
  logger with `logging.getLogger(__name__)`. Don't use `print`.
- **Async:** request handlers never block the event loop. Java (OWLAPI) and SQLite calls run in the thread pool;
  Fuseki is called with an async HTTP client.
- **SQL:** the standard library's `sqlite3` with hand-written SQL; no ORM. The derived database is rebuilt on every
  load, and when its build keys change, so it needs no migrations ([`DESIGN.md`](DESIGN.md) §2.5, Build keys);
  pre-caching hooks follow [`SCHEMA.md`](SCHEMA.md)'s rules for the keys. The persistent database (job state, admins)
  records a schema version and migrates forward from it. Every table and column is described in `.tbls/`, from which
  `make schema-docs` generates `docs/schema/` ([`SCHEMA.md`](SCHEMA.md)).

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
