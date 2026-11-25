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
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import httpx2

from browl.fuseki import FusekiAdmin, FusekiAdminError, FusekiClient, SparqlError
from browl.imports import CONVERTED
from browl.jobs import JobContext
from browl.jobtypes import StageError
from browl.prefixes import read_prefixes
from browl.process import Completed, run_process
from browl.progress_text import count_of
from browl.rdf import Literal, RDFSyntaxError, Term, parse_tsv_header, parse_tsv_row
from browl.settings import Settings
from browl.upload import STORED_SUFFIXES, Syntax, check_job_file, upload_path

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

# `tdb2.tdbloader`'s output: a line, then the messages in it.
_LOADER_MESSAGE = re.compile(r"^\S+ +[A-Z]+ +\S+ +:: (.*)$")  # "01:43:19 INFO  loader          :: <message>"
_ADDED = re.compile(r"Add: ([\d,]+) (\S+)")  # "Add: 1,000,000 big.nt (Batch: 1,360,544 / Avg: 1,360,544)"
_FINISHED = re.compile(r"Finished: .*: ([\d,]+) tuples in ")  # "Finished: /jobs/3/big.nt: 1,200,000 tuples in 0.89s"
_INDEX_DONE = re.compile(r"Finish - index (\w+)")  # "Finish - index SPO"
_INDEX_REPLAY = re.compile(r"Start replay index (\w+)")  # "Start replay index SPO"
_REPLAYED = "Index"
"""The name in the `Add:` lines of `tdb2.tdbloader`'s index phase, where a file's name is in its loading phase's."""


class LoaderProgress:
    """Follow a `tdb2.tdbloader` run from its output, reporting how many triples it has added, then its index phases.

    It logs a running count while adding triples (`Add: 1,000,000 uberon.ttl …`), and its index phases by name, the
    second building the other indexes from the first, with a running count of its own (`Add: 1,000,000 Index …`).

    Args:
        name: What's loaded, e.g. "Import 2 of 5", which starts each detail; None for no name.
        total: How many triples the file holds, if known, for a percentage.
        report: Called with each detail, and how far that phase has got, from 0 to 1, or None.
    """

    def __init__(self, name: str | None, total: int | None, report: Callable[[str, float | None], None]) -> None:
        """Follow a run that hasn't started."""
        self._prefix = "" if name is None else f"{name}: "
        self._total = total or None
        self._report = report

    def add(self, line: str) -> None:
        """Look at a line of output."""
        if (match := _LOADER_MESSAGE.match(line)) is None:
            return
        message = match[1]
        if match := _ADDED.match(message):
            done = int(match[1].replace(",", ""))
            verb = "indexed" if match[2] == _REPLAYED else "added"
            fraction = None if self._total is None else min(1.0, done / self._total)
            self._say(f"{verb} {count_of(done, self._total, 'triples')}", fraction)
        elif match := _FINISHED.match(message):
            self._total = int(match[1].replace(",", "")) or None  # the index phase's total
            self._say("writing the primary index")
        elif match := _INDEX_REPLAY.match(message):
            self._say(f"building the other indexes from {match[1]}", None if self._total is None else 0.0)
        elif match := _INDEX_DONE.match(message):
            self._say(f"finished index {match[1]}")
        elif message.startswith("Start:"):
            self._say("adding triples", None if self._total is None else 0.0)

    def _say(self, detail: str, fraction: float | None = None) -> None:
        text = f"{self._prefix}{detail}"
        self._report(text[0].upper() + text[1:], fraction)


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


async def load_file(
    settings: Settings, store: Path, file: Path, graph: str | None = None, progress: LoaderProgress | None = None
) -> None:
    """Load an RDF file into a TDB2 store, into the named graph if one is given, else the default graph.

    `progress`, if given, follows the loader's output.

    Raises:
        StageError: The loader failed.
    """
    command = [_jena(settings, "tdb2.tdbloader"), "--loc", str(store)]
    if graph is not None:
        command += ["--graph", graph]
    completed = await run_process(
        [*command, str(file)],
        None if progress is None else progress.add,
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

    Sets the context's `triples`, and each loaded import's, and its `prefixes` (`read_upload_prefixes`), so the job
    state stores them before pre-caching starts. Reports the loader's progress (`LoaderProgress`), of the file's
    triples where the imports stage counted them (`JobContext.triple_counts`), then each step after it.

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
        files = len(context.import_files)
        progress = LoaderProgress("Ontology" if files else None, context.triple_counts.get(ontology), context.report)
        await load_file(settings, store, ontology, progress=progress)
        for number, (iri, file) in enumerate(context.import_files.items(), start=1):
            progress = LoaderProgress(f"Import {number} of {files}", context.triple_counts.get(file), context.report)
            await load_file(settings, store, file, graph=iri, progress=progress)
        context.report("Counting the new store's triples")
        expected = await count_store(settings, store)
        registering = True
        context.report("Opening the new store in Fuseki")
        await admin.add(name)
        context.report("Counting the triples Fuseki serves")
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
    context.prefixes = await asyncio.to_thread(read_upload_prefixes, settings.data_dir, context.generation)


def read_upload_prefixes(data_dir: Path, generation: int) -> dict[str, str]:
    """Return the prefixes a generation's uploaded file declares (`browl.prefixes.read_prefixes`); none if it's gone.

    They're read from the file the admin chose, not a reasoned one, whose writer may not keep them: the upload, or the
    Turtle an OFN upload was converted to (`browl.imports.CONVERTED`), as the prefixes reader reads only RDF.
    """
    for path in (
        upload_path(data_dir, generation, STORED_SUFFIXES[Syntax.OFN]).with_name(CONVERTED),
        upload_path(data_dir, generation, STORED_SUFFIXES[Syntax.TURTLE]),
        upload_path(data_dir, generation, STORED_SUFFIXES[Syntax.RDF_XML]),
    ):
        if path.is_file():
            return read_prefixes(path)
    logger.warning("Load job %d's uploaded file is gone, so the ontology's prefixes aren't known", generation)
    return {}


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
