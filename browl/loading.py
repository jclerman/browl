"""The loading stage (DESIGN.md §2.4): build a new TDB2 store with Jena's loader, then switch Fuseki to it.

Each job's store is the dataset `gen-N`, built at `{FUSEKI_BASE}/databases/gen-N`, where Fuseki's template puts a
dataset registered by that name. The ontology goes in the store's default graph, each import in a named graph of its
own IRI. The loader runs as a subprocess, `tdb2.tdbloader`, which loaded NCBITaxon in about a minute in the app
container, against 2.5 minutes for `tdb2.xloader` (research/P-8).

Before Fuseki opens the store, its triples are counted with `tdb2.tdbquery`; once Fuseki has opened it, they're counted
again through Fuseki, and must match, which confirms that Fuseki serves the whole store. The counts are kept with the
job, for the ontology's home page. Every other `gen-N` dataset is then removed, which deletes its store.

The store survives restarts, and so does Fuseki's registration of it, so nothing is reloaded on restart.
"""

import asyncio
import logging
import re
import shutil
from dataclasses import replace
from pathlib import Path

import httpx2

from browl.fuseki import FusekiAdmin, FusekiAdminError, FusekiClient, SparqlError
from browl.jobs import JobContext
from browl.jobtypes import StageError
from browl.process import Completed, run_process
from browl.rdf import Literal, RDFSyntaxError, Term, parse_tsv_header, parse_tsv_row
from browl.settings import Settings
from browl.upload import check_job_file

logger = logging.getLogger(__name__)

DATASET_PREFIX = "gen-"
_DATASET = re.compile(rf"{DATASET_PREFIX}\d+")

LOADER_HEAP = "2g"
"""The loader's heap: it loaded NCBITaxon (24 M triples) within 2 GB; the store is memory-mapped, outside the heap."""

LOAD_TIMEOUT_S = 4 * 3600.0
"""The longest one loader or count run may take."""

COUNT_TIMEOUT_S = 1800.0
"""Time limit for counting the triples through Fuseki."""

COUNT_TRIPLES = (Path(__file__).parent / "sparql" / "count-triples.rq").read_text(encoding="utf-8")
"""The query that counts each graph's triples, in one pass over the store."""


def dataset_name(generation: int) -> str:
    """Return the name of the Fuseki dataset that serves a generation."""
    return f"{DATASET_PREFIX}{generation}"


def store_path(settings: Settings, generation: int) -> Path:
    """Return where a generation's TDB2 store is built, as the app sees it."""
    return settings.fuseki_base / "databases" / dataset_name(generation)


def _jena(settings: Settings, tool: str) -> str:
    return str(settings.jena_home / "bin" / tool)


def _failure(tool: str, completed: Completed) -> StageError:
    reason = completed.limit_reason(tool, f"{LOAD_TIMEOUT_S:g} s")
    return StageError(reason or f"{tool} failed (exit status {completed.status}):\n{completed.output}")


async def load_file(settings: Settings, store: Path, file: Path, graph: str | None = None) -> None:
    """Load an RDF file into a TDB2 store, into the named graph if one is given, else the default graph.

    Raises:
        StageError: The loader failed.
    """
    command = [_jena(settings, "tdb2.tdbloader"), "--loc", str(store)]
    if graph is not None:
        command += ["--graph", graph]
    completed = await run_process(
        [*command, str(file)],
        timeout_s=LOAD_TIMEOUT_S,
        watch=[settings.data_dir, settings.fuseki_base],
        env={"JVM_ARGS": f"-Xmx{LOADER_HEAP}"},
        name="tdb2.tdbloader",
    )
    if not completed.ok:
        raise _failure("tdb2.tdbloader", completed)


async def _query_store(settings: Settings, store: Path, query: str) -> list[tuple[Term | None, ...]]:
    """Run a SELECT query on a store Fuseki hasn't opened, returning its rows."""
    output = bytearray()
    completed = await run_process(
        [_jena(settings, "tdb2.tdbquery"), "--loc", str(store), "--results=tsv", query],
        timeout_s=LOAD_TIMEOUT_S,
        on_stdout=output.extend,
        env={"JVM_ARGS": f"-Xmx{LOADER_HEAP}"},
        name="tdb2.tdbquery",
    )
    if not completed.ok:
        raise _failure("tdb2.tdbquery", completed)
    try:
        text = output.decode()
    except UnicodeDecodeError as error:
        raise StageError(f"tdb2.tdbquery's result isn't UTF-8: {error}") from error
    if not text:
        raise StageError("tdb2.tdbquery printed no result")
    header, *lines = text.splitlines()
    try:
        width = len(parse_tsv_header(header))
        return [parse_tsv_row(line, width) for line in lines]
    except RDFSyntaxError as error:
        raise StageError(f"tdb2.tdbquery's result is malformed: {error}") from error


def _counts(rows: list[tuple[Term | None, ...]]) -> dict[str | None, int]:
    """Return the triples in each graph, by its IRI (None for the default graph), from `COUNT_TRIPLES`' rows."""
    counts: dict[str | None, int] = {}
    for row in rows:
        if len(row) != 2:
            raise StageError(f"a triple count's row has {len(row)} cells, not 2")
        graph, count = row
        if not isinstance(count, Literal) or not count.value.isdigit():
            raise StageError(f"a triple count isn't a number: {count!r}")
        counts[None if graph is None else str(graph)] = int(count.value)
    return counts


async def count_store(settings: Settings, store: Path) -> dict[str | None, int]:
    """Return the triples in each of a store's graphs, by IRI (None for the default graph), read from its files.

    Raises:
        StageError: The query failed.
    """
    return _counts(await _query_store(settings, store, COUNT_TRIPLES))


async def count_fuseki(sparql: FusekiClient, dataset: str) -> dict[str | None, int]:
    """Return the triples in each of a dataset's graphs, by IRI (None for the default graph), as Fuseki serves them.

    Raises:
        SparqlError: A query failed.
    """
    return _counts((await sparql.select(dataset, COUNT_TRIPLES, COUNT_TIMEOUT_S)).rows)


async def remove_stale_stores(settings: Settings, admin: FusekiAdmin, name: str) -> None:
    """Clear the way for a new dataset: delete stale store directories, and any dataset with the new one's name.

    A stale store directory is one no registered dataset uses, left by a load that stopped. A dataset can have the
    new one's name only if the job state started again from scratch while Fuseki's volume was kept; it was never
    served by this job state's generations.
    """
    databases = settings.fuseki_base / "databases"
    registered = set(await admin.datasets())
    if name in registered:
        logger.warning("Removing the dataset %s, registered before this load of it", name)
        await admin.remove(name)
        registered.remove(name)
    for path in await asyncio.to_thread(lambda: list(databases.glob(f"{DATASET_PREFIX}*"))):
        if _DATASET.fullmatch(path.name) and path.name not in registered:
            logger.info("Deleting the stale store %s", path)
            await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)


async def loading_stage(context: JobContext, http: httpx2.AsyncClient) -> None:
    """Load the ontology and its fetched imports into a new TDB2 store, and switch Fuseki to it.

    Sets the context's `triples`, and each loaded import's.

    Args:
        context: The job.
        http: The HTTP client for Fuseki.

    Raises:
        StageError: The loader failed, Fuseki couldn't open the store, or it serves a different number of triples.
    """
    ontology = context.ontology
    if ontology is None:
        return
    settings = context.settings
    admin = FusekiAdmin(http)
    name = dataset_name(context.generation)
    store = store_path(settings, context.generation)
    for file in (ontology, *context.import_files.values()):
        await asyncio.to_thread(check_job_file, settings.data_dir, file)
    registering = False
    try:
        await remove_stale_stores(settings, admin, name)  # this also deletes any store left at `store`
        await asyncio.to_thread(store.parent.mkdir, parents=True, exist_ok=True)
        await load_file(settings, store, ontology)
        for iri, file in context.import_files.items():
            await load_file(settings, store, file, graph=iri)
        expected = await count_store(settings, store)
        registering = True
        await admin.add(name)
        served = await count_fuseki(FusekiClient(http), name)
        if served != expected:
            raise StageError(f"Fuseki serves {_describe(served)} from the new store, which holds {_describe(expected)}")
    except BaseException as error:
        await _discard(admin, name, store, registering)
        if isinstance(error, FusekiAdminError):
            raise StageError(str(error)) from error
        if isinstance(error, SparqlError):
            raise StageError(f"counting the new store's triples through Fuseki failed: {error}") from error
        raise
    context.triples = served.get(None, 0)
    context.imports = [
        replace(result, triples=served.get(result.iri, 0)) if result.iri in context.import_files else result
        for result in context.imports
    ]
    await _remove_others(admin, name)


async def _discard(admin: FusekiAdmin, name: str, store: Path, registering: bool) -> None:
    """Remove a failed load's dataset and store.

    Once registering has begun, Fuseki may have opened the store, even if the call failed or was cancelled, so the
    dataset is removed through Fuseki, which deletes the store, before the directory is deleted.
    """
    if registering:
        try:
            await admin.remove(name)
        except FusekiAdminError:
            logger.exception("Couldn't remove the dataset %s after a failed load", name)
    await asyncio.to_thread(shutil.rmtree, store, ignore_errors=True)


async def _remove_others(admin: FusekiAdmin, name: str) -> None:
    """Remove every other generation's dataset, each on its own, so one failure doesn't keep the rest.

    The new store is served by now: an old one left behind only takes space, and the next load removes it.
    """
    try:
        others = [other for other in await admin.datasets() if _DATASET.fullmatch(other) and other != name]
    except FusekiAdminError:
        logger.exception("Couldn't list the previous datasets")
        return
    for other in others:
        logger.info("Removing the previous dataset %s", other)
        try:
            await admin.remove(other)
        except FusekiAdminError:
            logger.exception("Couldn't remove the previous dataset %s", other)


def _describe(counts: dict[str | None, int]) -> str:
    named = sum(1 for graph in counts if graph is not None)
    return f"{sum(counts.values()):,} triples in {named + 1} graphs"
