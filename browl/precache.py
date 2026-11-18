"""The pre-caching stage (DESIGN.md §2.4, §2.5): build the derived database from bulk queries to the loaded store.

The stage reads the ontology's IRI and version IRI, starts a new derived database (`browl.derived.build`) with its
`meta` row, and runs the hooks in `HOOKS` in order; each fills tables from its own bulk queries (P-10 entities, P-11
names, P-12 hierarchy, P-19 search). The database is swapped in only once every hook has succeeded; if any fails, the
stage fails and the previous database stays in place.

A SQLite connection must stay on one thread, while the queries stream on the event loop. So the build runs on a
single-thread executor that owns the connection (`DerivedWriter`): a hook reads its query's rows on the event loop and
hands them over in batches (`batched`, `DerivedWriter.insert`), which keeps the event loop free.
"""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, AbstractContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2

from browl import derived
from browl.derived import Meta
from browl.fuseki import BulkSelectStream, FusekiClient, SparqlError, dataset_clause
from browl.jobs import ImportResult, JobContext, StageError
from browl.loading import dataset_name

logger = logging.getLogger(__name__)

BATCH_ROWS = 10_000
"""How many rows `batched` hands over at once: few enough to hold, many enough that the thread hand-off is cheap."""

PREFIXES = """PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""
"""The prologue of browl's bulk queries."""

ONTOLOGY_IRIS = """SELECT ?ontology ?version
WHERE {
  ?ontology rdf:type owl:Ontology .
  FILTER isIRI(?ontology)
  FILTER NOT EXISTS { ?importer owl:imports ?ontology }
  OPTIONAL { ?ontology owl:versionIRI ?version FILTER isIRI(?version) }
}"""
"""The ontology's IRI and version IRI, from its own (default) graph; an ontology it imports, as an OFN upload's
converted Turtle may declare, is left out."""


class DerivedWriter:
    """Writes to the derived database being built, on the one thread that owns its connection.

    Each call queues its work on that thread and waits for it. A cancelled call's work still runs; anything queued
    after it, such as the build's cleanup, runs once it's done.
    """

    def __init__(self, executor: ThreadPoolExecutor, connection: sqlite3.Connection) -> None:
        """Use the connection, which belongs to the executor's only thread."""
        self._executor = executor
        self._connection = connection

    async def run[T](self, function: Callable[[sqlite3.Connection], T]) -> T:
        """Call a function with the connection on its thread, returning its result."""
        return await asyncio.get_running_loop().run_in_executor(self._executor, function, self._connection)

    async def insert(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        """Run a statement once per row (`executemany`), on the connection's thread.

        The rows are read on that thread, so pass a list, not an iterator that runs on the event loop.
        """
        await self.run(lambda connection: connection.executemany(sql, rows))


@dataclass(frozen=True)
class PreCacheContext:
    """What a pre-caching hook gets.

    Attributes:
        job: The load job.
        sparql: The Fuseki client.
        dataset: The Fuseki dataset that serves the new store (`browl.loading.dataset_name`).
        has_imports: Whether any import was loaded, into a named graph of its own.
        db: Writes to the new derived database.
    """

    job: JobContext
    sparql: FusekiClient
    dataset: str
    has_imports: bool
    db: DerivedWriter

    @property
    def dataset_clause(self) -> str:
        """Return the dataset clause of a query that should see the imports (`browl.fuseki.dataset_clause`)."""
        return dataset_clause(self.has_imports)

    def bulk_select(
        self, query: str, prologue: str = PREFIXES, imports: bool = True
    ) -> AbstractAsyncContextManager[BulkSelectStream]:
        """Run a bulk SELECT query on the new store, checked to be complete (`FusekiClient.bulk_select`).

        Args:
            query: The SELECT query, without prologue or dataset clause.
            prologue: Its `PREFIX` declarations; `PREFIXES` by default.
            imports: Whether it sees the imports too, or only the ontology (the default graph).
        """
        clause = self.dataset_clause if imports else ""
        return self.sparql.bulk_select(self.dataset, query, prologue, clause)


Hook = Callable[[PreCacheContext], Awaitable[None]]
"""A pre-caching hook: fills derived tables from bulk queries. A `SparqlError` it raises fails the stage, naming it."""

HOOKS: tuple[Hook, ...] = ()
"""The hooks the app runs, in order: a hook may read tables an earlier one filled."""


def has_loaded_imports(imports: Iterable[ImportResult]) -> bool:
    """Return whether any import was loaded, so queries that should see the imports need `UNION_FROM`."""
    return any(not result.dropped for result in imports)


async def batched[T](rows: AsyncIterable[T], size: int = BATCH_ROWS) -> AsyncIterator[list[T]]:
    """Yield the rows in lists of `size`, the last one shorter."""
    batch: list[T] = []
    async for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


async def pre_caching_stage(context: JobContext, http: httpx2.AsyncClient, hooks: Sequence[Hook] = HOOKS) -> None:
    """Build the new generation's derived database from the store just loaded, and swap it in (`pre_cache`).

    Raises:
        StageError: A query failed or returned an incomplete result.
    """
    if context.ontology is None:  # nothing was loaded (as the loading stage skips it)
        return
    await pre_cache(context, http, hooks)


async def pre_cache(context: JobContext, http: httpx2.AsyncClient, hooks: Sequence[Hook] = HOOKS) -> None:
    """Build a job's derived database from the store Fuseki serves for its generation, and swap it in.

    The pre-caching stage runs it, and so does a rebuild after a restart (`JobRunner.rebuild`), with a context made from
    the stored job state: it uses only the generation, reasoner, start time (the `meta` table's load time, the same
    for both) and imports.

    Args:
        context: The job.
        http: The HTTP client for Fuseki.
        hooks: The hooks that fill the tables, run in order.

    Raises:
        StageError: A query failed or returned an incomplete result.
    """
    sparql = FusekiClient(http)
    dataset = dataset_name(context.generation)
    try:
        ontology_iri, version_iri = await ontology_iris(sparql, dataset)
        meta = Meta(context.generation, context.reasoner, context.started_at, ontology_iri, version_iri)
        async with build(context.settings.data_dir, meta) as db:
            hook_context = PreCacheContext(context, sparql, dataset, has_loaded_imports(context.imports), db)
            for hook in hooks:
                name = getattr(hook, "__qualname__", repr(hook))
                logger.info("Load job %d: pre-caching hook %s", context.generation, name)
                try:
                    await hook(hook_context)
                except SparqlError as error:
                    raise StageError(f"pre-caching query failed ({name}): {error}") from error
    except SparqlError as error:
        raise StageError(f"pre-caching query failed: {error}") from error


async def ontology_iris(sparql: FusekiClient, dataset: str) -> tuple[str | None, str | None]:
    """Return the ontology's IRI and version IRI, each None if it has none.

    If the default graph declares several ontologies (none importing another), the first IRI in order is taken, so the
    choice is stable, and a warning logged; likewise for several version IRIs.

    Raises:
        SparqlError: The query failed, or its result is incomplete.
    """
    async with sparql.bulk_select(dataset, ONTOLOGY_IRIS, PREFIXES) as stream:
        columns = stream.variables.index("ontology"), stream.variables.index("version")
        rows = [(row[columns[0]], row[columns[1]]) async for row in stream]
    ontologies = sorted({str(ontology) for ontology, _ in rows if ontology is not None})
    if not ontologies:
        return None, None
    if len(ontologies) > 1:
        logger.warning("The ontology declares several ontology IRIs; taking %s from %s", ontologies[0], ontologies)
    versions = sorted({str(version) for ontology, version in rows if ontology == ontologies[0] and version is not None})
    if len(versions) > 1:
        logger.warning("The ontology %s has several version IRIs; taking %s", ontologies[0], versions[0])
    return ontologies[0], versions[0] if versions else None


@asynccontextmanager
async def build(data_dir: Path, meta: Meta) -> AsyncIterator[DerivedWriter]:
    """Build a generation's derived database on a thread of its own (`browl.derived.build`), yielding its writer.

    On a clean exit the database is swapped in; if the block fails or is cancelled, the temporary file is deleted.
    Either way the build's end runs on the connection's thread after any work still queued there, and is waited for,
    even if this is cancelled meanwhile: the end, e.g. the rename into place, mustn't outlive the job lock. A
    cancellation then propagates once the end is done, even if the database was swapped in.
    """
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="derived-build")
    building = _Build(derived.build(data_dir, meta))
    error: BaseException | None = None
    try:
        yield DerivedWriter(executor, await loop.run_in_executor(executor, building.enter))
    except BaseException as caught:
        error = caught
        raise
    finally:
        end = loop.run_in_executor(executor, building.exit, error)
        executor.shutdown(wait=False)
        try:
            await asyncio.shield(end)
        except asyncio.CancelledError:
            while not end.done():  # however often it's cancelled, e.g. by an anyio cancel scope
                with suppress(asyncio.CancelledError):
                    await asyncio.wait({end})
            if not end.cancelled() and (end_error := end.exception()) is not None:
                logger.warning("The cancelled derived build's end failed", exc_info=end_error)
            raise


class _Build:
    """`derived.build`'s context manager, entered and exited by calls on the connection's thread."""

    def __init__(self, manager: AbstractContextManager[sqlite3.Connection]) -> None:
        self._manager = manager
        self._entered = False

    def enter(self) -> sqlite3.Connection:
        connection = self._manager.__enter__()
        self._entered = True
        return connection

    def exit(self, error: BaseException | None) -> None:
        """Finish the build, or on an error discard it; nothing if it was never entered."""
        if not self._entered:
            return
        self._entered = False
        if error is None:
            self._manager.__exit__(None, None, None)
        else:
            self._manager.__exit__(type(error), error, error.__traceback__)


async def remove_previous(context: JobContext) -> None:
    """Delete every derived database but the ready generation's (`JobRunner`'s `on_ready`, under the job lock)."""
    await asyncio.to_thread(derived.remove_others, context.settings.data_dir, context.generation)
