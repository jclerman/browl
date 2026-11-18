"""The load job (DESIGN.md §2.4): its stages, its state in a persistent SQLite database, and the runner.

A job starts with `JobRunner.upload`, which the upload handler (`browl.upload`) runs.
"""

import asyncio
import fcntl
import json
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, closing, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import IO, Self

import httpx2

from browl.settings import Settings

logger = logging.getLogger(__name__)

STATE_DB = "state.sqlite3"
"""The persistent database's file name in `DATA_DIR`: job state, and later admins (§2.11)."""

JOB_LOCK = "job.lock"
"""The job lock's file name in `DATA_DIR` (`JobLock`)."""

STATE_TTL_S = 1.0
"""How long a runner serves the job state it last read before reading the database again."""

UPLOAD_PROGRESS_S = 1.0
"""How often a running upload's size so far is recorded, for the status to show it growing."""

INTERRUPTED_BY_RESTART = "interrupted by restart"
INTERRUPTED_BY_SHUTDOWN = "interrupted by shutdown"
CANCELLED = "cancelled"
STOPPED_UNRECORDED = "internal error: the job stopped without recording why"

MAX_REASON_CHARS = 2000
"""The longest failure reason stored and shown to admins; a longer one, e.g. a tool's whole stderr, is cut short."""

MIGRATIONS: list[str] = [
    # Version 1. The job table has at most one row, the latest job.
    """
    CREATE TABLE job (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        generation INTEGER NOT NULL,
        stage TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        failed_stage TEXT,
        reason TEXT,
        ready_generation INTEGER
    );
    """,
    # Version 2: the reasoner the admin chose for the job (A-3), and the file uploaded.
    """
    ALTER TABLE job ADD COLUMN reasoner TEXT NOT NULL DEFAULT 'none';
    ALTER TABLE job ADD COLUMN upload_name TEXT;
    ALTER TABLE job ADD COLUMN upload_syntax TEXT;
    ALTER TABLE job ADD COLUMN upload_bytes INTEGER;
    """,
    # Version 3: the ontology's imports, as a JSON list of `ImportResult`s, and the triples loaded into the default
    # graph (P-8).
    """
    ALTER TABLE job ADD COLUMN imports TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE job ADD COLUMN triples INTEGER;
    """,
]
"""The persistent database's schema migrations: entry n migrates from version n to n + 1."""

_COLUMNS = (
    "generation, stage, started_at, finished_at, failed_stage, reason, ready_generation, reasoner,"
    " upload_name, upload_syntax, upload_bytes, imports, triples"
)
"""The job table's columns but its id, in `JobState`'s order."""

_PLACEHOLDERS = ", ".join("?" for _ in _COLUMNS.split(","))
"""A `?` for each of `_COLUMNS`."""


class Stage(StrEnum):
    """The stages of a load job, in the order they run."""

    UPLOADING = "uploading"
    IMPORTS = "imports"
    REASONING = "reasoning"
    LOADING = "loading"
    PRE_CACHING = "pre-caching"
    READY = "ready"


class Reasoner(StrEnum):
    """The reasoner an admin chooses for a job (A-3); `NONE` skips the reasoning stage."""

    NONE = "none"
    ELK = "elk"
    HERMIT = "hermit"


class Site(StrEnum):
    """What the site serves (U-16): the "loading" page, the "not available" page, or the ontology."""

    LOADING = "loading"
    NOT_AVAILABLE = "not available"
    AVAILABLE = "available"


@dataclass(frozen=True)
class ImportResult:
    """An ontology the served ontology imports, directly or not, and whether it was loaded (A-4).

    Attributes:
        iri: Its IRI, as declared in the `owl:imports` that imports it.
        imported_by: The IRI of the import that imports it; None if the upload does.
        reason: Why it was dropped, e.g. "only http and https imports are fetched"; None if it was fetched.
        triples: How many triples its named graph holds, once loaded; None before then, or if it was dropped.
    """

    iri: str
    imported_by: str | None = None
    reason: str | None = None
    triples: int | None = None

    @property
    def dropped(self) -> bool:
        """Whether it couldn't be fetched, or wasn't allowed, so it isn't part of the served ontology."""
        return self.reason is not None


def _imports_json(imports: tuple[ImportResult, ...]) -> str:
    return json.dumps([vars(result) for result in imports])


def _imports_from_json(text: str) -> tuple[ImportResult, ...]:
    return tuple(ImportResult(**result) for result in json.loads(text))


BACKGROUND_STAGES = (Stage.IMPORTS, Stage.REASONING, Stage.LOADING, Stage.PRE_CACHING)
"""The stages that run as registered functions, after the upload and before ready."""


@dataclass(frozen=True)
class JobState:
    """The state of the latest load job, and the latest generation that was loaded.

    Attributes:
        generation: The latest job's generation number; None if no job has run.
        stage: The latest job's current stage, or the stage it failed in or reached.
        started_at: When the latest job started.
        finished_at: When it finished, reaching ready or failing; None while it runs.
        failed_stage: The stage it failed in; None unless it failed.
        reason: Why it failed.
        ready_generation: The generation of the latest job that reached ready; None if none has.
        reasoner: The reasoner chosen for the latest job.
        upload_name: The name of the file uploaded for it, as the admin's browser gave it; None if it had none.
        upload_syntax: The file's syntax, e.g. "RDF/XML".
        upload_bytes: The file's size, or while uploading or after a failed upload, how much had arrived.
        imports: The ontology's imports, direct and indirect, once the imports stage has listed them.
        triples: How many triples the store's default graph holds, once loaded: the ontology without its imports.
    """

    generation: int | None = None
    stage: Stage | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failed_stage: Stage | None = None
    reason: str | None = None
    ready_generation: int | None = None
    reasoner: Reasoner = Reasoner.NONE
    upload_name: str | None = None
    upload_syntax: str | None = None
    upload_bytes: int | None = None
    imports: tuple[ImportResult, ...] = ()
    triples: int | None = None

    @property
    def dropped_imports(self) -> list[ImportResult]:
        """Return the imports that couldn't be fetched or weren't allowed."""
        return [result for result in self.imports if result.dropped]

    @property
    def running(self) -> bool:
        """Whether a job is running."""
        return self.started_at is not None and self.finished_at is None

    @property
    def site(self) -> Site:
        """Return what the site serves."""
        if self.running:
            return Site.LOADING
        if self.failed_stage is not None or self.ready_generation is None:
            return Site.NOT_AVAILABLE
        return Site.AVAILABLE

    def failed(self, reason: str) -> Self:
        """Return this state with the job failed in its current stage."""
        return replace(self, failed_stage=self.stage, reason=reason, finished_at=_now())


def _now() -> datetime:
    return datetime.now(UTC)


class JobStore:
    """The persistent SQLite database that holds the job state.

    Each call opens its own connection, so calls can run on any thread, and in any server worker.

    Args:
        path: The database file, created if missing; its schema is migrated to the latest version.

    Raises:
        RuntimeError: The database's schema is newer than this version of browl knows.
    """

    def __init__(self, path: Path) -> None:
        """Open the database, creating or migrating it as needed."""
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self) -> closing[sqlite3.Connection]:
        # Autocommit: each statement is its own transaction, and scripts manage theirs.
        return closing(sqlite3.connect(self.path, isolation_level=None))

    def _migrate(self) -> None:
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                raise RuntimeError(
                    f"{self.path} has schema version {version}; this browl knows up to {len(MIGRATIONS)}"
                )
            for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
                logger.info("Migrating %s to schema version %d", self.path, number)
                try:
                    connection.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
                except sqlite3.Error:
                    if connection.in_transaction:
                        connection.rollback()
                    raise

    def read(self) -> JobState:
        """Return the stored job state; the empty state if no job has run."""
        with self._connect() as connection:
            return self._read(connection)

    def write(self, state: JobState) -> None:
        """Store the job state, replacing the previous one.

        Raises:
            ValueError: The state has no job.
        """
        with self._connect() as connection:
            self._write(connection, state)

    def claim(
        self,
        started_at: datetime,
        reasoner: Reasoner = Reasoner.NONE,
        upload_name: str | None = None,
        upload_syntax: str | None = None,
    ) -> JobState:
        """Record the start of the next job, in the uploading stage, in one transaction, and return its state.

        The caller holds the job lock (`JobLock`), so a job still recorded as running belongs to a process that
        stopped without recording its end; it's replaced.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")  # takes the write lock first, so no other writer comes between
            try:
                previous = self._read(connection)
                if previous.running:
                    logger.warning("Load job %d was left running by a process that stopped", previous.generation)
                state = JobState(
                    generation=(previous.generation or 0) + 1,
                    stage=Stage.UPLOADING,
                    started_at=started_at,
                    ready_generation=previous.ready_generation,
                    reasoner=reasoner,
                    upload_name=upload_name,
                    upload_syntax=upload_syntax,
                )
                self._write(connection, state)
                connection.execute("COMMIT")
            except BaseException:
                connection.rollback()
                raise
        return state

    @staticmethod
    def _read(connection: sqlite3.Connection) -> JobState:
        row = connection.execute(f"SELECT {_COLUMNS} FROM job").fetchone()
        if row is None:
            return JobState()
        (
            generation,
            stage,
            started_at,
            finished_at,
            failed_stage,
            reason,
            ready_generation,
            reasoner,
            upload_name,
            upload_syntax,
            upload_bytes,
            imports,
            triples,
        ) = row
        return JobState(
            generation=generation,
            stage=Stage(stage),
            started_at=datetime.fromisoformat(started_at),
            finished_at=None if finished_at is None else datetime.fromisoformat(finished_at),
            failed_stage=None if failed_stage is None else Stage(failed_stage),
            reason=reason,
            ready_generation=ready_generation,
            reasoner=Reasoner(reasoner),
            upload_name=upload_name,
            upload_syntax=upload_syntax,
            upload_bytes=upload_bytes,
            imports=_imports_from_json(imports),
            triples=triples,
        )

    @staticmethod
    def _write(connection: sqlite3.Connection, state: JobState) -> None:
        if state.generation is None or state.stage is None or state.started_at is None:
            raise ValueError("a stored job state needs a generation, stage and start time")
        connection.execute(
            f"INSERT OR REPLACE INTO job (id, {_COLUMNS}) VALUES (1, {_PLACEHOLDERS})",
            (
                state.generation,
                state.stage,
                state.started_at.isoformat(),
                None if state.finished_at is None else state.finished_at.isoformat(),
                state.failed_stage,
                state.reason,
                state.ready_generation,
                state.reasoner,
                state.upload_name,
                state.upload_syntax,
                state.upload_bytes,
                _imports_json(state.imports),
                state.triples,
            ),
        )


class JobLock:
    """The job lock: an exclusive `flock` on a file, which the process running a job holds until its end is stored.

    The operating system releases it when the process ends, however it ends, so a job recorded as running while
    nobody holds the lock was interrupted. It works between processes on one host, as the server workers are.

    Args:
        path: The lock file, created if missing.
    """

    def __init__(self, path: Path) -> None:
        """Make the lock, not yet held."""
        self.path = path
        self._file: IO[bytes] | None = None

    def acquire(self) -> bool:
        """Take the lock if no one holds it, without waiting; return whether it was taken."""
        file = self.path.open("ab")
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            file.close()
            return False
        self._file = file
        return True

    def release(self) -> None:
        """Release the lock, if held."""
        if self._file is not None:
            self._file.close()  # which releases the flock
            self._file = None


@dataclass
class JobContext:
    """What a job's stage functions get.

    Attributes:
        generation: The job's generation number, one more than the previous job's.
        settings: The app's settings.
        reasoner: The reasoner the admin chose.
        upload: The uploaded file, its suffix naming its format (`browl.upload.FORMATS`); None until it's stored.
        upload_bytes: How much of the file has arrived.
        converted: An OWL Functional Syntax upload converted to Turtle by the imports stage, which stands in for it
            from then on; None for any other upload.
        reasoned: The reasoned ontology the reasoning stage made; None if it made none.
        catalog: The catalog that maps each import in the closure to a local file, written by the imports stage.
        imports: The imports, direct and indirect, listed by the imports stage.
        import_files: The RDF file to load for each import that was fetched, by its IRI.
        triples: How many triples the store's default graph holds, once loaded.
    """

    generation: int
    settings: Settings
    reasoner: Reasoner = Reasoner.NONE
    upload: Path | None = None
    upload_bytes: int | None = None
    converted: Path | None = None
    reasoned: Path | None = None
    catalog: Path | None = None
    imports: list[ImportResult] = field(default_factory=list)
    import_files: dict[str, Path] = field(default_factory=dict)
    triples: int | None = None

    @property
    def ontology(self) -> Path | None:
        """Return the ontology to serve: the reasoned one if reasoning ran, else the upload, converted if it's OFN."""
        return self.reasoned or self.converted or self.upload


StageFunction = Callable[[JobContext], Awaitable[None]]
"""A stage's work. It runs on the event loop, so it runs blocking work in a thread or subprocess."""


class JobRunningError(RuntimeError):
    """A job was started while another one runs."""


class StageError(Exception):
    """A stage failed for a reason worth showing to admins as it is (A-6), e.g. "time limit"."""


@asynccontextmanager
async def time_limit(seconds: float, operation: str) -> AsyncIterator[None]:
    """Fail the stage if the block takes longer than `seconds`, with a reason that names the operation (A-6).

    Stage code wraps each step that can hang in one, e.g. `time_limit(60, "Fuseki HTTP request POST /$/datasets")`,
    whose reason would be "Fuseki HTTP request POST /$/datasets timed out after 60 s". Other timeouts get a generic
    reason (see `_reason`).

    Raises:
        StageError: The block took too long.
    """
    timeout = asyncio.timeout(seconds)
    try:
        async with timeout:
            yield
    except TimeoutError as error:
        if not timeout.expired():
            raise
        raise StageError(f"{operation} timed out after {seconds:g} s") from error


def _reason(error: BaseException) -> str:
    if isinstance(error, StageError):
        return str(error)
    if isinstance(error, httpx2.TimeoutException):
        return _http_timeout_reason(error)
    if isinstance(error, TimeoutError):
        return f"timed out: {error}" if str(error) else "timed out; the app's log shows where"
    name = f"internal error: {type(error).__name__}"
    return f"{name}: {error}" if str(error) else name


def _http_timeout_reason(error: httpx2.TimeoutException) -> str:
    try:
        request = error.request
    except RuntimeError:  # not set
        return f"HTTP request timed out ({type(error).__name__})"
    url = request.url.copy_with(userinfo=b"", query=None)
    phase = type(error).__name__.removesuffix("Timeout").lower()  # connect, read, write or pool
    limit = request.extensions.get("timeout", {}).get(phase)
    after = "" if limit is None else f" after {limit:g} s"
    return f"HTTP request {request.method} {url} timed out{after} ({type(error).__name__})"


class JobRunner:
    """Run one load job at a time, recording its state in a `JobStore`, the state's source of truth.

    Make one with `open`, which marks failed a job that a stopped process left running. Each server worker has a
    runner; a job runs in the worker that started it, which holds the `JobLock` until the job's end is stored.

    Args:
        store: The persistent database.
        lock: The job lock.
        settings: The app's settings, given to the stage functions.
        state: The state read from the store.
        stages: Stage functions for the background stages; more can be registered later.
    """

    def __init__(
        self,
        store: JobStore,
        lock: JobLock,
        settings: Settings,
        state: JobState,
        stages: Mapping[Stage, StageFunction] | None = None,
    ) -> None:
        """Make a runner with no job running."""
        self._store = store
        self._lock = lock
        self._settings = settings
        self._stages: dict[Stage, StageFunction] = {}
        for stage, function in (stages or {}).items():
            self.register(stage, function)
        self._busy = False  # this runner has a job, from claiming it until its end is stored
        self._closing = False
        self._task: asyncio.Task[None] | None = None
        self._writes: set[asyncio.Task[None]] = set()
        self._unstored: JobState | None = None  # the end of this runner's last job, if it couldn't be stored
        self._progress_write: asyncio.Future[None] | None = None  # the running upload's last progress write
        self._progress_at = 0.0  # when the running upload's progress was last recorded
        self._refreshing = asyncio.Lock()
        self._changes = 0
        self._set(state)

    @classmethod
    async def open(cls, settings: Settings, stages: Mapping[Stage, StageFunction] | None = None) -> Self:
        """Open the persistent database and job lock in `DATA_DIR`, mark failed an interrupted job, and return a runner.

        A job recorded as running is interrupted unless another process holds the job lock, as another server
        worker running it would.

        Raises:
            RuntimeError: The database's schema is newer than this version of browl knows.
        """
        store = await asyncio.to_thread(JobStore, settings.data_dir / STATE_DB)
        lock = JobLock(settings.data_dir / JOB_LOCK)
        state = await asyncio.to_thread(_recover, store, lock)
        return cls(store, lock, settings, state, stages)

    @property
    def state(self) -> JobState:
        """Return the job state as this runner last read or changed it, without reading the database."""
        return self._state

    async def current(self) -> JobState:
        """Return the job state, reading the database at most every `STATE_TTL_S` (this runner's changes show at once).

        While this runner has a job, it is the only writer, so its own state is the stored one.
        """
        if self._busy or time.monotonic() - self._read_at < STATE_TTL_S:
            return self._state
        async with self._refreshing:
            if self._busy or time.monotonic() - self._read_at < STATE_TTL_S:
                return self._state
            changes = self._changes
            state = await asyncio.to_thread(self._store.read)
            if self._changes == changes:  # else this runner changed it meanwhile, which is newer
                unstored = self._unstored
                if unstored is not None and state.running and state.generation == unstored.generation:
                    state = unstored
                else:
                    self._unstored = None
                self._set(state)
        return self._state

    def register(self, stage: Stage, function: StageFunction) -> None:
        """Make the function do the stage's work, replacing any registered before.

        Raises:
            ValueError: The stage isn't one of `BACKGROUND_STAGES`.
        """
        if stage not in BACKGROUND_STAGES:
            raise ValueError(f"no function can be registered for stage {stage}")
        self._stages[stage] = function

    @asynccontextmanager
    async def upload(
        self, reasoner: Reasoner = Reasoner.NONE, name: str | None = None, syntax: str | None = None
    ) -> AsyncIterator[JobContext]:
        """Start a job in the uploading stage; on leaving the block, run its other stages in the background.

        The block stores the file, sets the context's `upload` and `upload_bytes`, and records the size as it grows
        (`record_upload_progress`).

        A request handler streams the upload inside the block, as it can't hand its request body to a background task.
        If the block raises, the job fails in the uploading stage and the exception propagates. A time limit on the
        upload belongs inside the block (`time_limit`), so the reason names it; a cancellation from outside is
        recorded only as "cancelled".

        Raises:
            JobRunningError: A job is already running, in this server worker or another.
        """
        if self._busy:
            raise JobRunningError(f"load job {self._state.generation} is running, in stage {self._state.stage}")
        self._busy = True  # set before any await, so a concurrent call is refused
        await self._claim(reasoner, name, syntax)
        context = JobContext(self._state.generation or 0, self._settings, reasoner)
        logger.info("Load job %d started", context.generation)
        self._progress_at = time.monotonic()
        try:
            try:
                yield context
            finally:
                self._set(replace(self._state, upload_bytes=context.upload_bytes))  # stored with the end or next stage
        except BaseException as error:
            try:
                await self._fail(error)
            finally:
                self._settle()
            raise
        self._task = asyncio.create_task(self._run(context), name=f"load job {context.generation}")

    async def start(self, reasoner: Reasoner = Reasoner.NONE) -> JobContext:
        """Start a job with nothing to upload, running all its other stages in the background.

        Raises:
            JobRunningError: A job is already running, in this server worker or another.
        """
        async with self.upload(reasoner) as context:
            return context

    async def wait(self) -> None:
        """Wait for the running job, if any, to finish."""
        if self._task is not None:
            await asyncio.shield(self._task)

    async def close(self) -> None:
        """Cancel the running job, if any, recording it as interrupted by shutdown; wait for its end to be stored."""
        self._closing = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._settle()  # if the task was cancelled before it started, it recorded nothing
        await asyncio.gather(*self._writes)

    async def record_upload_progress(self, context: JobContext) -> None:
        """Record the running upload's size so far, if it has grown and `UPLOAD_PROGRESS_S` has passed since last time.

        The upload handler calls it as data arrives. A failed write is logged and skipped: the status just shows an
        older size.
        """
        now = time.monotonic()
        if now - self._progress_at < UPLOAD_PROGRESS_S or context.upload_bytes == self._state.upload_bytes:
            return
        self._progress_at = now
        state = replace(self._state, upload_bytes=context.upload_bytes)
        self._set(state)
        # A cancelled await leaves the thread writing, so the job's end waits for it (`_store_end`).
        write = self._progress_write = asyncio.ensure_future(asyncio.to_thread(self._store.write, state))
        try:
            await asyncio.shield(write)
        except Exception:
            logger.warning("Couldn't record load job %d's upload progress", context.generation, exc_info=True)

    def _set(self, state: JobState) -> None:
        self._state = state
        self._read_at = time.monotonic()
        self._changes += 1

    async def _claim(self, reasoner: Reasoner, name: str | None, syntax: str | None) -> None:
        """Take the job lock and record the job's start, in a thread that runs to its end even if this is cancelled.

        A job claimed after this was cancelled is failed as cancelled, so it doesn't hold the lock for good.

        Raises:
            JobRunningError: Another process holds the job lock.
        """
        claim = asyncio.ensure_future(asyncio.to_thread(self._claim_in_thread, _now(), reasoner, name, syntax))
        try:
            self._set(await asyncio.shield(claim))
        except asyncio.CancelledError:
            claim.add_done_callback(self._abandon_claim)
            raise
        except BaseException:
            self._busy = False
            raise

    def _claim_in_thread(
        self, started_at: datetime, reasoner: Reasoner, name: str | None, syntax: str | None
    ) -> JobState:
        if not self._lock.acquire():
            raise JobRunningError("a load job is running in another server worker")
        try:
            return self._store.claim(started_at, reasoner, name, syntax)
        except BaseException:
            self._lock.release()
            raise

    def _abandon_claim(self, claim: asyncio.Future[JobState]) -> None:
        if claim.cancelled() or claim.exception() is not None:
            self._busy = False
            return
        self._set(claim.result())
        self._record_end(self._state.failed(CANCELLED))

    async def _record(self, state: JobState) -> None:
        await asyncio.to_thread(self._store.write, state)
        self._set(state)

    async def _fail(self, error: BaseException) -> None:
        if isinstance(error, asyncio.CancelledError):
            reason = INTERRUPTED_BY_SHUTDOWN if self._closing else CANCELLED
            logger.warning("Load job %d was %s in stage %s", self._state.generation, reason, self._state.stage)
        else:
            reason = _reason(error)
            logger.error(
                "Load job %d failed in stage %s: %s",
                self._state.generation,
                self._state.stage,
                reason,
                exc_info=not isinstance(error, StageError),
            )
        if len(reason) > MAX_REASON_CHARS:
            reason = f"{reason[:MAX_REASON_CHARS]}… (cut short; the app's log has it all)"
        # Shielded: a second cancellation, e.g. from an anyio cancel scope, mustn't stop the failure being stored.
        await asyncio.shield(self._record_end(self._state.failed(reason)))

    def _record_end(self, state: JobState) -> asyncio.Task[None]:
        """Show the job's end at once, so the site stops serving "loading", and store it in a task of its own.

        Once it's stored, or couldn't be, the task frees the runner and releases the job lock.
        """
        self._set(state)
        task = asyncio.create_task(self._store_end(state))
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)
        return task

    async def _store_end(self, state: JobState) -> None:
        try:
            if self._progress_write is not None:
                await asyncio.wait({self._progress_write})  # so it can't land after, and overwrite, the end
            await asyncio.to_thread(self._store.write, state)
        except Exception:
            # Other workers keep reading it as running until the next job or a restart; this one serves its end.
            logger.exception("Couldn't record the end of load job %d", state.generation)
            self._unstored = state
        finally:
            self._unlock()

    def _unlock(self) -> None:
        self._lock.release()
        self._busy = False

    def _settle(self) -> None:
        """Fail a job that stopped while still recorded as running, so the site can't stay on "loading" for good."""
        if self._state.running:
            logger.error(
                "Load job %d stopped in stage %s without recording why", self._state.generation, self._state.stage
            )
            self._record_end(self._state.failed(INTERRUPTED_BY_SHUTDOWN if self._closing else STOPPED_UNRECORDED))

    def _with_results(self, context: JobContext) -> JobState:
        """Return the state with what the stages have found so far: the imports and the triples loaded."""
        return replace(self._state, imports=tuple(context.imports), triples=context.triples)

    async def _run(self, context: JobContext) -> None:
        try:
            for stage in BACKGROUND_STAGES:
                await self._record(replace(self._with_results(context), stage=stage))
                if function := self._stages.get(stage):
                    logger.info("Load job %d: %s", context.generation, stage)
                    try:
                        await function(context)
                    finally:
                        self._set(self._with_results(context))  # stored with the next stage, or the job's end
            await self._record(
                replace(self._state, stage=Stage.READY, finished_at=_now(), ready_generation=context.generation)
            )
            self._unlock()
            logger.info("Load job %d is ready", context.generation)
        except asyncio.CancelledError as error:
            await self._fail(error)
            raise
        except Exception as error:  # noqa: BLE001  # any stage failure fails the job; _fail logs it
            await self._fail(error)
        finally:
            self._settle()


def _recover(store: JobStore, lock: JobLock) -> JobState:
    """Return the stored job state, failing a job recorded as running unless another process holds the job lock."""
    if not lock.acquire():
        return store.read()  # another server worker is running the job
    try:
        state = store.read()
        if state.running:
            logger.warning("Load job %d was interrupted by a restart in stage %s", state.generation, state.stage)
            state = state.failed(INTERRUPTED_BY_RESTART)
            store.write(state)
        return state
    finally:
        lock.release()
