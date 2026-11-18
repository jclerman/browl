"""The derived SQLite database (DESIGN.md §2.5): whole-ontology data computed once per load.

Each generation has its own file, `DATA_DIR/derived/{generation}.sqlite3`. `build` writes it to a temporary file beside
it and renames it into place only once it's complete, so a file at the final name is always whole, and is never written
again. A reader never sees a file change under it: a new load writes a new file. On Linux, deleting an old one
(`remove_others`) leaves it readable to connections already open on it; macOS's SQLite may fail their reads instead
(`SQLITE_IOERR_VNODE`).

The database is rebuilt on every load, so it has no migrations: a browl whose `SCHEMA_VERSION` differs treats the file
as not current (`is_current`), and the caller rebuilds it.
"""

import asyncio
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from browl.jobs import Reasoner

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
"""The derived database's schema version; raise it whenever `SCHEMA` or `INDEXES` changes, forcing a rebuild."""

DERIVED_DIR = "derived"
"""The directory in `DATA_DIR` that holds the derived databases."""

_SUFFIX = ".sqlite3"
_TEMP_SUFFIX = ".tmp"
_FILE_NAME = re.compile(r"(\d+)\.sqlite3")

SCHEMA = """
CREATE TABLE meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    ontology_iri TEXT,
    version_iri TEXT,
    reasoner TEXT NOT NULL,
    loaded_at TEXT NOT NULL
);
CREATE TABLE entity (
    id INTEGER PRIMARY KEY,
    iri TEXT NOT NULL,
    type TEXT NOT NULL,
    path TEXT NOT NULL,
    fragment TEXT,
    display_name TEXT,
    curie TEXT
);
CREATE TABLE name_value (
    entity INTEGER NOT NULL,
    property TEXT NOT NULL,
    value TEXT NOT NULL,
    normalized TEXT NOT NULL,
    language TEXT,
    direct INTEGER NOT NULL
);
CREATE TABLE edge (
    hierarchy TEXT NOT NULL,
    parent INTEGER NOT NULL,
    child INTEGER NOT NULL,
    PRIMARY KEY (hierarchy, parent, child)
) WITHOUT ROWID;
CREATE TABLE equiv_group (
    entity INTEGER PRIMARY KEY,
    representative INTEGER NOT NULL
);
CREATE TABLE counts (
    hierarchy TEXT NOT NULL,
    node INTEGER NOT NULL,
    children INTEGER NOT NULL,
    descendants INTEGER NOT NULL,
    PRIMARY KEY (hierarchy, node)
) WITHOUT ROWID;
CREATE TABLE hash_prefix (
    path TEXT PRIMARY KEY
) WITHOUT ROWID;
CREATE TABLE prefix (
    prefix TEXT PRIMARY KEY,
    namespace TEXT NOT NULL
) WITHOUT ROWID;
"""
"""The tables (§2.5). Entities are referred to by `entity.id`, a dense integer (e.g. for descendant bitmaps); an IRI
declared with several types (punning) has a row for each. The `meta` table's `schema_version` and `generation` columns
must keep their names, since `connect` reads them from a file of any schema version."""

INDEXES = """
CREATE UNIQUE INDEX entity_iri_type ON entity (iri, type);
CREATE INDEX entity_path ON entity (path, fragment);
CREATE INDEX name_value_entity ON name_value (entity);
CREATE INDEX name_value_normalized ON name_value (normalized);
CREATE INDEX edge_child ON edge (hierarchy, child);
CREATE INDEX equiv_group_representative ON equiv_group (representative);
"""
"""The secondary indexes, created once the tables are filled, which is faster than keeping them up to date."""


@dataclass(frozen=True)
class Meta:
    """What the derived database was built from.

    Attributes:
        generation: The load job's generation number.
        reasoner: The reasoner the load ran.
        loaded_at: When the load job started; a rebuild after a restart keeps it (`browl.precache.pre_cache`).
        ontology_iri: The ontology's IRI; None if it has none.
        version_iri: Its version IRI; None if it has none.
    """

    generation: int
    reasoner: Reasoner
    loaded_at: datetime
    ontology_iri: str | None = None
    version_iri: str | None = None


class NotCurrentError(Exception):
    """The derived database for a generation is missing, unreadable, or built for another generation or schema."""


def derived_dir(data_dir: Path) -> Path:
    """Return the directory that holds the derived databases."""
    return data_dir / DERIVED_DIR


def derived_path(data_dir: Path, generation: int) -> Path:
    """Return the path of a generation's derived database."""
    return derived_dir(data_dir) / f"{generation}{_SUFFIX}"


@contextmanager
def build(data_dir: Path, meta: Meta) -> Iterator[sqlite3.Connection]:
    """Build a generation's derived database, and swap it in atomically once the block exits cleanly.

    The block fills the tables through the connection yielded, which is in a transaction; it needn't commit. On a clean
    exit the secondary indexes are created, the file is synced to disk and renamed to `derived_path`, replacing any
    file there. If the block, or any of that, fails, the temporary file is deleted and any file at `derived_path` is
    left as it was. The connection may be used only on the thread that called `build`.

    Args:
        data_dir: The data directory.
        meta: What the database is built from; stored in its `meta` table.

    Yields:
        A connection to the new database, with its tables created and empty but for `meta`.
    """
    path = derived_path(data_dir, meta.generation)
    temp = path.with_name(path.name + _TEMP_SUFFIX)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp.unlink(missing_ok=True)
    try:
        with closing(sqlite3.connect(temp, isolation_level=None)) as connection:
            # The file is thrown away if anything fails, so there's no need for a journal or for syncing until the end.
            connection.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;")
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT INTO meta (id, schema_version, generation, ontology_iri, version_iri, reasoner, loaded_at)"
                " VALUES (1, ?, ?, ?, ?, ?, ?)",
                (
                    SCHEMA_VERSION,
                    meta.generation,
                    meta.ontology_iri,
                    meta.version_iri,
                    str(meta.reasoner),
                    meta.loaded_at.isoformat(),
                ),
            )
            connection.execute("BEGIN")
            yield connection
            if connection.in_transaction:
                connection.execute("COMMIT")
            connection.executescript(INDEXES)
            connection.executescript("PRAGMA analysis_limit = 1000; ANALYZE;")
        _fsync(temp)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    _fsync(path.parent)
    logger.info("Built the derived database for generation %d", meta.generation)


def _fsync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def remove_others(data_dir: Path, keep: int | None) -> None:
    """Delete every derived database but `keep`'s, and every temporary file a build left.

    The caller holds the job lock, so no build is running.

    Args:
        data_dir: The data directory.
        keep: The generation whose database stays; None to delete them all.
    """
    directory = derived_dir(data_dir)
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        match = _FILE_NAME.fullmatch(path.name)
        if path.name.endswith(_TEMP_SUFFIX) or (match is not None and int(match[1]) != keep):
            logger.info("Deleting %s", path)
            path.unlink(missing_ok=True)


def connect(data_dir: Path, generation: int) -> sqlite3.Connection:
    """Open a generation's derived database read-only, after checking that it's current.

    The file is opened as immutable, so reads take no locks: `build` never writes a file at its final name.

    Raises:
        NotCurrentError: The file is missing or unreadable, or was built for another generation or schema version.
    """
    path = derived_path(data_dir, generation)
    if not path.is_file():
        raise NotCurrentError(f"{path} doesn't exist")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True, check_same_thread=False)
    try:
        row = connection.execute("SELECT schema_version, generation FROM meta").fetchone()
        if row is None:
            raise NotCurrentError(f"{path} has no meta row")
        schema_version, stored_generation = row
        if schema_version != SCHEMA_VERSION:
            raise NotCurrentError(f"{path} has schema version {schema_version}, not {SCHEMA_VERSION}")
        if stored_generation != generation:
            raise NotCurrentError(f"{path} was built for generation {stored_generation}, not {generation}")
    except sqlite3.Error as error:
        connection.close()
        raise NotCurrentError(f"{path} can't be read: {error}") from error
    except BaseException:
        connection.close()
        raise
    return connection


def is_current(data_dir: Path, generation: int) -> bool:
    """Return whether a generation's derived database exists and was built for it by this schema version."""
    try:
        connection = connect(data_dir, generation)
    except NotCurrentError as error:
        logger.info("The derived database isn't current: %s", error)
        return False
    connection.close()
    return True


def read_meta(connection: sqlite3.Connection) -> Meta:
    """Return the `meta` row of a derived database."""
    generation, reasoner, loaded_at, ontology_iri, version_iri = connection.execute(
        "SELECT generation, reasoner, loaded_at, ontology_iri, version_iri FROM meta"
    ).fetchone()
    return Meta(
        generation=generation,
        reasoner=Reasoner(reasoner),
        loaded_at=datetime.fromisoformat(loaded_at),
        ontology_iri=ontology_iri,
        version_iri=version_iri,
    )


class DerivedReader:
    """Read-only connections to the derived database, one per thread, for the generation last asked for.

    Each thread keeps its own connection, since a `sqlite3.Connection` mustn't be used by two threads at once. Asking
    for another generation closes the thread's connection and opens that generation's file.

    Args:
        data_dir: The data directory.
    """

    def __init__(self, data_dir: Path) -> None:
        """Hold no connections yet."""
        self.data_dir = data_dir
        self._local = threading.local()

    def connection(self, generation: int) -> sqlite3.Connection:
        """Return this thread's connection to a generation's derived database, opening it if needed.

        Raises:
            NotCurrentError: The database for the generation is not current (`connect`).
        """
        current: tuple[int, sqlite3.Connection] | None = getattr(self._local, "current", None)
        if current is not None:
            if current[0] == generation:
                return current[1]
            self.close()
        connection = connect(self.data_dir, generation)
        self._local.current = (generation, connection)
        return connection

    async def read[T](self, generation: int, function: Callable[[sqlite3.Connection], T]) -> T:
        """Return a function's result on a connection to a generation's derived database, run in the thread pool.

        Raises:
            NotCurrentError: The database for the generation is not current (`connect`).
        """
        return await asyncio.to_thread(lambda: function(self.connection(generation)))

    def close(self) -> None:
        """Close this thread's connection, if it has one."""
        current: tuple[int, sqlite3.Connection] | None = getattr(self._local, "current", None)
        self._local.current = None
        if current is not None:
            with suppress(sqlite3.Error):
                current[1].close()
