"""What pre-caching hooks share: their context, the writer, batching, the `Hook` type, `sparql_file` and `logged_iri`.

A hook's module imports only what it uses, for its build key (`browl.precache.keys`).
"""

import asyncio
import sqlite3
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from browl import derived
from browl.fuseki import BulkSelectStream, FusekiClient, dataset_clause
from browl.jobtypes import ImportResult

if TYPE_CHECKING:
    from browl.jobs import JobContext

BATCH_ROWS = 10_000
"""How many rows `batched` hands over at once: few enough to hold, many enough that the thread hand-off is cheap."""

PREFIXES = """PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""
"""The prologue of browl's bulk queries."""

SPARQL_DIR = Path(__file__).parent.parent / "sparql"
"""Where `sparql_file` finds its files."""

LOGGED_IRI_CHARS = 300
"""The most characters of an IRI a log line shows (`logged_iri`)."""


def logged_iri(iri: str) -> str:
    """Return an IRI as a log line shows it: cut short, with "…", so a huge IRI can't give a huge log line."""
    return iri if len(iri) <= LOGGED_IRI_CHARS else f"{iri[:LOGGED_IRI_CHARS]}…"


def sparql_file(name: str) -> str:
    """Return the text of a file in `SPARQL_DIR`, such as a query.

    Pre-caching code reads its data files only through this, always with a string literal, so the hooks' build keys
    cover them (`browl.precache.keys`).
    """
    return (SPARQL_DIR / name).read_text(encoding="utf-8")


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
        sparql: The Fuseki client, for small queries. A bulk query must go through `bulk_select`, which checks that its
            result is complete; the client's own methods don't.
        dataset: The Fuseki dataset that serves the new store (`browl.loading.dataset_name`).
        db: Writes to the new derived database.
    """

    job: "JobContext"
    sparql: FusekiClient
    dataset: str
    db: DerivedWriter

    @property
    def dataset_clause(self) -> str:
        """Return the dataset clause of a query that should see the imports (`browl.fuseki.dataset_clause`)."""
        return dataset_clause(has_loaded_imports(self.job.imports))

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


def has_loaded_imports(imports: Iterable[ImportResult]) -> bool:
    """Return whether any import was loaded, so queries that should see the imports need `UNION_FROM`."""
    return any(not result.dropped for result in imports)


HookFunction = Callable[[PreCacheContext], Awaitable[None]]
"""What a hook runs."""


@dataclass(frozen=True)
class Hook:
    """A pre-caching hook: fills derived tables from bulk queries (`PreCacheContext.bulk_select`).

    A `SparqlError` it raises fails the stage, naming it. Make one with the `hook` decorator.

    Attributes:
        function: What it runs.
        settings: The names of the `Settings` fields it reads, whose values its build key covers.
    """

    function: HookFunction
    settings: frozenset[str] = frozenset()

    @property
    def name(self) -> str:
        """Return its name, its function's, which names its build key's component."""
        return self.function.__name__

    @property
    def module(self) -> str:
        """Return the name of its function's module, whose import closure its build key covers."""
        return self.function.__module__

    async def __call__(self, context: PreCacheContext) -> None:
        """Run it."""
        await self.function(context)


def hook(*settings: str) -> Callable[[HookFunction], Hook]:
    """Return a decorator that makes a function a `Hook` that reads these `Settings` fields (e.g. `"base_url"`)."""

    def decorate(function: HookFunction) -> Hook:
        return Hook(function, frozenset(settings))

    return decorate


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
