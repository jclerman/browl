"""Tests for the derived database: its schema, atomic swap, cleanup and readers."""

import asyncio
import sqlite3
import sys
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

import browl.derived
from browl.derived import (
    DERIVED_DIR,
    DerivedReader,
    Entity,
    EntityType,
    Meta,
    NotCurrentError,
    build,
    connect,
    create_indexes,
    derived_path,
    entities_with_iri,
    is_current,
    is_hash_prefix,
    read_meta,
    remove_others,
)
from browl.jobtypes import Reasoner

COLUMNS = {
    "build_key": [("component", "TEXT", 1, 1), ("key", "TEXT", 1, 0)],
    "counts": [
        ("hierarchy", "TEXT", 1, 1),
        ("node", "INTEGER", 1, 2),
        ("children", "INTEGER", 1, 0),
        ("descendants", "INTEGER", 1, 0),
    ],
    "edge": [("hierarchy", "TEXT", 1, 1), ("parent", "INTEGER", 1, 2), ("child", "INTEGER", 1, 3)],
    "entity": [
        ("id", "INTEGER", 0, 1),
        ("iri", "TEXT", 1, 0),
        ("type", "TEXT", 1, 0),
        ("path", "TEXT", 1, 0),
        ("fragment", "TEXT", 0, 0),
        ("display_name", "TEXT", 0, 0),
        ("curie", "TEXT", 0, 0),
    ],
    "equiv_group": [("entity", "INTEGER", 0, 1), ("representative", "INTEGER", 1, 0)],
    "hash_prefix": [("path", "TEXT", 1, 1)],
    "meta": [
        ("id", "INTEGER", 0, 1),
        ("generation", "INTEGER", 1, 0),
        ("ontology_iri", "TEXT", 0, 0),
        ("version_iri", "TEXT", 0, 0),
        ("title", "TEXT", 0, 0),
        ("version_info", "TEXT", 0, 0),
        ("reasoner", "TEXT", 1, 0),
        ("loaded_at", "TEXT", 1, 0),
    ],
    "annotation_value": [
        ("entity", "INTEGER", 1, 0),
        ("property", "INTEGER", 1, 0),
        ("value", "TEXT", 1, 0),
        ("language", "TEXT", 0, 0),
    ],
    "name_value": [
        ("entity", "INTEGER", 1, 0),
        ("property", "TEXT", 1, 0),
        ("value", "TEXT", 1, 0),
        ("normalized", "TEXT", 1, 0),
        ("language", "TEXT", 0, 0),
        ("direct", "INTEGER", 1, 0),
    ],
    "prefix": [("prefix", "TEXT", 1, 1), ("namespace", "TEXT", 1, 0)],
    "type_count": [("type", "TEXT", 1, 1), ("count", "INTEGER", 1, 0)],
    "axiom_count": [("kind", "TEXT", 1, 1), ("count", "INTEGER", 1, 0)],
    "common_value": [
        ("property", "INTEGER", 1, 1),
        ("value", "TEXT", 1, 2),
        ("type", "TEXT", 1, 3),
        ("normalized", "TEXT", 1, 0),
        ("entities", "INTEGER", 1, 0),
        ("of_type", "INTEGER", 1, 0),
    ],
    "search_lane": [
        ("type", "TEXT", 1, 1),
        ("property", "INTEGER", 1, 2),
        ("language_rank", "INTEGER", 1, 3),
        ("word", "INTEGER", 1, 4),
        ("max_length", "INTEGER", 1, 0),
    ],
    "search_property": [
        ("id", "INTEGER", 0, 1),
        ("iri", "TEXT", 1, 0),
        ("prose", "INTEGER", 1, 0),
        ("by_default", "INTEGER", 1, 0),
        ("value_count", "INTEGER", 1, 0),
        ("curie", "TEXT", 0, 0),
    ],
    "search_value": [
        ("type", "TEXT", 1, 1),
        ("property", "INTEGER", 1, 2),
        ("language_rank", "INTEGER", 1, 3),
        ("word", "INTEGER", 1, 4),
        ("length", "INTEGER", 1, 5),
        ("normalized", "TEXT", 1, 6),
        ("value", "TEXT", 1, 7),
        ("entity", "INTEGER", 1, 8),
        ("normalized_rest", "TEXT", 0, 0),
        ("value_rest", "TEXT", 0, 0),
        ("language", "TEXT", 0, 0),
        ("word_start", "INTEGER", 1, 9),
    ],
    "sub_property": [("property", "INTEGER", 1, 1), ("sub", "INTEGER", 1, 2)],
    "root": [("hierarchy", "TEXT", 1, 1), ("node", "INTEGER", 1, 2)],
    "usage_blank": [("entity", "INTEGER", 0, 1), ("object", "INTEGER", 1, 0), ("predicate", "INTEGER", 1, 0)],
    "usage_ref": [("entity", "INTEGER", 1, 1), ("root", "INTEGER", 1, 2)],
    "usage_root": [
        ("id", "INTEGER", 0, 1),
        ("kind", "TEXT", 1, 0),
        ("label", "TEXT", 1, 0),
        ("owner", "INTEGER", 0, 0),
        ("owner_iri", "TEXT", 0, 0),
        ("predicate", "TEXT", 0, 0),
        ("object", "TEXT", 0, 0),
    ],
}
"""Each table's columns, as (name, type, not null, place in the primary key)."""
INDEXES = {
    "entity_iri_type",
    "entity_path",
    "name_value_entity",
    "annotation_value_entity",
    "search_property_iri",
    "edge_child",
    "equiv_group_representative",
    "prefix_name",
}


KEYS = {"schema": "s1", "meta": "m1", "entities": "e1"}
"""The build keys of a test build."""


def index_names(connection: sqlite3.Connection) -> set[str]:
    """Return the names of the indexes in a database."""
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'index'")}


def meta(generation: int) -> Meta:
    """Return the meta of a test build."""
    return Meta(
        generation=generation,
        reasoner=Reasoner.ELK,
        loaded_at=datetime(2026, 11, 17, 12, 30, tzinfo=UTC),
        ontology_iri="http://example.org/onto",
        version_iri="http://example.org/onto/1.0",
        title="Onto",
        version_info="1.0-dev",
    )


def make(data_dir: Path, generation: int, iri: str = "http://example.org/onto#A") -> None:
    """Build a generation's derived database holding one entity."""
    with build(data_dir, meta(generation), KEYS) as connection:
        connection.execute(
            "INSERT INTO entity (iri, type, path, fragment) VALUES (?, 'class', '/example.org/onto', 'A')", (iri,)
        )


def entity_iris(data_dir: Path, generation: int) -> list[str]:
    """Return the entity IRIs in a generation's derived database."""
    with closing(connect(data_dir, generation, KEYS)) as connection:
        return [row[0] for row in connection.execute("SELECT iri FROM entity")]


def test_build_creates_schema_and_meta(tmp_path: Path) -> None:
    """A build creates every table, with its columns, and the indexes, stores its meta, and keeps the rows written."""
    make(tmp_path, 3)
    assert sorted(path.name for path in (tmp_path / DERIVED_DIR).iterdir()) == ["3.sqlite3"]
    connection = connect(tmp_path, 3, KEYS)
    try:
        query = "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        tables = [row[0] for row in connection.execute(query)]
        columns = {table: connection.execute(f"PRAGMA table_info({table})").fetchall() for table in tables}
        assert {table: [(n, t, nn, pk) for _, n, t, nn, _, pk in rows] for table, rows in columns.items()} == COLUMNS
        assert index_names(connection) == INDEXES
        assert read_meta(connection) == meta(3)
        assert dict(connection.execute("SELECT component, key FROM build_key")) == KEYS
    finally:
        connection.close()
    assert entity_iris(tmp_path, 3) == ["http://example.org/onto#A"]


def test_entities_with_iri(tmp_path: Path) -> None:
    """An IRI's entities are its rows, one per type, by type; an IRI no entity has has none."""
    with build(tmp_path, meta(1), KEYS) as connection:
        connection.executemany(
            "INSERT INTO entity (iri, type, path, fragment) VALUES (?, ?, ?, ?)",
            [
                ("urn:x:p", "object_property", "/_browl/iri?iri=urn%3Ax%3Ap", None),
                ("urn:x:p", "class", "/_browl/iri?iri=urn%3Ax%3Ap", None),
                ("urn:x:q", "class", "/q", None),
            ],
        )
    with closing(connect(tmp_path, 1, KEYS)) as connection:
        assert entities_with_iri(connection, "urn:x:p") == [
            Entity(2, "urn:x:p", EntityType.CLASS, "/_browl/iri?iri=urn%3Ax%3Ap", None),
            Entity(1, "urn:x:p", EntityType.OBJECT_PROPERTY, "/_browl/iri?iri=urn%3Ax%3Ap", None),
        ]
        assert entities_with_iri(connection, "urn:x:nothing") == []


def test_is_hash_prefix(tmp_path: Path) -> None:
    """A path is a hash prefix if `hash_prefix` has it."""
    with build(tmp_path, meta(1), KEYS) as connection:
        connection.execute("INSERT INTO hash_prefix (path) VALUES ('/vocab')")
    with closing(connect(tmp_path, 1, KEYS)) as connection:
        assert is_hash_prefix(connection, "/vocab")
        assert not is_hash_prefix(connection, "/vocab/")
        assert not is_hash_prefix(connection, "/other")


def test_build_pragmas(tmp_path: Path) -> None:
    """A build runs with a large page cache, and helper threads to sort."""
    with build(tmp_path, meta(1), KEYS) as connection:
        assert connection.execute("PRAGMA cache_size").fetchone()[0] == -256 * 1024
        assert connection.execute("PRAGMA threads").fetchone()[0] == 4


def test_meta_without_iris(tmp_path: Path) -> None:
    """An ontology without IRIs stores none."""
    plain = Meta(generation=1, reasoner=Reasoner.NONE, loaded_at=datetime(2026, 1, 1, tzinfo=UTC))
    with build(tmp_path, plain, KEYS):
        pass
    with closing(connect(tmp_path, 1, KEYS)) as connection:
        assert read_meta(connection) == plain


def test_build_commits_after_block_commits(tmp_path: Path) -> None:
    """Rows written after the block commits its own transaction are kept too."""
    with build(tmp_path, meta(1), KEYS) as connection:
        connection.execute("COMMIT")
        connection.execute("INSERT INTO hash_prefix (path) VALUES ('/example.org/onto')")
    with closing(connect(tmp_path, 1, KEYS)) as connection:
        assert connection.execute("SELECT path FROM hash_prefix").fetchall() == [("/example.org/onto",)]


def test_failed_build_leaves_previous_file(tmp_path: Path) -> None:
    """A build that fails deletes its temporary file and leaves the file it would have replaced."""
    make(tmp_path, 1, "http://example.org/old")
    with pytest.raises(RuntimeError, match="boom"), build(tmp_path, meta(1), KEYS) as connection:
        connection.execute("INSERT INTO entity (iri, type, path) VALUES ('http://example.org/new', 'class', '/x')")
        raise RuntimeError("boom")
    assert sorted(path.name for path in (tmp_path / DERIVED_DIR).iterdir()) == ["1.sqlite3"]
    assert entity_iris(tmp_path, 1) == ["http://example.org/old"]


def test_failed_index_leaves_no_file(tmp_path: Path) -> None:
    """A build whose indexes can't be created, e.g. a duplicate entity, fails and leaves no file."""
    with pytest.raises(sqlite3.IntegrityError), build(tmp_path, meta(1), KEYS) as connection:
        for _ in range(2):
            connection.execute("INSERT INTO entity (iri, type, path) VALUES ('http://example.org/a', 'class', '/a')")
    assert list((tmp_path / DERIVED_DIR).iterdir()) == []


def test_create_indexes(tmp_path: Path) -> None:
    """A build can create a table's indexes once it has filled it, and use them; the build creates the rest."""
    with build(tmp_path, meta(1), KEYS) as connection:
        connection.execute("INSERT INTO entity (iri, type, path) VALUES ('http://example.org/a', 'class', '/a')")
        create_indexes(connection, "entity")
        assert index_names(connection) == {"entity_iri_type", "entity_path"}
        plan = connection.execute("EXPLAIN QUERY PLAN SELECT id FROM entity WHERE iri = 'x' AND type = 'class'")
        assert "entity_iri_type" in " ".join(row[-1] for row in plan)
        create_indexes(connection, "entity")  # once only
        create_indexes(connection, "prefix")  # none
        with pytest.raises(ValueError, match="no table 'entities'"):
            create_indexes(connection, "entities")
    with closing(connect(tmp_path, 1, KEYS)) as connection:
        assert index_names(connection) == INDEXES


def test_failed_rename_leaves_no_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A build whose rename fails deletes its temporary file."""

    def fail(*args: object) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(browl.derived.os, "replace", fail)
    with pytest.raises(OSError, match="rename failed"):
        make(tmp_path, 1)
    assert list((tmp_path / DERIVED_DIR).iterdir()) == []


def test_build_replaces_stale_temp(tmp_path: Path) -> None:
    """A temporary file left by an earlier build is replaced, not built on."""
    path = derived_path(tmp_path, 1)
    path.parent.mkdir(parents=True)
    path.with_name("1.sqlite3.tmp").write_text("left over")
    make(tmp_path, 1)
    assert sorted(path.name for path in path.parent.iterdir()) == ["1.sqlite3"]
    assert is_current(tmp_path, 1, KEYS)


def test_remove_others(tmp_path: Path) -> None:
    """Every derived database but the one kept, and every temporary file, is deleted; other files stay."""
    for generation in (1, 2, 3):
        make(tmp_path, generation)
    directory = tmp_path / DERIVED_DIR
    (directory / "4.sqlite3.tmp").write_text("")
    (directory / "notes.txt").write_text("")
    (directory / "1.sqlite3-journal").write_text("")
    (directory / "x1.sqlite3").write_text("")
    remove_others(tmp_path, 2)
    assert sorted(path.name for path in directory.iterdir()) == [
        "1.sqlite3-journal",
        "2.sqlite3",
        "notes.txt",
        "x1.sqlite3",
    ]


def test_remove_others_without_directory(tmp_path: Path) -> None:
    """Cleaning up before any build does nothing."""
    remove_others(tmp_path, 1)
    assert not (tmp_path / DERIVED_DIR).exists()


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS's SQLite may fail reads of a deleted file")
def test_open_file_survives_removal(tmp_path: Path) -> None:
    """A connection open on a generation's file can still read it after cleanup deletes it."""
    make(tmp_path, 1)
    with closing(connect(tmp_path, 1, KEYS)) as connection:
        make(tmp_path, 2)
        remove_others(tmp_path, 2)
        assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 1


def test_connection_is_read_only(tmp_path: Path) -> None:
    """A reader can't write to the derived database."""
    make(tmp_path, 1)
    with closing(connect(tmp_path, 1, KEYS)) as connection, pytest.raises(sqlite3.OperationalError):
        connection.execute("DELETE FROM entity")


def test_is_current(tmp_path: Path) -> None:
    """A generation's database is current once built."""
    assert not is_current(tmp_path, 1, KEYS)
    make(tmp_path, 1)
    assert is_current(tmp_path, 1, KEYS)
    assert not is_current(tmp_path, 2, KEYS)


def test_rejects_other_generation(tmp_path: Path) -> None:
    """A file built for another generation, e.g. renamed, isn't current."""
    make(tmp_path, 1)
    derived_path(tmp_path, 1).rename(derived_path(tmp_path, 2))
    with pytest.raises(NotCurrentError, match="generation 1, not 2"):
        connect(tmp_path, 2, KEYS)
    assert not is_current(tmp_path, 2, KEYS)


@pytest.mark.parametrize(
    ("keys", "match"),
    [
        ({**KEYS, "entities": "e2"}, "changed entities$"),
        ({**KEYS, "names": "n1"}, "missing names$"),
        ({"schema": "s1", "meta": "m1"}, "extra entities$"),
        ({"schema": "s2", "meta": "m1", "names": "n1"}, "changed schema; missing names; extra entities$"),
    ],
    ids=["stale", "missing component", "extra component", "all"],
)
def test_rejects_other_build_keys(tmp_path: Path, keys: dict[str, str], match: str) -> None:
    """A file built with other build keys, by component, isn't current: one built by other code or settings."""
    make(tmp_path, 1)
    with pytest.raises(NotCurrentError, match=f"built with other build keys: {match}"):
        connect(tmp_path, 1, keys)
    assert not is_current(tmp_path, 1, keys)


def test_rejects_old_format(tmp_path: Path) -> None:
    """A file built before build keys, with a schema version in its `meta` row, isn't current."""
    path = derived_path(tmp_path, 1)
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            "CREATE TABLE meta (id INTEGER PRIMARY KEY, schema_version INTEGER, generation INTEGER);"
            " INSERT INTO meta VALUES (1, 3, 1);"
        )
    with pytest.raises(NotCurrentError, match="has no build keys, so an older browl built it"):
        connect(tmp_path, 1, KEYS)


def test_rejects_missing_file(tmp_path: Path) -> None:
    """A generation with no file isn't current."""
    with pytest.raises(NotCurrentError, match="doesn't exist"):
        connect(tmp_path, 1, KEYS)


@pytest.mark.parametrize(
    ("script", "match"),
    [
        ("CREATE TABLE build_key (component, key);", "can't be read: no such table: meta"),
        ("CREATE TABLE build_key (component, key); CREATE TABLE meta (generation);", "no meta row"),
    ],
)
def test_rejects_unreadable_file(tmp_path: Path, script: str, match: str) -> None:
    """A file without a meta row, or not a derived database at all, isn't current."""
    path = derived_path(tmp_path, 1)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(script)
    connection.close()
    with pytest.raises(NotCurrentError, match=match):
        connect(tmp_path, 1, KEYS)


def test_rejects_non_database(tmp_path: Path) -> None:
    """A file that isn't SQLite isn't current."""
    path = derived_path(tmp_path, 1)
    path.parent.mkdir(parents=True)
    path.write_text("not a database" * 100)
    with pytest.raises(NotCurrentError, match="can't be read"):
        connect(tmp_path, 1, KEYS)


def generation_of(connection: sqlite3.Connection) -> int:
    """Return the generation of the derived database a connection reads."""
    return read_meta(connection).generation


def is_closed(connection: sqlite3.Connection) -> bool:
    """Return whether a connection is closed."""
    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


async def read_together(reader: DerivedReader, generation: int, count: int) -> list[sqlite3.Connection]:
    """Return the connections of reads of a generation that run at once, so each has its own."""
    barrier = threading.Barrier(count, timeout=5)

    def wait(connection: sqlite3.Connection) -> sqlite3.Connection:
        barrier.wait()
        return connection

    return list(await asyncio.gather(*(reader.read(generation, wait) for _ in range(count))))


@pytest.mark.anyio
async def test_reader_reuses_connections(tmp_path: Path) -> None:
    """A reader gives reads that run at once their own connections, and reuses them for later reads."""
    make(tmp_path, 1)
    reader = DerivedReader(tmp_path, KEYS)
    first, second = await read_together(reader, 1, 2)
    assert first is not second
    assert generation_of(first) == generation_of(second) == 1
    assert await reader.read(1, lambda connection: connection) in (first, second)
    assert {id(connection) for connection in await read_together(reader, 1, 2)} == {id(first), id(second)}
    reader.close()
    assert is_closed(first) and is_closed(second)


@pytest.mark.anyio
async def test_reader_closes_older_generation(tmp_path: Path) -> None:
    """Once a newer generation is read, every connection to an older one is closed, a busy one when its read ends."""
    make(tmp_path, 1)
    make(tmp_path, 2)
    reader = DerivedReader(tmp_path, KEYS)
    idle = await read_together(reader, 1, 2)
    started, release = threading.Event(), threading.Event()

    def busy(connection: sqlite3.Connection) -> sqlite3.Connection:
        started.set()
        release.wait(5)
        return connection

    task = asyncio.ensure_future(reader.read(1, busy))
    await asyncio.to_thread(started.wait, 5)
    try:
        newer = await reader.read(2, lambda connection: connection)
        assert generation_of(newer) == 2
        assert [is_closed(connection) for connection in idle].count(True) == 1  # the one not in use
    finally:
        release.set()
    held = await task
    assert held in idle
    assert is_closed(held)
    # A request that saw the older generation can still read it.
    older, generation = await reader.read(1, lambda connection: (connection, generation_of(connection)))
    assert generation == 1
    assert is_closed(older)
    assert await reader.read(2, lambda connection: connection) is newer
    reader.close()
    assert is_closed(newer)


@pytest.mark.anyio
async def test_reader_rejects_not_current(tmp_path: Path) -> None:
    """A reader asked for a generation without a current database raises, and closes the older one's connections."""
    make(tmp_path, 1)
    reader = DerivedReader(tmp_path, KEYS)
    old = await reader.read(1, lambda connection: connection)
    with pytest.raises(NotCurrentError):
        await reader.read(2, generation_of)
    assert is_closed(old)
    reader.close()


@pytest.mark.anyio
async def test_reader_closed(tmp_path: Path) -> None:
    """A read after the reader is closed still works, and closes its connection after."""
    make(tmp_path, 1)
    reader = DerivedReader(tmp_path, KEYS)
    reader.close()
    connection = await reader.read(1, lambda connection: connection)
    assert is_closed(connection)
