"""The pre-caching stage: build the derived database from bulk queries to the loaded store (DESIGN.md §2.5).

The ontology header gives the `meta` row (`browl.precache.header`); then the hooks in `HOOKS` run in order, each
filling tables from its own bulk queries and writing through a `DerivedWriter`, on the build's own thread, so the
queries can stream on the event loop. The file stores its build keys (`browl.precache.keys`).
"""

import asyncio
import functools
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, asynccontextmanager, suppress
from pathlib import Path

import httpx2

from browl import derived
from browl.derived import Meta
from browl.fuseki import BulkSelectStream, FusekiClient, SparqlError, ping
from browl.job_view import format_duration
from browl.jobs import PROGRESS_S, JobContext
from browl.jobtypes import StageError
from browl.loading import dataset_name, read_upload_prefixes
from browl.precache.axiom_counts import axiom_counts
from browl.precache.core import DerivedWriter, Hook, PreCacheContext
from browl.precache.entities import entities
from browl.precache.header import header_meta
from browl.precache.hierarchies import hierarchies
from browl.precache.keys import build_keys
from browl.precache.names import names
from browl.precache.owner_map import owner_map
from browl.precache.search import search
from browl.precache.values import annotation_values
from browl.rdf import Term

logger = logging.getLogger(__name__)

FUSEKI_WAIT_S = 120.0
"""How long a rebuild after a restart waits for Fuseki to answer, as it may be starting too (`wait_for_fuseki`)."""

FUSEKI_RETRY_S = (0.5, 5.0)
"""The first and the longest wait between pings while waiting for Fuseki; each wait doubles the last."""

INTERRUPT_CHECK_STEPS = 10_000
"""How many SQLite virtual machine steps an interruptible build's statements take between checks for an interrupt."""

HOOKS: tuple[Hook, ...] = (entities, names, annotation_values, search, hierarchies, owner_map, axiom_counts)
"""The hooks the app runs, in order: a hook may read tables an earlier one filled.

A hook creates the indexes of each table it fills once it's done (`DerivedWriter.create_indexes`), so later hooks can
use them; the build creates any left at its end. Where it can, it inserts into a `WITHOUT ROWID` table in its primary
key's order, as such a table is stored in that order."""

WRITING = "writing tables"
"""A hook's phase, in its progress, while it waits for a database write, unless it's `INDEXING`."""

INDEXING = "indexing"
"""A hook's phase, in its progress, while it creates indexes (`DerivedWriter.create_indexes`)."""


async def pre_caching_stage(context: JobContext, http: httpx2.AsyncClient, hooks: Sequence[Hook] = HOOKS) -> None:
    """Build the new generation's derived database from the store just loaded, and swap it in (`pre_cache`).

    Raises:
        StageError: A query failed or returned an incomplete result, or the header query too many rows.
    """
    if context.ontology is None:  # nothing was loaded (as the loading stage skips it)
        return
    await pre_cache(context, http, hooks)


async def pre_cache(context: JobContext, http: httpx2.AsyncClient, hooks: Sequence[Hook] = HOOKS) -> None:
    """Build a job's derived database from the store Fuseki serves for its generation, and swap it in.

    The pre-caching stage runs it, and so does a rebuild after a restart (`rebuild`), with a context made from the
    stored job state: it uses only the generation, reasoner, start time (the `meta` table's load time, the same for
    both), imports and prefixes.

    Args:
        context: The job.
        http: The HTTP client for Fuseki.
        hooks: The hooks that fill the tables, run in order.

    Raises:
        StageError: A query failed or returned an incomplete result, or the header query too many rows.
    """
    sparql = _CountingClient(http)
    dataset = dataset_name(context.generation)
    keys = build_keys(context.settings, hooks)
    try:
        context.report("Reading the ontology's header")
        meta = await header_meta(context, sparql, dataset)
        async with build(context.settings.data_dir, meta, keys) as built:
            db = _PhasedWriter(built)
            hook_context = PreCacheContext(context, sparql, dataset, db)
            for number, hook in enumerate(hooks, start=1):
                logger.info("Load job %d: pre-caching hook %s", context.generation, hook.name)
                label = f"Hook {number} of {len(hooks)}, {hook.name}"
                try:
                    async with _hook_progress(context, sparql, db, label):
                        await hook(hook_context)
                except SparqlError as error:
                    raise StageError(f"pre-caching query failed ({hook.name}): {error}") from error
    except SparqlError as error:
        raise StageError(f"pre-caching query failed: {error}") from error


async def rebuild(context: JobContext, http: httpx2.AsyncClient, hooks: Sequence[Hook] = HOOKS) -> None:
    """Rebuild a job's derived database after a restart (`JobRunner.rebuild`), once Fuseki answers (`pre_cache`).

    A job stored without prefixes, as one is by an upgrade from before they were stored, has them read again from its
    uploaded file if that's still there (`browl.loading.read_upload_prefixes`); the job state then stores them.

    Raises:
        StageError: Fuseki didn't answer in time, or a query failed or returned an incomplete result.
    """
    if not context.prefixes:
        context.prefixes = await asyncio.to_thread(read_upload_prefixes, context.settings.data_dir, context.generation)
    context.report("Waiting for Fuseki")
    await wait_for_fuseki(http)
    await pre_cache(context, http, hooks)


async def wait_for_fuseki(http: httpx2.AsyncClient, limit_s: float | None = None) -> None:
    """Wait until Fuseki answers its ping, pinging with a growing pause between tries (`FUSEKI_RETRY_S`).

    Args:
        http: The HTTP client for Fuseki.
        limit_s: How long to keep trying; `FUSEKI_WAIT_S` if not given.

    Raises:
        StageError: Fuseki didn't answer within the limit.
    """
    limit_s = FUSEKI_WAIT_S if limit_s is None else limit_s
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    pause, longest = FUSEKI_RETRY_S
    while not await ping(http):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise StageError(f"Fuseki didn't answer its ping within {limit_s:g} s")
        logger.info("Waiting for Fuseki to answer its ping")
        await asyncio.sleep(min(pause, remaining))
        pause = min(pause * 2, longest)


@asynccontextmanager
async def build(data_dir: Path, meta: Meta, keys: Mapping[str, str]) -> AsyncIterator[DerivedWriter]:
    """Build a generation's derived database on a thread of its own (`browl.derived.build`, `on_thread`).

    On a clean exit the database is swapped in; if the block fails or is cancelled, the temporary file is deleted.
    """
    async with on_thread(derived.build(data_dir, meta, keys), "derived-build") as db:
        yield db


@asynccontextmanager
async def _hook_progress(
    context: JobContext, sparql: "_CountingClient", db: "_PhasedWriter", label: str
) -> AsyncIterator[None]:
    """Report a hook's progress every `PROGRESS_S` while the block runs: its label, time so far, and phase.

    The phase is the rows received while a bulk query streams, else `INDEXING` or `WRITING` while the hook waits for
    one. Then the database changes the hook has made, when they've moved since the last report or there's no phase:
    every row inserted, updated or deleted, staging rows too, so they can be more than the rows received (the names
    hook sets each entity's CURIE first). They move only when a write ends.
    """
    await db.run(lambda connection: None)  # so `changes` counts what the build changed before, e.g. `meta`
    started, before = time.monotonic(), db.changes

    async def tick() -> None:
        last = 0
        while True:
            parts = [label, format_duration(time.monotonic() - started)]
            if sparql.streams:
                received = sparql.received
                parts.append(f"receiving query results: {received:,} rows" if received else "waiting for query results")
            elif db.phase is not None:
                parts.append(db.phase)
            changes = db.changes - before
            if changes != last or len(parts) == 2:
                parts.append(f"{changes:,} database changes")
            last = changes
            context.report(" · ".join(parts))
            await asyncio.sleep(PROGRESS_S)

    ticker = asyncio.create_task(tick())
    try:
        yield
    finally:
        ticker.cancel()


class _CountingClient(FusekiClient):
    """A Fuseki client that counts the rows of the bulk queries' results as they arrive, for the hooks' progress.

    The hooks' build keys don't cover it, as it changes no result: the build-key coverage tests run the hooks without
    it.

    Attributes:
        streams: How many bulk queries are open.
        received: How many rows the latest one to open has yielded.
    """

    def __init__(self, http: httpx2.AsyncClient) -> None:
        """Use an HTTP client whose base URL is Fuseki's (`FUSEKI_URL`)."""
        super().__init__(http)
        self.streams = 0
        self.received = 0

    @asynccontextmanager
    async def bulk_select(
        self, dataset: str, query: str, prologue: str = "", dataset_clause: str = ""
    ) -> AsyncIterator[BulkSelectStream]:
        """Run a bulk SELECT query, counting its rows (`FusekiClient.bulk_select`)."""
        self.streams += 1
        self.received = 0
        try:
            async with super().bulk_select(dataset, query, prologue, dataset_clause) as stream:
                yield _CountedStream(stream, self)
        finally:
            self.streams -= 1


class _CountedStream(BulkSelectStream):
    """A bulk query's result that counts its rows in its client's `received`."""

    def __init__(self, stream: BulkSelectStream, client: _CountingClient) -> None:
        self.variables = stream.variables
        self._rows = stream
        self._client = client

    async def __aiter__(self) -> AsyncIterator[tuple[Term | None, ...]]:
        """Yield each row, counting it."""
        client = self._client
        async for row in self._rows:
            client.received += 1
            yield row


class _PhasedWriter(DerivedWriter):
    """A writer that says what it's waiting for and how much it has changed, for the hooks' progress.

    It writes through another, the build's own. The hooks' build keys don't cover it, as it changes no result.

    Attributes:
        phase: `INDEXING` or `WRITING` while a call runs, else None.
        changes: The rows the connection had inserted, updated or deleted (`sqlite3.Connection.total_changes`) when
            the last call ended, staging rows included.
    """

    def __init__(self, writer: DerivedWriter) -> None:
        self._writer = writer
        self.phase: str | None = None
        self.changes = 0

    def _counted[T](self, function: Callable[[sqlite3.Connection], T], connection: sqlite3.Connection) -> T:
        try:
            return function(connection)
        finally:
            self.changes = connection.total_changes

    async def run[T](self, function: Callable[[sqlite3.Connection], T]) -> T:
        """Call a function with the connection on its thread, returning its result (`DerivedWriter.run`)."""
        outer = self.phase is None
        if outer:
            self.phase = WRITING
        try:
            return await self._writer.run(functools.partial(self._counted, function))
        finally:
            if outer:
                self.phase = None

    async def create_indexes(self, table: str) -> None:
        """Create a table's secondary indexes (`DerivedWriter.create_indexes`)."""
        self.phase = INDEXING
        try:
            await super().create_indexes(table)
        finally:
            self.phase = None


@asynccontextmanager
async def on_thread(
    manager: AbstractContextManager[sqlite3.Connection],
    name: str,
    interrupt: bool = False,
) -> AsyncIterator[DerivedWriter]:
    """Enter a database build's context manager on a thread of its own, yielding a writer for its connection.

    The build's end, the manager's exit, runs on the connection's thread after any work still queued there, and is
    waited for, even if this is cancelled meanwhile: the end, e.g. the rename into place, mustn't outlive the job
    lock. A cancellation then propagates once the end is done, even if the database was swapped in.

    Args:
        manager: The build, e.g. `browl.derived.build`, which yields a connection.
        name: The thread's name's prefix, which names the build in the log, e.g. "derived-build".
        interrupt: Whether a cancellation interrupts the statement running on the connection
            (`sqlite3.Connection.interrupt`), and any the work queued starts after, so the end needn't wait for them.
    """
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
    building = _Build(manager, interrupt)
    error: BaseException | None = None
    try:
        yield DerivedWriter(executor, await loop.run_in_executor(executor, building.enter))
    except BaseException as caught:
        error = caught
        raise
    finally:
        end = loop.run_in_executor(executor, building.exit, error)
        executor.shutdown(wait=False)
        if interrupt and isinstance(error, asyncio.CancelledError):
            building.interrupt()
        try:
            await asyncio.shield(end)
        except asyncio.CancelledError:
            while not end.done():  # however often it's cancelled, e.g. by an anyio cancel scope
                with suppress(asyncio.CancelledError):
                    await asyncio.wait({end})
            if not end.cancelled() and (end_error := end.exception()) is not None:
                logger.warning("The cancelled %s's end failed", name.replace("-", " "), exc_info=end_error)
            raise


class _Build:
    """`derived.build`'s context manager, entered and exited by calls on the connection's thread."""

    def __init__(self, manager: AbstractContextManager[sqlite3.Connection], interruptible: bool) -> None:
        self._manager = manager
        self._interruptible = interruptible
        self._interrupted = False
        self._entered = False
        self._connection: sqlite3.Connection | None = None

    def enter(self) -> sqlite3.Connection:
        connection = self._connection = self._manager.__enter__()
        self._entered = True
        if self._interruptible:  # so a statement that starts after `interrupt` stops too
            connection.set_progress_handler(lambda: self._interrupted, INTERRUPT_CHECK_STEPS)
        return connection

    def interrupt(self) -> None:
        """Interrupt the statement running on the connection, if any, and any that starts after; from any thread."""
        self._interrupted = True
        if self._connection is not None:
            self._connection.interrupt()

    def exit(self, error: BaseException | None) -> None:
        """Finish the build, or on an error discard it; nothing if it was never entered."""
        if not self._entered:
            return
        self._entered = False
        if self._connection is not None:
            self._connection.set_progress_handler(None, 0)  # the end may run statements
        if error is None:
            self._manager.__exit__(None, None, None)
        else:
            self._manager.__exit__(type(error), error, error.__traceback__)


async def remove_previous(context: JobContext) -> None:
    """Delete every derived database but the ready generation's (`JobRunner`'s `on_ready`, under the job lock)."""
    await asyncio.to_thread(derived.remove_others, context.settings.data_dir, context.generation)
