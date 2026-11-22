"""The usage tables (P-27c, DESIGN.md §2.5): one file per generation, built in the background once it's live.

`UsageTables` builds a generation's table (`browl.precache.usage_table`) after its load job is ready, or at startup if
the ready generation's table isn't current, and cancels an older generation's build when a newer one goes live. A
table is written to a temporary file and renamed into place once complete, so it's read only then; a restart during
the build starts it again. Its key (`browl.precache.keys.usage_table_key`) covers its code and the derived database's
keys, so a file another browl built, or built from another derived database, isn't current and is built again.
"""

import asyncio
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from browl import derived
from browl.fuseki import FusekiClient
from browl.jobs import JobLock
from browl.loading import dataset_name
from browl.precache.keys import usage_table_key
from browl.precache.stage import on_thread
from browl.precache.usage_table import DERIVED, SCHEMA, Progress, fill_usage_table

logger = logging.getLogger(__name__)

USAGES_DIR = "usages"
"""The directory in `DATA_DIR` that holds the usage tables."""

LOCK_RETRY_S = 1.0
"""How often a build waits to take the build lock again, which another server worker's build holds."""

WRITE_SHARE = 0.25
"""The share of a build's time spent writing, after the scan, for its estimate (`UsageTables.remaining_s`): 0.23–0.27
on NCBITaxon, Uberon and HPO (P-27c)."""

BUILD_CACHE_KIB = 256 * 1024
"""The table's page cache, as the derived database's build has (`browl.derived.BUILD_CACHE_KIB`), which also bounds
the memory of the positions' sort before it spills to temporary files."""

SCRATCH_CACHE_KIB = 64 * 1024
"""The page cache of the scratch database, which the scan appends to, and the positions' sort reads in order."""

BUILD_THREADS = 4
"""How many helper threads SQLite may use to sort, as the derived database's build does."""

_SUFFIX = ".sqlite3"
_TEMP_SUFFIX = ".tmp"
_SCRATCH_SUFFIX = ".scratch.tmp"
_LOCK = "build.lock"


class TableNotReadyError(Exception):
    """A generation's usage table is missing, unreadable, or was built for another generation or key."""


def table_path(data_dir: Path, generation: int) -> Path:
    """Return the path of a generation's usage table."""
    return data_dir / USAGES_DIR / f"{generation}{_SUFFIX}"


@contextmanager
def build_file(data_dir: Path, generation: int, key: str) -> Iterator[sqlite3.Connection]:
    """Build a generation's usage table, and rename it into place once the block exits cleanly.

    The connection yielded is in a transaction, with the schema created, the `meta` row written, the generation's
    derived database attached read-only as `DERIVED`, and a scratch database for the staged rows as `scratch`, deleted
    after. If the block, or the end, fails, the temporary files are deleted and any file in place is left as it was.
    The connection may be used only on the thread that called this.
    """
    path = table_path(data_dir, generation)
    temp = path.with_name(path.name + _TEMP_SUFFIX)
    scratch = path.with_name(path.name + _SCRATCH_SUFFIX)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp.unlink(missing_ok=True)
    scratch.unlink(missing_ok=True)
    source = derived.derived_path(data_dir, generation)
    try:
        with closing(sqlite3.connect(temp.as_uri(), uri=True, isolation_level=None)) as connection:
            # Thrown away if anything fails, so neither file needs a journal or syncing until the end.
            connection.executescript(
                "PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;"
                f" PRAGMA cache_size = -{BUILD_CACHE_KIB}; PRAGMA threads = {BUILD_THREADS};"
            )
            connection.execute(f"ATTACH ? AS {DERIVED}", (f"{source.as_uri()}?mode=ro&immutable=1",))
            connection.execute("ATTACH ? AS scratch", (scratch.as_uri(),))
            connection.executescript(
                "PRAGMA scratch.journal_mode = OFF; PRAGMA scratch.synchronous = OFF;"
                f" PRAGMA scratch.cache_size = -{SCRATCH_CACHE_KIB};"
            )
            connection.executescript(SCHEMA)
            connection.execute("INSERT INTO meta (id, generation, key) VALUES (1, ?, ?)", (generation, key))
            connection.execute("BEGIN")
            yield connection
            if connection.in_transaction:
                connection.execute("COMMIT")
            connection.execute(f"DETACH {DERIVED}")
            connection.execute("DETACH scratch")
        _fsync(temp)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    finally:
        scratch.unlink(missing_ok=True)
    _fsync(path.parent)


def _fsync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def connect(data_dir: Path, generation: int, key: str) -> sqlite3.Connection:
    """Open a generation's usage table read-only and immutable, after checking that it's current.

    Raises:
        TableNotReadyError: The file is missing or unreadable, or was built for another generation or key.
    """
    path = table_path(data_dir, generation)
    if not path.is_file():
        raise TableNotReadyError(f"{path} doesn't exist")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True, check_same_thread=False)
    try:
        row = connection.execute("SELECT generation, key FROM meta").fetchone()
        if row != (generation, key):
            raise TableNotReadyError(f"{path} was built for another generation or with another key")
    except sqlite3.Error as error:
        connection.close()
        raise TableNotReadyError(f"{path} can't be read: {error}") from error
    except BaseException:
        connection.close()
        raise
    return connection


def is_current(data_dir: Path, generation: int, key: str) -> bool:
    """Return whether a generation's usage table exists and is current (`connect`)."""
    try:
        connection = connect(data_dir, generation, key)
    except TableNotReadyError:
        return False
    connection.close()
    return True


def remove_others(data_dir: Path, keep: int) -> None:
    """Delete every usage table but `keep`'s, and every temporary file a build left; the caller holds the lock.

    A file it can't delete is logged, and left.
    """
    directory = data_dir / USAGES_DIR
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        stem = path.name.removesuffix(_SCRATCH_SUFFIX).removesuffix(_TEMP_SUFFIX).removesuffix(_SUFFIX)
        temporary = path.name.endswith(_TEMP_SUFFIX)
        old = path.name.endswith(_SUFFIX) and stem.isascii() and stem.isdigit() and int(stem) != keep
        if (temporary or old) and path.is_file():
            logger.info("Deleting %s", path)
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                logger.warning("Couldn't delete %s: %s", path, error)


@dataclass(frozen=True)
class _Build:
    """A build running in this process."""

    generation: int
    task: asyncio.Task[None]
    progress: Progress


class UsageTables:
    """Builds the usage tables in the background, and reads the ready generation's.

    A build runs in the server worker that starts it, holding a lock (`JobLock` on `usages/build.lock`) so another
    worker's doesn't write the same files; it waits for one already running elsewhere. A build that fails is logged and
    not tried again until the app restarts.

    Args:
        data_dir: The data directory.
        sparql: The Fuseki client.
        derived_keys: The build keys of the derived databases the tables are built from (`browl.precache.build_keys`).
    """

    def __init__(self, data_dir: Path, sparql: FusekiClient, derived_keys: Mapping[str, str]) -> None:
        """Run no build yet."""
        self.data_dir = data_dir
        self.key = usage_table_key(derived_keys)
        self._sparql = sparql
        self._build: _Build | None = None
        self._failed: set[int] = set()
        self._lock = threading.Lock()
        self._reading: tuple[int, sqlite3.Connection, threading.Lock] | None = None  # the newest generation read

    async def start(self, generation: int, dataset_clause: str) -> None:
        """Build a generation's table in the background, unless it's current or building; cancel an older one's build.

        Args:
            generation: The generation, which is ready.
            dataset_clause: The dataset clause that makes its queries see its imports.
        """
        if self.building(generation):
            return
        await self.cancel()
        self._failed.discard(generation)
        if await asyncio.to_thread(is_current, self.data_dir, generation, self.key):
            return
        progress = Progress()
        task = asyncio.create_task(self._run(generation, dataset_clause, progress), name=f"usage table {generation}")
        self._build = _Build(generation, task, progress)

    async def cancel(self) -> None:
        """Cancel the running build, if any, and wait for it to end."""
        build, self._build = self._build, None
        if build is None or build.task.done():
            return
        logger.info("Cancelling the usage table build of generation %d", build.generation)
        build.task.cancel()
        with suppress(asyncio.CancelledError):
            await build.task

    async def wait(self) -> None:
        """Wait for the running build, if any, to end."""
        if self._build is not None:
            await asyncio.wait({self._build.task})

    async def close(self) -> None:
        """Cancel the running build, if any, and close the connection being read, once its read ends."""
        await self.cancel()
        with self._lock:
            reading, self._reading = self._reading, None
        if reading is not None:
            with reading[2], suppress(sqlite3.Error):
                reading[1].close()

    def building(self, generation: int) -> bool:
        """Return whether this process is building a generation's table."""
        return self._build is not None and self._build.generation == generation and not self._build.task.done()

    def failed(self, generation: int) -> bool:
        """Return whether a generation's build failed in this process."""
        return generation in self._failed

    def remaining_s(self, generation: int, now: float | None = None) -> float | None:
        """Return about how long a generation's build has left, in seconds; None if it isn't building or can't tell.

        The scan's rate so far gives its time left, and the writing after it takes about `WRITE_SHARE` of the whole.

        Args:
            generation: The generation.
            now: The time (`time.monotonic`); now if not given.
        """
        if self._build is None or not self.building(generation):
            return None
        progress = self._build.progress
        now = time.monotonic() if now is None else now
        if progress.scanned_at is not None:
            scan_s = progress.scanned_at - progress.started
        elif progress.triples and progress.scanned:
            scan_s = (now - progress.started) * progress.triples / progress.scanned
        else:
            return None
        return max(0.0, scan_s / (1 - WRITE_SHARE) - (now - progress.started))

    async def read[T](self, generation: int, function: Callable[[sqlite3.Connection], T]) -> T:
        """Return a function's result on a connection to a generation's table, run in the thread pool.

        Reads of the newest generation read share one connection, one at a time, as each is a range read; a read of
        an older one opens its own.

        Raises:
            TableNotReadyError: The table isn't built, or isn't current.
        """
        return await asyncio.to_thread(self._read, generation, function)

    def _read[T](self, generation: int, function: Callable[[sqlite3.Connection], T]) -> T:
        old = None
        with self._lock:  # opening a newer generation's under it, so two reads can't both open one
            reading = self._reading
            if reading is None or generation > reading[0]:
                old, reading = reading, (generation, connect(self.data_dir, generation, self.key), threading.Lock())
                self._reading = reading
        if old is not None:
            with old[2], suppress(sqlite3.Error):  # once its read, if any, ends
                old[1].close()
        if generation != reading[0]:
            with closing(connect(self.data_dir, generation, self.key)) as connection:
                return function(connection)
        with reading[2]:
            return function(reading[1])

    async def _run(self, generation: int, dataset_clause: str, progress: Progress) -> None:
        """Build a generation's table, once this process holds the build lock; log a failure."""
        lock = JobLock(self.data_dir / USAGES_DIR / _LOCK)
        try:
            (self.data_dir / USAGES_DIR).mkdir(parents=True, exist_ok=True)
            while not await asyncio.to_thread(lock.acquire):
                await asyncio.sleep(LOCK_RETRY_S)
            try:
                await asyncio.to_thread(remove_others, self.data_dir, generation)
                if await asyncio.to_thread(is_current, self.data_dir, generation, self.key):
                    return  # another worker built it meanwhile
                start = time.perf_counter()
                logger.info("Building the usage table of generation %d", generation)
                manager = build_file(self.data_dir, generation, self.key)
                async with on_thread(manager, "usage-table", interrupt=True) as db:
                    await fill_usage_table(self._sparql, dataset_name(generation), dataset_clause, db, progress)
                logger.info("Built the usage table of generation %d in %.0f s", generation, time.perf_counter() - start)
            finally:
                lock.release()
        except asyncio.CancelledError:
            logger.info("The usage table build of generation %d was cancelled", generation)
            raise
        except Exception:
            logger.exception("The usage table build of generation %d failed", generation)
            self._failed.add(generation)
