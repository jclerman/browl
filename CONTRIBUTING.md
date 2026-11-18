# Contributing to browl

## What you need

Required:

| Tool | Version | For |
| --- | --- | --- |
| Git | | |
| Python | 3.12+ | CI tests 3.12 and 3.14 |
| [Poetry](https://python-poetry.org/docs/#installation) | 2.5.1+ | The venv and its dependencies, from `poetry.lock` |
| curl, shasum, tar | | `make test` downloads the pinned `robot.jar` and Jena tools with them, and `make ci` the pinned [tbls](https://github.com/k1LoW/tbls) |

Optional locally. CI has them all, and there the tests that need them fail instead of being skipped:

| Tool | Version | For |
| --- | --- | --- |
| Java | 21 | Tests marked `jvm` (the OWLAPI, through JPype). On macOS the tests find an installed Java 21 when `JAVA_HOME` is unset; elsewhere, set `JAVA_HOME`. |
| Docker, with Compose | | Tests marked `fuseki`, which start Fuseki in Docker unless `BROWL_TEST_FUSEKI_URL` points at one; running the app |
| [ShellCheck](https://www.shellcheck.net/) | | actionlint checks the workflows' `run:` steps with it |

pre-commit fetches the linters it runs, with their own runtimes, the first time it runs them.

## Setting up

```sh
make install   # create the venv from poetry.lock, and install the pre-commit git hook
make doctor    # check the tools above, and say what's missing
make ci        # lint and test, as CI does
make help      # list all targets
```

The first `make test` downloads `robot.jar` and Jena's tools into `.cache/`, at the versions the `Dockerfile` pins, and
the first `make ci` (or `make tbls`) downloads tbls, at the version the `Makefile` pins, for the SQLite schema docs
([`docs/SCHEMA.md`](docs/SCHEMA.md)). The git hook runs the linters on each commit; `make lint` runs them on every
file.

## Running the app

```sh
cp .env.example .env    # development settings: sign-in off
docker compose up --build
```

The site is then at <http://localhost:8000/>. The settings are described in `.env.example` and
[`docs/DESIGN.md`](docs/DESIGN.md) §2.12.

## Making changes

- [`docs/CODING-STANDARDS.md`](docs/CODING-STANDARDS.md): code style, tests, and the git and pull-request workflow.
- [`docs/DESIGN.md`](docs/DESIGN.md): requirements, decisions and the plan. Each work item there has a GitHub issue;
  read the design sections it cites before starting.
- [`AGENTS.md`](AGENTS.md): instructions for coding agents.
