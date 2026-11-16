"""The load job (DESIGN.md §2.4): its stages, its state in a persistent SQLite database, and the runner.

One job runs at a time, as an asyncio task in the app process. The upload is streamed by a request handler, which can't
hand its request body to a background task, so a job starts in two steps: `JobRunner.upload()` claims the runner and
records the uploading stage, the handler streams the file inside it, and leaving it starts the remaining stages in the
background. The other stages are async functions registered with `JobRunner.register`; a stage with none registered
passes at once.
"""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, closing, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from browl.settings import Settings

logger = logging.getLogger(__name__)

STATE_DB = "state.sqlite3"
"""The persistent database's file name in `DATA_DIR`: job state, and later admins (§2.11)."""

INTERRUPTED_BY_RESTART = "interrupted by restart"
INTERRUPTED_BY_SHUTDOWN = "interrupted by shutdown"

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
]
"""The persistent database's schema migrations: entry n migrates from version n to n + 1."""


class Stage(StrEnum):
    """The stages of a load job, in the order they run."""

    UPLOADING = "uploading"
    IMPORTS = "imports"
    REASONING = "reasoning"
    LOADING = "loading"
    PRE_CACHING = "pre-caching"
    READY = "ready"


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
    """

    generation: int | None = None
    stage: Stage | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failed_stage: Stage | None = None
    reason: str | None = None
    ready_generation: int | None = None

    @property
    def running(self) -> bool:
        """Whether a job is running."""
        return self.started_at is not None and self.finished_at is None

    @property
    def site(self) -> Literal["loading", "not available", "available"]:
        """Return what the site serves: the "loading" page, the "not available" page, or the ontology (U-16)."""
        if self.running:
            return "loading"
        if self.failed_stage is not None or self.ready_generation is None:
            return "not available"
        return "available"

    def failed(self, reason: str) -> Self:
        """Return this state with the job failed in its current stage."""
        return replace(self, failed_stage=self.stage, reason=reason, finished_at=_now())


def _now() -> datetime:
    return datetime.now(UTC)


class JobStore:
    """The persistent SQLite database that holds the job state.

    Each call opens its own connection, so calls can run on any thread.

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

    @property
    def version(self) -> int:
        """Return the database's schema version."""
        with self._connect() as connection:
            version: int = connection.execute("PRAGMA user_version").fetchone()[0]
            return version

    def read(self) -> JobState:
        """Return the stored job state; the empty state if no job has run."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT generation, stage, started_at, finished_at, failed_stage, reason, ready_generation FROM job"
            ).fetchone()
        if row is None:
            return JobState()
        generation, stage, started_at, finished_at, failed_stage, reason, ready_generation = row
        return JobState(
            generation=generation,
            stage=Stage(stage),
            started_at=datetime.fromisoformat(started_at),
            finished_at=None if finished_at is None else datetime.fromisoformat(finished_at),
            failed_stage=None if failed_stage is None else Stage(failed_stage),
            reason=reason,
            ready_generation=ready_generation,
        )

    def write(self, state: JobState) -> None:
        """Store the job state, replacing the previous one.

        Raises:
            ValueError: The state has no job.
        """
        if state.generation is None or state.stage is None or state.started_at is None:
            raise ValueError("a stored job state needs a generation, stage and start time")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO job"
                " (id, generation, stage, started_at, finished_at, failed_stage, reason, ready_generation)"
                " VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                (
                    state.generation,
                    state.stage,
                    state.started_at.isoformat(),
                    None if state.finished_at is None else state.finished_at.isoformat(),
                    state.failed_stage,
                    state.reason,
                    state.ready_generation,
                ),
            )


@dataclass(frozen=True)
class JobContext:
    """What a job's stage functions get.

    Attributes:
        generation: The job's generation number, one more than the previous job's.
        settings: The app's settings.
    """

    generation: int
    settings: Settings


StageFunction = Callable[[JobContext], Awaitable[None]]
"""A stage's work. It runs on the event loop, so it runs blocking work in a thread or subprocess."""


class JobRunningError(RuntimeError):
    """A job was started while another one runs."""


class StageError(Exception):
    """A stage failed for a reason worth showing to admins as it is (A-6), e.g. "time limit"."""


def _reason(error: BaseException) -> str:
    if isinstance(error, StageError):
        return str(error)
    return (
        f"internal error: {type(error).__name__}: {error}" if str(error) else f"internal error: {type(error).__name__}"
    )


class JobRunner:
    """Run one load job at a time, recording its state in a `JobStore`.

    Make one with `open`, which marks failed a job that a previous process left running.

    Args:
        store: The persistent database.
        settings: The app's settings, given to the stage functions.
        state: The state read from the store.
        stages: Stage functions for the background stages; more can be registered later.
    """

    def __init__(
        self,
        store: JobStore,
        settings: Settings,
        state: JobState,
        stages: Mapping[Stage, StageFunction] | None = None,
    ) -> None:
        """Make a runner with no job running."""
        self._store = store
        self._settings = settings
        self._state = state
        self._stages: dict[Stage, StageFunction] = {}
        for stage, function in (stages or {}).items():
            self.register(stage, function)
        self._busy = False
        self._task: asyncio.Task[None] | None = None

    @classmethod
    async def open(cls, settings: Settings, stages: Mapping[Stage, StageFunction] | None = None) -> Self:
        """Open the persistent database in `DATA_DIR`, mark failed a job that was running, and return a runner.

        No job survives a restart, so one the database records as running was interrupted.

        Raises:
            RuntimeError: The database's schema is newer than this version of browl knows.
        """
        store = await asyncio.to_thread(JobStore, settings.data_dir / STATE_DB)
        state = await asyncio.to_thread(store.read)
        if state.running:
            logger.warning("Load job %d was interrupted by a restart in stage %s", state.generation, state.stage)
            state = state.failed(INTERRUPTED_BY_RESTART)
            await asyncio.to_thread(store.write, state)
        return cls(store, settings, state, stages)

    @property
    def state(self) -> JobState:
        """Return the job state, as last stored."""
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
    async def upload(self) -> AsyncIterator[JobContext]:
        """Start a job in the uploading stage; on leaving the block, run its other stages in the background.

        The caller streams the upload inside the block. If the block raises, the job fails in the uploading stage
        and the exception propagates.

        Raises:
            JobRunningError: A job is already running.
        """
        if self._busy:
            raise JobRunningError(f"load job {self._state.generation} is running, in stage {self._state.stage}")
        self._busy = True  # set before any await, so a concurrent call is refused
        try:
            generation = (self._state.generation or 0) + 1
            await self._record(
                JobState(
                    generation=generation,
                    stage=Stage.UPLOADING,
                    started_at=_now(),
                    ready_generation=self._state.ready_generation,
                )
            )
            logger.info("Load job %d started", generation)
            context = JobContext(generation, self._settings)
            try:
                yield context
            except BaseException as error:
                await self._fail(error)
                raise
        except BaseException:
            self._busy = False
            raise
        self._task = asyncio.create_task(self._run(context), name=f"load job {generation}")

    async def start(self) -> JobContext:
        """Start a job with nothing to upload, running all its other stages in the background.

        Raises:
            JobRunningError: A job is already running.
        """
        async with self.upload() as context:
            return context

    async def wait(self) -> None:
        """Wait for the running job, if any, to finish."""
        if self._task is not None:
            await asyncio.shield(self._task)

    async def close(self) -> None:
        """Cancel the running job, if any, recording it as interrupted by shutdown."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _record(self, state: JobState) -> None:
        await asyncio.to_thread(self._store.write, state)
        self._state = state

    async def _fail(self, error: BaseException) -> None:
        if isinstance(error, asyncio.CancelledError):
            reason = INTERRUPTED_BY_SHUTDOWN
            logger.warning("Load job %d was cancelled in stage %s", self._state.generation, self._state.stage)
        else:
            reason = _reason(error)
            logger.error(
                "Load job %d failed in stage %s: %s",
                self._state.generation,
                self._state.stage,
                reason,
                exc_info=not isinstance(error, StageError),
            )
        state = self._state.failed(reason)
        try:
            await self._record(state)
        except Exception:
            logger.exception("Couldn't record the failure of load job %d", state.generation)
            self._state = state  # the site still stops serving

    async def _run(self, context: JobContext) -> None:
        try:
            for stage in BACKGROUND_STAGES:
                await self._record(replace(self._state, stage=stage))
                if function := self._stages.get(stage):
                    logger.info("Load job %d: %s", context.generation, stage)
                    await function(context)
            await self._record(
                replace(self._state, stage=Stage.READY, finished_at=_now(), ready_generation=context.generation)
            )
            logger.info("Load job %d is ready", context.generation)
        except asyncio.CancelledError as error:
            await self._fail(error)
            raise
        except Exception as error:  # noqa: BLE001  # any stage failure fails the job; _fail logs it
            await self._fail(error)
        finally:
            self._busy = False
