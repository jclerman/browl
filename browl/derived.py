"""The derived SQLite database (DESIGN.md §2.5): whole-ontology data computed once per load.

Each generation has its own file, written whole by `build` and never again (DESIGN.md §2.4, the pre-caching stage).
It stores the build keys it was built with, one per component, and is current only if they're the ones the running
code computes (`connect`, `browl.precache.keys`); otherwise it's rebuilt. On Linux, deleting an old one
(`remove_others`) leaves it readable to connections already open on it; macOS's SQLite may fail their reads instead
(`SQLITE_IOERR_VNODE`).
"""

import asyncio
import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager, suppress
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic.dataclasses import dataclass as validated_dataclass

from browl.jobtypes import Reasoner

if TYPE_CHECKING:
    from browl.jobs import JobState

logger = logging.getLogger(__name__)

DERIVED_DIR = "derived"
"""The directory in `DATA_DIR` that holds the derived databases."""

BUILD_CACHE_KIB = 256 * 1024
"""The build's page cache: SQLite's default 2 MB would thrash on inserts in no key order into the `WITHOUT ROWID`
tables, and spill the index builds' sorts to temporary files early."""

BUILD_THREADS = 4
"""How many helper threads SQLite may use to sort, as for an index build."""

_SUFFIX = ".sqlite3"
_TEMP_SUFFIX = ".tmp"

SCHEMA = """
CREATE TABLE meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    generation INTEGER NOT NULL,
    ontology_iri TEXT,
    version_iri TEXT,
    title TEXT,
    version_info TEXT,
    reasoner TEXT NOT NULL,
    loaded_at TEXT NOT NULL
);
CREATE TABLE build_key (
    component TEXT PRIMARY KEY,
    key TEXT NOT NULL
) WITHOUT ROWID;
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
CREATE TABLE search_value (
    type TEXT NOT NULL,
    property_rank INTEGER NOT NULL,
    language_rank INTEGER NOT NULL,
    entailed INTEGER NOT NULL,
    word INTEGER NOT NULL,
    length INTEGER NOT NULL,
    normalized TEXT NOT NULL,
    value TEXT NOT NULL,
    entity INTEGER NOT NULL,
    normalized_rest TEXT,
    value_rest TEXT,
    language TEXT,
    word_start INTEGER NOT NULL,
    PRIMARY KEY (type, property_rank, language_rank, entailed, word, length, normalized, value, entity, word_start)
) WITHOUT ROWID;
CREATE TABLE search_lane (
    type TEXT NOT NULL,
    property_rank INTEGER NOT NULL,
    language_rank INTEGER NOT NULL,
    entailed INTEGER NOT NULL,
    word INTEGER NOT NULL,
    max_length INTEGER NOT NULL,
    property TEXT NOT NULL,
    PRIMARY KEY (type, property_rank, language_rank, entailed, word)
) WITHOUT ROWID;
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
CREATE TABLE root (
    hierarchy TEXT NOT NULL,
    node INTEGER NOT NULL,
    PRIMARY KEY (hierarchy, node)
) WITHOUT ROWID;
CREATE TABLE usage_root (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    owner INTEGER,
    owner_iri TEXT,
    predicate TEXT
);
CREATE TABLE usage_ref (
    entity INTEGER NOT NULL,
    root INTEGER NOT NULL,
    PRIMARY KEY (entity, root)
) WITHOUT ROWID;
CREATE TABLE usage_blank (
    entity INTEGER PRIMARY KEY,
    object INTEGER NOT NULL,
    predicate INTEGER NOT NULL
);
CREATE TABLE hash_prefix (
    path TEXT PRIMARY KEY
) WITHOUT ROWID;
CREATE TABLE prefix (
    prefix TEXT PRIMARY KEY,
    namespace TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE type_count (
    type TEXT PRIMARY KEY,
    count INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE axiom_count (
    kind TEXT PRIMARY KEY,
    count INTEGER NOT NULL
) WITHOUT ROWID;
"""
"""The tables (§2.5). Entities are referred to by `entity.id`, a dense integer (e.g. for descendant bitmaps); an IRI
declared with several types (punning) has a row for each. The `build_key` table and its columns, and `meta.generation`,
must keep their names, since `connect` reads them from a file built by any browl."""

INDEXES: dict[str, tuple[str, ...]] = {
    "entity": (
        "CREATE UNIQUE INDEX entity_iri_type ON entity (iri, type)",
        "CREATE INDEX entity_path ON entity (path, fragment)",
    ),
    "name_value": ("CREATE INDEX name_value_entity ON name_value (entity)",),
    "edge": ("CREATE INDEX edge_child ON edge (hierarchy, child)",),
    "equiv_group": ("CREATE INDEX equiv_group_representative ON equiv_group (representative)",),
    # The home page's order (`browl.home`), so it reads the first prefixes rather than sorting them all per view.
    "prefix": ("CREATE INDEX prefix_name ON prefix (prefix COLLATE NOCASE, prefix)",),
}
"""Each table's secondary indexes, created once the table is filled, which is faster than keeping them up to date
(`create_indexes`)."""


@validated_dataclass(frozen=True)
class Meta:
    """What the derived database was built from.

    The IRIs, title and version info are the upload's, unchecked: a page may link an IRI only if it's `http` or
    `https`, since autoescaping doesn't stop a `javascript:` IRI in an `href` (P-17).

    Attributes:
        generation: The load job's generation number.
        reasoner: The reasoner the load ran.
        loaded_at: When the load job started; a rebuild after a restart keeps it (`browl.precache.stage.pre_cache`).
        ontology_iri: The ontology's IRI; None if it has none.
        version_iri: Its version IRI; None if it has none.
        title: Its display name (`browl.precache.header.ontology_header`); None if it has none, and pages show the IRI.
        version_info: Its `owl:versionInfo`; None if it has none.
    """

    generation: int
    reasoner: Reasoner
    loaded_at: datetime
    ontology_iri: str | None = None
    version_iri: str | None = None
    title: str | None = None
    version_info: str | None = None


class EntityType(StrEnum):
    """An entity's type, as `entity.type` stores it."""

    ONTOLOGY = "ontology"
    CLASS = "class"
    OBJECT_PROPERTY = "object_property"
    DATA_PROPERTY = "data_property"
    ANNOTATION_PROPERTY = "annotation_property"


PROPERTY_TYPES = frozenset({EntityType.OBJECT_PROPERTY, EntityType.DATA_PROPERTY, EntityType.ANNOTATION_PROPERTY})
"""The entity types that are properties."""


class AxiomKind(StrEnum):
    """A kind of axiom the home page counts, as `axiom_count.kind` stores it (`browl.precache.axiom_counts`).

    Attributes:
        LOGICAL: A logical axiom: a class, property or individual axiom, a key, or a SWRL rule.
        DECLARATION: A declaration.
        ANNOTATION: An annotation assertion, or an annotation property's sub-property, domain or range.
        DATATYPE_DEFINITION: A datatype definition.
    """

    LOGICAL = "logical"
    DECLARATION = "declaration"
    ANNOTATION = "annotation"
    DATATYPE_DEFINITION = "datatype_definition"


class UsageRootKind(StrEnum):
    """What a root of the blank-node owner map is, as `usage_root.kind` stores it (DESIGN.md §2.5).

    Attributes:
        TRIPLE: A triple with an IRI subject, its owner, and a blank object (`owner predicate _:label`).
        AXIOM: A reified axiom (`_:label a owl:Axiom`), owned by its `owl:annotatedSource` if that's an IRI.
        GENERAL: A blank node nothing links to, with no owner: a general class axiom, an n-ary axiom, a negative
            property assertion, a SWRL rule.
    """

    TRIPLE = "triple"
    AXIOM = "axiom"
    GENERAL = "general"


TYPE_LABELS = {
    EntityType.ONTOLOGY: "Ontology",
    EntityType.CLASS: "Class",
    EntityType.OBJECT_PROPERTY: "Object property",
    EntityType.DATA_PROPERTY: "Data property",
    EntityType.ANNOTATION_PROPERTY: "Annotation property",
}
"""Each entity type's name, as pages show it."""


@validated_dataclass(frozen=True)
class Entity:
    """A row of the `entity` table.

    Attributes:
        id: The entity's number, by which the other tables refer to it.
        iri: Its IRI.
        type: Its type; an IRI declared with several types has an `Entity` for each.
        path: The path of its URL (`browl.urls.EntityURL`).
        fragment: The fragment of a hash IRI's URL; None for any other.
        display_name: Its display name (DESIGN.md §2.7): its best name value, else its CURIE, else its IRI.
        curie: Its CURIE; None if no prefix covers it.
    """

    id: int
    iri: str
    type: EntityType
    path: str
    fragment: str | None
    display_name: str | None = None
    curie: str | None = None


def entities_at(connection: sqlite3.Connection, path: str, fragment: str | None = None) -> list[Entity]:
    """Return the entities whose URL has this path and fragment, by IRI then type (the `entity_path` index).

    Several IRIs can share a URL: the `http` and `https` forms of an IRI always do, and so can IRIs that differ only in
    their host's case, or whose native and proxied URLs coincide (`browl.urls.URLMapper`). The page for such a URL lets
    the visitor choose (U-15). An IRI with several types (punning) has an entity for each.
    """
    rows = connection.execute(
        "SELECT id, iri, type, path, fragment, display_name, curie FROM entity WHERE path = ? AND fragment IS ?"
        " ORDER BY iri, type",
        (path, fragment),
    )
    return [Entity(*row) for row in rows]


def entities_with_iri(connection: sqlite3.Connection, iri: str) -> list[Entity]:
    """Return the entities with this IRI, one for each of its types, by type (the `entity_iri_type` index)."""
    rows = connection.execute(
        "SELECT id, iri, type, path, fragment, display_name, curie FROM entity WHERE iri = ? ORDER BY type", (iri,)
    )
    return [Entity(*row) for row in rows]


def is_hash_prefix(connection: sqlite3.Connection, path: str) -> bool:
    """Return whether hash entities have URLs with this path (the `hash_prefix` table)."""
    return connection.execute("SELECT 1 FROM hash_prefix WHERE path = ?", (path,)).fetchone() is not None


class NotCurrentError(Exception):
    """The derived database for a generation is missing, unreadable, or built for another generation or build keys."""


def ready_generation(state: "JobState") -> int:
    """Return a job state's ready generation, whose derived database pages read.

    Raises:
        NotCurrentError: No generation is ready.
    """
    if state.ready_generation is None:
        raise NotCurrentError("no generation is ready")
    return state.ready_generation


def derived_dir(data_dir: Path) -> Path:
    """Return the directory that holds the derived databases."""
    return data_dir / DERIVED_DIR


def derived_path(data_dir: Path, generation: int) -> Path:
    """Return the path of a generation's derived database."""
    return derived_dir(data_dir) / f"{generation}{_SUFFIX}"


@contextmanager
def build(data_dir: Path, meta: Meta, keys: Mapping[str, str]) -> Iterator[sqlite3.Connection]:
    """Build a generation's derived database, and swap it in atomically once the block exits cleanly.

    The block fills the tables through the connection yielded, which is in a transaction; it needn't commit. It creates
    a table's indexes once it has filled it (`create_indexes`), so it can use them for the rest of the build. On a clean
    exit any indexes not yet created are, the database is analyzed, and the file is synced to disk and renamed to
    `derived_path`, replacing any file there. If the block, or any of that, fails, the temporary file is deleted and
    any file at `derived_path` is left as it was. The connection may be used only on the thread that called `build`.

    Args:
        data_dir: The data directory.
        meta: What the database is built from; stored in its `meta` table.
        keys: Its build keys, by component (`browl.precache.keys.build_keys`); stored in its `build_key` table.

    Yields:
        A connection to the new database, with its tables created and empty but for `meta` and `build_key`.
    """
    path = derived_path(data_dir, meta.generation)
    temp = path.with_name(path.name + _TEMP_SUFFIX)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp.unlink(missing_ok=True)
    try:
        with closing(sqlite3.connect(temp, isolation_level=None)) as connection:
            # The file is thrown away if anything fails, so there's no need for a journal or for syncing until the end.
            connection.executescript(
                "PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;"
                f" PRAGMA cache_size = -{BUILD_CACHE_KIB}; PRAGMA threads = {BUILD_THREADS};"
            )
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT INTO meta (id, generation, ontology_iri, version_iri, title, version_info, reasoner, loaded_at)"
                " VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                (
                    meta.generation,
                    meta.ontology_iri,
                    meta.version_iri,
                    meta.title,
                    meta.version_info,
                    str(meta.reasoner),
                    meta.loaded_at.isoformat(),
                ),
            )
            connection.executemany("INSERT INTO build_key (component, key) VALUES (?, ?)", sorted(keys.items()))
            connection.execute("BEGIN")
            yield connection
            if connection.in_transaction:
                connection.execute("COMMIT")
            for table in INDEXES:
                create_indexes(connection, table)
            connection.executescript("PRAGMA analysis_limit = 1000; ANALYZE;")
        _fsync(temp)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    _fsync(path.parent)
    logger.info("Built the derived database for generation %d", meta.generation)


def create_indexes(connection: sqlite3.Connection, table: str) -> None:
    """Create a table's secondary indexes (`INDEXES`), unless they have been; do nothing for a table that has none.

    Raises:
        ValueError: The database has no such table.
    """
    if connection.execute("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)).fetchone() is None:
        raise ValueError(f"the derived database has no table {table!r}")
    # An index of browl's has SQL; one SQLite made for a constraint has none.
    query = "SELECT 1 FROM sqlite_schema WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL"
    if connection.execute(query, (table,)).fetchone() is not None:
        return
    for statement in INDEXES.get(table, ()):
        connection.execute(statement)


def _fsync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def remove_others(data_dir: Path, keep: int) -> None:
    """Delete every derived database but `keep`'s, and every temporary file a build left.

    The caller holds the job lock, so no build is running.

    Args:
        data_dir: The data directory.
        keep: The generation whose database stays.
    """
    directory = derived_dir(data_dir)
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        generation = path.stem if path.suffix == _SUFFIX else ""
        if path.suffix == _TEMP_SUFFIX or (generation.isascii() and generation.isdigit() and int(generation) != keep):
            logger.info("Deleting %s", path)
            path.unlink(missing_ok=True)


def connect(data_dir: Path, generation: int, keys: Mapping[str, str]) -> sqlite3.Connection:
    """Open a generation's derived database read-only, after checking that it's current.

    The file is opened as immutable, so reads take no locks: `build` never writes a file at its final name.

    Args:
        data_dir: The data directory.
        generation: The generation.
        keys: The build keys it must have been built with, by component: no more, no fewer.

    Raises:
        NotCurrentError: The file is missing or unreadable, or was built for another generation or with other keys.
    """
    path = derived_path(data_dir, generation)
    if not path.is_file():
        raise NotCurrentError(f"{path} doesn't exist")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True, check_same_thread=False)
    try:
        if connection.execute("SELECT 1 FROM sqlite_schema WHERE name = 'build_key'").fetchone() is None:
            raise NotCurrentError(f"{path} has no build keys, so an older browl built it")
        row = connection.execute("SELECT generation FROM meta").fetchone()
        if row is None:
            raise NotCurrentError(f"{path} has no meta row")
        if row[0] != generation:
            raise NotCurrentError(f"{path} was built for generation {row[0]}, not {generation}")
        stored = dict(connection.execute("SELECT component, key FROM build_key"))
        if stored != dict(keys):
            raise NotCurrentError(f"{path} was built with other build keys: {_differences(stored, keys)}")
    except sqlite3.Error as error:
        connection.close()
        raise NotCurrentError(f"{path} can't be read: {error}") from error
    except BaseException:
        connection.close()
        raise
    return connection


def _differences(stored: Mapping[str, str], keys: Mapping[str, str]) -> str:
    """Describe how a file's build keys differ from the ones expected."""
    differences = [
        (label, sorted(names))
        for label, names in [
            ("changed", {name for name in stored.keys() & keys.keys() if stored[name] != keys[name]}),
            ("missing", keys.keys() - stored.keys()),
            ("extra", stored.keys() - keys.keys()),
        ]
        if names
    ]
    return "; ".join(f"{label} {', '.join(names)}" for label, names in differences)


def is_current(data_dir: Path, generation: int, keys: Mapping[str, str]) -> bool:
    """Return whether a generation's derived database exists and was built for it with these build keys (`connect`)."""
    try:
        connection = connect(data_dir, generation, keys)
    except NotCurrentError as error:
        logger.info("The derived database isn't current: %s", error)
        return False
    connection.close()
    return True


def read_type_counts(connection: sqlite3.Connection) -> dict[EntityType, int]:
    """Return how many entities have each type (the `type_count` table); a type with none has no key."""
    return {EntityType(kind): count for kind, count in connection.execute("SELECT type, count FROM type_count")}


def read_axiom_counts(connection: sqlite3.Connection) -> dict[AxiomKind, int]:
    """Return how many of the ontology's own axioms there are of each kind (the `axiom_count` table)."""
    return {AxiomKind(kind): count for kind, count in connection.execute("SELECT kind, count FROM axiom_count")}


def read_meta(connection: sqlite3.Connection) -> Meta:
    """Return the `meta` row of a derived database."""
    row = connection.execute(
        "SELECT generation, reasoner, loaded_at, ontology_iri, version_iri, title, version_info FROM meta"
    ).fetchone()
    return Meta(*row)


class DerivedReader:
    """A pool of read-only connections to the derived database, for the newest generation asked for.

    A `sqlite3.Connection` mustn't be used by two threads at once, so each read takes an idle connection, or opens
    one, and puts it back when done. Once a newer generation is asked for, the older one's connections are closed: the
    idle ones then, the busy ones when their read ends. So none outlives `remove_others` deleting its file, which
    would keep the file's space in use. A read of an older generation, e.g. by a request that read the job state just
    before, opens a connection of its own and closes it after.

    Args:
        data_dir: The data directory.
        keys: The build keys a file must have (`connect`).
    """

    def __init__(self, data_dir: Path, keys: Mapping[str, str]) -> None:
        """Hold no connections yet."""
        self.data_dir = data_dir
        self.keys = dict(keys)
        self._lock = threading.Lock()
        self._generation: int | None = None  # the newest asked for
        self._idle: list[sqlite3.Connection] = []  # the newest generation's
        self._closed = False

    async def read[T](self, generation: int, function: Callable[[sqlite3.Connection], T]) -> T:
        """Return a function's result on a connection to a generation's derived database, run in the thread pool.

        Raises:
            NotCurrentError: The database for the generation is not current (`connect`).
        """
        return await asyncio.to_thread(self._read, generation, function)

    def close(self) -> None:
        """Close every connection, each busy one when its read ends; later reads close theirs after."""
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        _close_all(idle)

    def _read[T](self, generation: int, function: Callable[[sqlite3.Connection], T]) -> T:
        connection = self._take(generation)
        try:
            return function(connection)
        finally:
            self._put_back(generation, connection)

    def _take(self, generation: int) -> sqlite3.Connection:
        """Return an idle connection to a generation's database, or a new one; close older generations' idle ones."""
        stale: list[sqlite3.Connection] = []
        connection = None
        with self._lock:
            if self._generation is None or generation > self._generation:
                self._generation = generation
                stale, self._idle = self._idle, []
            elif generation == self._generation and self._idle:
                connection = self._idle.pop()
        _close_all(stale)
        return connection if connection is not None else connect(self.data_dir, generation, self.keys)

    def _put_back(self, generation: int, connection: sqlite3.Connection) -> None:
        """Keep the connection for the next read if its generation is still the newest; else close it."""
        with self._lock:
            if generation == self._generation and not self._closed:
                self._idle.append(connection)
                return
        _close_all([connection])


def _close_all(connections: Iterable[sqlite3.Connection]) -> None:
    for connection in connections:
        with suppress(sqlite3.Error):
            connection.close()
