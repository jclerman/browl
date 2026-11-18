"""Build browl's two SQLite databases, empty, from their schemas in code, for tbls to document (`make schema-docs`).

Usage: `poetry run python scripts/schema_dbs.py DIRECTORY`. Writes `derived.sqlite3`, from `browl.derived.SCHEMA` and
every statement in `INDEXES`, and `state.sqlite3`, by opening a `JobStore` on a new file so the real migrations run.
Any earlier files of those names in the directory are replaced.
"""

import sys
from contextlib import closing
from pathlib import Path
from sqlite3 import connect

from browl import derived
from browl.jobs import JobStore


def build_derived(path: Path) -> None:
    """Create the derived database's tables and indexes, empty, at `path`."""
    with closing(connect(path, isolation_level=None)) as connection:
        connection.executescript(derived.SCHEMA)
        for statements in derived.INDEXES.values():
            for statement in statements:
                connection.execute(statement)


def build_state(path: Path) -> None:
    """Create the state database at `path`, migrated to the latest schema version."""
    JobStore(path)


def main(directory: Path) -> None:
    """Build both databases in `directory`, creating it if needed."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, function in [("derived", build_derived), ("state", build_state)]:
        path = directory / f"{name}.sqlite3"
        path.unlink(missing_ok=True)
        function(path)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} DIRECTORY")
    main(Path(sys.argv[1]))
