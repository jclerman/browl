"""Tests for the derived database: its schema, atomic swap, cleanup and readers."""

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
    SCHEMA_VERSION,
    DerivedReader,
    Meta,
    NotCurrentError,
    build,
    connect,
    create_indexes,
    derived_path,
    is_current,
    read_meta,
    remove_others,
)
from browl.jobs import Reasoner

TABLES = {"entity", "name_value", "edge", "equiv_group", "counts", "hash_prefix", "prefix", "meta"}
INDEXES = {
    "entity_iri_type",
    "entity_path",
    "name_value_entity",
    "name_value_normalized",
    "edge_child",
    "equiv_group_representative",
}


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
    with build(data_dir, meta(generation)) as connection:
        connection.execute(
            "INSERT INTO entity (iri, type, path, fragment) VALUES (?, 'class', '/example.org/onto', 'A')", (iri,)
        )


def entity_iris(data_dir: Path, generation: int) -> list[str]:
    """Return the entity IRIs in a generation's derived database."""
    with closing(connect(data_dir, generation)) as connection:
        return [row[0] for row in connection.execute("SELECT iri FROM entity")]


def test_build_creates_schema_and_meta(tmp_path: Path) -> None:
    """A build creates every table and the indexes, stores its meta, and keeps the rows written."""
    make(tmp_path, 3)
    assert sorted(path.name for path in (tmp_path / DERIVED_DIR).iterdir()) == ["3.sqlite3"]
    connection = connect(tmp_path, 3)
    try:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
        assert names >= TABLES
        assert index_names(connection) == INDEXES
        assert read_meta(connection) == meta(3)
        assert connection.execute("SELECT schema_version FROM meta").fetchone()[0] == SCHEMA_VERSION
    finally:
        connection.close()
    assert entity_iris(tmp_path, 3) == ["http://example.org/onto#A"]


def test_build_pragmas(tmp_path: Path) -> None:
    """A build runs with a large page cache, and helper threads to sort."""
    with build(tmp_path, meta(1)) as connection:
        assert connection.execute("PRAGMA cache_size").fetchone()[0] == -256 * 1024
        assert connection.execute("PRAGMA threads").fetchone()[0] == 4


def test_meta_without_iris(tmp_path: Path) -> None:
    """An ontology without IRIs stores none."""
    plain = Meta(generation=1, reasoner=Reasoner.NONE, loaded_at=datetime(2026, 1, 1, tzinfo=UTC))
    with build(tmp_path, plain):
        pass
    with closing(connect(tmp_path, 1)) as connection:
        assert read_meta(connection) == plain


@pytest.mark.parametrize(
    ("version_iri", "version_info", "expected"),
    [
        ("http://example.org/onto/1.0", "1.0-dev", "http://example.org/onto/1.0"),
        (None, "4.4-dev", "4.4-dev"),
        (None, None, None),
    ],
    ids=["version-iri", "version-info", "neither"],
)
def test_meta_version(version_iri: str | None, version_info: str | None, expected: str | None) -> None:
    """The version shown is the version IRI, else the version info."""
    when = datetime(2026, 1, 1, tzinfo=UTC)
    assert Meta(1, Reasoner.NONE, when, version_iri=version_iri, version_info=version_info).version == expected


def test_build_commits_after_block_commits(tmp_path: Path) -> None:
    """Rows written after the block commits its own transaction are kept too."""
    with build(tmp_path, meta(1)) as connection:
        connection.execute("COMMIT")
        connection.execute("INSERT INTO hash_prefix (path) VALUES ('/example.org/onto')")
    with closing(connect(tmp_path, 1)) as connection:
        assert connection.execute("SELECT path FROM hash_prefix").fetchall() == [("/example.org/onto",)]


def test_failed_build_leaves_previous_file(tmp_path: Path) -> None:
    """A build that fails deletes its temporary file and leaves the file it would have replaced."""
    make(tmp_path, 1, "http://example.org/old")
    with pytest.raises(RuntimeError, match="boom"), build(tmp_path, meta(1)) as connection:
        connection.execute("INSERT INTO entity (iri, type, path) VALUES ('http://example.org/new', 'class', '/x')")
        raise RuntimeError("boom")
    assert sorted(path.name for path in (tmp_path / DERIVED_DIR).iterdir()) == ["1.sqlite3"]
    assert entity_iris(tmp_path, 1) == ["http://example.org/old"]


def test_failed_index_leaves_no_file(tmp_path: Path) -> None:
    """A build whose indexes can't be created, e.g. a duplicate entity, fails and leaves no file."""
    with pytest.raises(sqlite3.IntegrityError), build(tmp_path, meta(1)) as connection:
        for _ in range(2):
            connection.execute("INSERT INTO entity (iri, type, path) VALUES ('http://example.org/a', 'class', '/a')")
    assert list((tmp_path / DERIVED_DIR).iterdir()) == []


def test_create_indexes(tmp_path: Path) -> None:
    """A build can create a table's indexes once it has filled it, and use them; the build creates the rest."""
    with build(tmp_path, meta(1)) as connection:
        connection.execute("INSERT INTO entity (iri, type, path) VALUES ('http://example.org/a', 'class', '/a')")
        create_indexes(connection, "entity")
        assert index_names(connection) == {"entity_iri_type", "entity_path"}
        plan = connection.execute("EXPLAIN QUERY PLAN SELECT id FROM entity WHERE iri = 'x' AND type = 'class'")
        assert "entity_iri_type" in " ".join(row[-1] for row in plan)
        create_indexes(connection, "entity")  # once only
        create_indexes(connection, "prefix")  # none
        with pytest.raises(ValueError, match="no table 'entities'"):
            create_indexes(connection, "entities")
    with closing(connect(tmp_path, 1)) as connection:
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
    assert is_current(tmp_path, 1)


def test_remove_others(tmp_path: Path) -> None:
    """Every derived database but the one kept, and every temporary file, is deleted; other files stay."""
    for generation in (1, 2, 3):
        make(tmp_path, generation)
    directory = tmp_path / DERIVED_DIR
    (directory / "4.sqlite3.tmp").write_text("")
    (directory / "notes.txt").write_text("")
    remove_others(tmp_path, 2)
    assert sorted(path.name for path in directory.iterdir()) == ["2.sqlite3", "notes.txt"]
    remove_others(tmp_path, None)
    assert sorted(path.name for path in directory.iterdir()) == ["notes.txt"]


def test_remove_others_without_directory(tmp_path: Path) -> None:
    """Cleaning up before any build does nothing."""
    remove_others(tmp_path, 1)
    assert not (tmp_path / DERIVED_DIR).exists()


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS's SQLite may fail reads of a deleted file")
def test_open_file_survives_removal(tmp_path: Path) -> None:
    """A connection open on a generation's file can still read it after cleanup deletes it."""
    make(tmp_path, 1)
    with closing(connect(tmp_path, 1)) as connection:
        make(tmp_path, 2)
        remove_others(tmp_path, 2)
        assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 1


def test_connection_is_read_only(tmp_path: Path) -> None:
    """A reader can't write to the derived database."""
    make(tmp_path, 1)
    with closing(connect(tmp_path, 1)) as connection, pytest.raises(sqlite3.OperationalError):
        connection.execute("DELETE FROM entity")


def test_is_current(tmp_path: Path) -> None:
    """A generation's database is current once built."""
    assert not is_current(tmp_path, 1)
    make(tmp_path, 1)
    assert is_current(tmp_path, 1)
    assert not is_current(tmp_path, 2)


def test_rejects_other_generation(tmp_path: Path) -> None:
    """A file built for another generation, e.g. renamed, isn't current."""
    make(tmp_path, 1)
    derived_path(tmp_path, 1).rename(derived_path(tmp_path, 2))
    with pytest.raises(NotCurrentError, match="generation 1, not 2"):
        connect(tmp_path, 2)
    assert not is_current(tmp_path, 2)


def test_rejects_other_schema_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file built by a browl with another schema version isn't current."""
    make(tmp_path, 1)
    monkeypatch.setattr(browl.derived, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    with pytest.raises(NotCurrentError, match="schema version"):
        connect(tmp_path, 1)


def test_rejects_missing_file(tmp_path: Path) -> None:
    """A generation with no file isn't current."""
    with pytest.raises(NotCurrentError, match="doesn't exist"):
        connect(tmp_path, 1)


@pytest.mark.parametrize(
    ("script", "match"),
    [
        ("CREATE TABLE other (x);", "can't be read"),
        ("CREATE TABLE meta (schema_version, generation);", "no meta row"),
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
        connect(tmp_path, 1)


def test_rejects_non_database(tmp_path: Path) -> None:
    """A file that isn't SQLite isn't current."""
    path = derived_path(tmp_path, 1)
    path.parent.mkdir(parents=True)
    path.write_text("not a database" * 100)
    with pytest.raises(NotCurrentError, match="can't be read"):
        connect(tmp_path, 1)


def test_reader_reuses_and_switches(tmp_path: Path) -> None:
    """A reader keeps a thread's connection for the same generation, and opens another for a new one."""
    make(tmp_path, 1)
    make(tmp_path, 2)
    reader = DerivedReader(tmp_path)
    first = reader.connection(1)
    assert reader.connection(1) is first
    second = reader.connection(2)
    assert second is not first
    assert read_meta(second).generation == 2
    with pytest.raises(sqlite3.ProgrammingError):
        first.execute("SELECT 1")  # closed when the reader switched
    reader.close()
    with pytest.raises(sqlite3.ProgrammingError):
        second.execute("SELECT 1")
    reader.close()


def test_reader_rejects_not_current(tmp_path: Path) -> None:
    """A reader asked for a generation without a current database raises, and holds no connection."""
    make(tmp_path, 1)
    reader = DerivedReader(tmp_path)
    reader.connection(1)
    with pytest.raises(NotCurrentError):
        reader.connection(2)
    assert reader.connection(1) is not None


def test_reader_connection_per_thread(tmp_path: Path) -> None:
    """Each thread gets its own connection."""
    make(tmp_path, 1)
    reader = DerivedReader(tmp_path)
    connections: list[sqlite3.Connection] = []

    def read() -> None:
        connections.append(reader.connection(1))

    thread = threading.Thread(target=read)
    thread.start()
    thread.join()
    assert reader.connection(1) is not connections[0]
