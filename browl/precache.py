"""The pre-caching stage (DESIGN.md §2.4, §2.5): build the derived database from bulk queries to the loaded store.

The stage reads the ontology's IRI, version IRI, title and version info (`ontology_header`), starts a new derived
database (`browl.derived.build`) with its `meta` row, and runs the hooks in `HOOKS` in order; each fills tables from its
own bulk queries (P-10 entities, P-11 names, P-12 hierarchy, P-19 search). The database is swapped in only once every
hook has succeeded; if any fails, the stage fails and the previous database stays in place.

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
from browl.fuseki import BulkSelectStream, FusekiClient, SparqlError, dataset_clause, ping
from browl.jobs import ImportResult, JobContext, StageError
from browl.loading import dataset_name
from browl.names import BUILTIN_PREFIXES, best_values, expand_curie
from browl.rdf import IRI, Literal, RDFSyntaxError, Term
from browl.settings import Settings

logger = logging.getLogger(__name__)

BATCH_ROWS = 10_000
"""How many rows `batched` hands over at once: few enough to hold, many enough that the thread hand-off is cheap."""

PREFIXES = """PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""
"""The prologue of browl's bulk queries."""

ONTOLOGY_HEADER = """SELECT ?ontology ?property ?value
WHERE {{
  ?ontology rdf:type owl:Ontology .
  FILTER isIRI(?ontology)
  FILTER NOT EXISTS {{ ?importer owl:imports ?ontology }}
  OPTIONAL {{ ?ontology ?property ?value FILTER (?property IN ({properties})) }}
}}"""
"""The ontology's IRI and its header values for `OntologyHeader` (`{properties}`: the properties' IRIs, in `<>`),
from its own (default) graph: an ontology it imports, as an OFN upload's converted Turtle may declare, is left out,
and so are an import's titles, in its own named graph."""

OWL_VERSION_IRI = "http://www.w3.org/2002/07/owl#versionIRI"
OWL_VERSION_INFO = "http://www.w3.org/2002/07/owl#versionInfo"
DCTERMS_TITLE = "http://purl.org/dc/terms/title"
DC_TITLE = "http://purl.org/dc/elements/1.1/title"


FUSEKI_WAIT_S = 120.0
"""How long a rebuild after a restart waits for Fuseki to answer, as it may be starting too (`wait_for_fuseki`)."""

FUSEKI_RETRY_S = (0.5, 5.0)
"""The first and the longest wait between pings while waiting for Fuseki; each wait doubles the last."""


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

    async def create_indexes(self, table: str) -> None:
        """Create a table's secondary indexes, once it's filled, so later hooks can use them (`derived.create_indexes`).

        Raises:
            ValueError: The database has no such table.
        """
        await self.run(lambda connection: derived.create_indexes(connection, table))

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
"""The hooks the app runs, in order: a hook may read tables an earlier one filled.

A hook creates the indexes of each table it fills once it's done (`DerivedWriter.create_indexes`), so later hooks can
use them; the build creates any left at its end. Where it can, it inserts into a `WITHOUT ROWID` table in its primary
key's order, as such a table is stored in that order."""


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

    The pre-caching stage runs it, and so does a rebuild after a restart (`rebuild`), with a context made from the
    stored job state: it uses only the generation, reasoner, start time (the `meta` table's load time, the same for
    both) and imports.

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
        header = await ontology_header(sparql, dataset, context.settings)
        meta = Meta(
            context.generation,
            context.reasoner,
            context.started_at,
            header.iri,
            header.version_iri,
            header.title,
            header.version_info,
        )
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


async def rebuild(context: JobContext, http: httpx2.AsyncClient, hooks: Sequence[Hook] = HOOKS) -> None:
    """Rebuild a job's derived database after a restart (`JobRunner.rebuild`), once Fuseki answers (`pre_cache`).

    Raises:
        StageError: Fuseki didn't answer in time, or a query failed or returned an incomplete result.
    """
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


@dataclass(frozen=True)
class OntologyHeader:
    """What the ontology says about itself, for the `meta` row; each None if it doesn't say.

    Attributes:
        iri: The ontology's IRI.
        version_iri: Its version IRI.
        title: Its display name (`ontology_header`); pages fall back to the IRI without one.
        version_info: Its `owl:versionInfo`, the lexical form.
    """

    iri: str | None = None
    version_iri: str | None = None
    title: str | None = None
    version_info: str | None = None


def title_properties(settings: Settings) -> list[str]:
    """Return the IRIs of the properties a title is taken from, best first: dcterms:title, dc:title, `NAME_PROPERTIES`.

    A name property's CURIE is expanded with the built-in prefixes and `EXTRA_PREFIXES`; one that's still not an IRI is
    left out, with a warning.
    """
    prefixes = BUILTIN_PREFIXES | settings.extra_prefixes
    properties = [DCTERMS_TITLE, DC_TITLE]
    for name in settings.name_properties:
        try:
            properties.append(IRI(expand_curie(name, prefixes)))
        except RDFSyntaxError:
            logger.warning("The name property %r isn't an IRI or a known CURIE; it's left out of the title", name)
    return list(dict.fromkeys(properties))


async def ontology_header(sparql: FusekiClient, dataset: str, settings: Settings) -> OntologyHeader:
    """Return the ontology's IRI, version IRI, title and version info, from its own (default) graph.

    If the default graph declares several ontologies (none importing another), the first IRI in order is taken, so the
    choice is stable, and a warning logged; likewise for several version IRIs. The title is a literal of the first
    property in `title_properties` that has one, chosen by language (`LANGUAGE_RANKING`, `browl.names.language_rank`)
    then lexical order; a warning is logged if several tie on language. The version info is a literal too, chosen the
    same way, with a warning if there are several.

    Raises:
        SparqlError: The query failed, or its result is incomplete.
    """
    titles = title_properties(settings)
    properties = ", ".join(f"<{iri}>" for iri in [OWL_VERSION_IRI, OWL_VERSION_INFO, *titles])
    async with sparql.bulk_select(dataset, ONTOLOGY_HEADER.format(properties=properties), PREFIXES) as stream:
        columns = [stream.variables.index(name) for name in ("ontology", "property", "value")]
        rows = [tuple(row[column] for column in columns) async for row in stream]
    ontologies = sorted({str(ontology) for ontology, _, _ in rows if ontology is not None})
    if not ontologies:
        return OntologyHeader()
    iri = ontologies[0]
    if len(ontologies) > 1:
        logger.warning("The ontology declares several ontology IRIs; taking %s from %s", iri, ontologies)
    values: dict[str, list[Term]] = {}
    for ontology, prop, value in rows:
        if ontology == iri and prop is not None and value is not None:
            values.setdefault(str(prop), []).append(value)

    versions = sorted({str(version) for version in values.get(OWL_VERSION_IRI, []) if isinstance(version, IRI)})
    if len(versions) > 1:
        logger.warning("The ontology %s has several version IRIs; taking %s", iri, versions[0])

    title = None
    ranking = settings.language_ranking
    for title_property in titles:
        best = best_values(_literals(values.get(title_property, [])), ranking)
        if best:
            title = best[0]
            if len(best) > 1:
                logger.warning(
                    "The ontology %s has several titles (%s) of the best language; taking %r",
                    iri,
                    title_property,
                    title,
                )
            break

    infos = _literals(values.get(OWL_VERSION_INFO, []))
    best_infos = best_values(infos, ranking)
    if len({info.value for info in infos}) > 1:
        logger.warning(
            "The ontology %s has several owl:versionInfo values %r; taking %r",
            iri,
            sorted({info.value for info in infos}),
            best_infos[0],
        )
    return OntologyHeader(iri, versions[0] if versions else None, title, best_infos[0] if best_infos else None)


def _literals(values: Iterable[Term]) -> list[Literal]:
    return [value for value in values if isinstance(value, Literal)]


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
