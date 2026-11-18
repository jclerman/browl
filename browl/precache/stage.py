"""The pre-caching stage: build the derived database from bulk queries to the loaded store (DESIGN.md §2.5).

The ontology header gives the `meta` row (`browl.precache.header`); then the hooks in `HOOKS` run in order, each
filling tables from its own bulk queries and writing through a `DerivedWriter`, on the build's own thread, so the
queries can stream on the event loop. The file stores its build keys (`browl.precache.keys`).
"""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, asynccontextmanager, suppress
from pathlib import Path

import httpx2

from browl import derived
from browl.derived import Meta
from browl.fuseki import FusekiClient, SparqlError, dataset_clause, ping
from browl.jobs import JobContext, StageError
from browl.loading import dataset_name
from browl.precache.core import DerivedWriter, Hook, PreCacheContext, has_loaded_imports
from browl.precache.entities import entities
from browl.precache.header import header_meta
from browl.precache.keys import build_keys

logger = logging.getLogger(__name__)

FUSEKI_WAIT_S = 120.0
"""How long a rebuild after a restart waits for Fuseki to answer, as it may be starting too (`wait_for_fuseki`)."""

FUSEKI_RETRY_S = (0.5, 5.0)
"""The first and the longest wait between pings while waiting for Fuseki; each wait doubles the last."""

HOOKS: tuple[Hook, ...] = (entities,)
"""The hooks the app runs, in order: a hook may read tables an earlier one filled.

A hook creates the indexes of each table it fills once it's done (`DerivedWriter.create_indexes`), so later hooks can
use them; the build creates any left at its end. Where it can, it inserts into a `WITHOUT ROWID` table in its primary
key's order, as such a table is stored in that order."""


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
    both) and imports.

    Args:
        context: The job.
        http: The HTTP client for Fuseki.
        hooks: The hooks that fill the tables, run in order.

    Raises:
        StageError: A query failed or returned an incomplete result, or the header query too many rows.
    """
    sparql = FusekiClient(http)
    dataset = dataset_name(context.generation)
    keys = build_keys(context.settings, hooks)
    try:
        meta = await header_meta(context, sparql, dataset)
        async with build(context.settings.data_dir, meta, keys) as db:
            clause = dataset_clause(has_loaded_imports(context.imports))
            hook_context = PreCacheContext(context, sparql, dataset, clause, db)
            for hook in hooks:
                logger.info("Load job %d: pre-caching hook %s", context.generation, hook.name)
                try:
                    await hook(hook_context)
                except SparqlError as error:
                    raise StageError(f"pre-caching query failed ({hook.name}): {error}") from error
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


@asynccontextmanager
async def build(data_dir: Path, meta: Meta, keys: Mapping[str, str]) -> AsyncIterator[DerivedWriter]:
    """Build a generation's derived database on a thread of its own (`browl.derived.build`), yielding its writer.

    On a clean exit the database is swapped in; if the block fails or is cancelled, the temporary file is deleted.
    Either way the build's end runs on the connection's thread after any work still queued there, and is waited for,
    even if this is cancelled meanwhile: the end, e.g. the rename into place, mustn't outlive the job lock. A
    cancellation then propagates once the end is done, even if the database was swapped in.
    """
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="derived-build")
    building = _Build(derived.build(data_dir, meta, keys))
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
