"""Tests for the load job: its persistent state, schema migrations, and the runner."""

import asyncio
import itertools
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Mapping
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest

import browl.jobs
from browl.jobs import (
    CANCELLED,
    INTERRUPTED_BY_RESTART,
    INTERRUPTED_BY_SHUTDOWN,
    JOB_LOCK,
    STATE_DB,
    STOPPED_UNRECORDED,
    ImportResult,
    JobContext,
    JobLock,
    JobRunner,
    JobRunningError,
    JobState,
    JobStore,
    Reasoner,
    Site,
    Stage,
    StageError,
    StageFunction,
    time_limit,
)
from tests.helpers import make_settings

pytestmark = pytest.mark.anyio

STARTED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


async def open_runner(data_dir: Path, stages: Mapping[Stage, StageFunction] | None = None) -> JobRunner:
    """Open a runner on the database in `data_dir`, the `DATA_DIR` that `make_settings` reads, with these functions."""
    return await JobRunner.open(make_settings(), stages)


async def run_job(runner: JobRunner) -> JobState:
    """Start a job, wait for it to finish, and return the runner's state."""
    await runner.start()
    await runner.wait()
    return runner.state


def schema_version(path: Path) -> int:
    """Return the schema version of the database at `path`."""
    with closing(sqlite3.connect(path)) as connection:
        version: int = connection.execute("PRAGMA user_version").fetchone()[0]
        return version


def test_store_migrates_new_database(data_dir: Path) -> None:
    """Check that a new database is created at the latest schema version, with no job."""
    store = JobStore(data_dir / "sub" / STATE_DB)
    assert schema_version(store.path) == len(browl.jobs.MIGRATIONS) == 3
    assert store.read() == JobState()


def test_store_migrates_forward(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that reopening a database runs only the migrations past its version, keeping its data."""
    path = data_dir / STATE_DB
    state = JobState(generation=1, stage=Stage.READY, started_at=STARTED, finished_at=STARTED, ready_generation=1)
    JobStore(path).write(state)
    monkeypatch.setattr(browl.jobs, "MIGRATIONS", [*browl.jobs.MIGRATIONS, "CREATE TABLE admin (email TEXT);"])
    store = JobStore(path)
    assert schema_version(path) == len(browl.jobs.MIGRATIONS)
    assert store.read() == state
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM admin").fetchone() == (0,)


def test_store_rolls_back_failed_migration(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a failing migration leaves the database at its previous version, with none of its changes."""
    path = data_dir / STATE_DB
    JobStore(path)
    bad = "CREATE TABLE admin (email TEXT); INSERT INTO missing VALUES (1);"
    monkeypatch.setattr(browl.jobs, "MIGRATIONS", [*browl.jobs.MIGRATIONS, bad])
    with pytest.raises(sqlite3.OperationalError, match="missing"):
        JobStore(path)
    assert schema_version(path) == len(browl.jobs.MIGRATIONS) - 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'admin'").fetchone() is None


def test_store_refuses_newer_schema(data_dir: Path) -> None:
    """Check that a database from a newer browl is refused, not used."""
    path = data_dir / STATE_DB
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="schema version 99"):
        JobStore(path)


def test_store_refuses_state_without_job(data_dir: Path) -> None:
    """Check that the empty state can't be stored."""
    with pytest.raises(ValueError, match="generation"):
        JobStore(data_dir / STATE_DB).write(JobState())


@pytest.mark.parametrize(
    ("state", "site"),
    [
        (JobState(), Site.NOT_AVAILABLE),
        (JobState(generation=1, stage=Stage.LOADING, started_at=STARTED), Site.LOADING),
        (JobState(generation=2, stage=Stage.IMPORTS, started_at=STARTED, ready_generation=1), Site.LOADING),
        (
            JobState(generation=1, stage=Stage.READY, started_at=STARTED, finished_at=STARTED, ready_generation=1),
            Site.AVAILABLE,
        ),
        (
            JobState(
                generation=2,
                stage=Stage.LOADING,
                started_at=STARTED,
                finished_at=STARTED,
                failed_stage=Stage.LOADING,
                reason="boom",
                ready_generation=1,
            ),
            Site.NOT_AVAILABLE,
        ),
    ],
    ids=["never-loaded", "first-load", "reload", "ready", "failed"],
)
def test_site(state: JobState, site: Site) -> None:
    """Check what the site serves in each job state."""
    assert state.site == site


async def test_runs_stages_in_order(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a job records each stage before running its function, in order, and ends ready."""
    times = (STARTED + timedelta(seconds=n) for n in itertools.count())
    monkeypatch.setattr(browl.jobs, "_now", lambda: next(times))
    seen: list[tuple[Stage, Stage | None, int]] = []
    runner: JobRunner

    def stage(name: Stage) -> StageFunction:
        async def run(context: JobContext) -> None:
            seen.append((name, runner.state.stage, context.generation))

        return run

    runner = await open_runner(data_dir, {Stage.IMPORTS: stage(Stage.IMPORTS), Stage.LOADING: stage(Stage.LOADING)})
    runner.register(Stage.PRE_CACHING, stage(Stage.PRE_CACHING))
    state = await run_job(runner)
    assert seen == [
        (Stage.IMPORTS, Stage.IMPORTS, 1),
        (Stage.LOADING, Stage.LOADING, 1),
        (Stage.PRE_CACHING, Stage.PRE_CACHING, 1),
    ]
    assert state.stage == Stage.READY
    assert state.ready_generation == state.generation == 1
    assert state.failed_stage is None
    assert (state.started_at, state.finished_at) == (STARTED, STARTED + timedelta(seconds=1))
    assert state.site == Site.AVAILABLE
    assert (await run_job(runner)).ready_generation == 2


async def test_state_persists(data_dir: Path) -> None:
    """Check that a reopened runner reads the state the previous one stored."""
    state = await run_job(await open_runner(data_dir))
    assert (await open_runner(data_dir)).state == state


async def test_refuses_second_job(data_dir: Path) -> None:
    """Check that a job can't start while one runs, and can once it has finished."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(context: JobContext) -> None:
        entered.set()
        await release.wait()

    runner = await open_runner(data_dir, {Stage.REASONING: blocked})
    await runner.start()
    async with asyncio.timeout(5):
        await entered.wait()
    assert (runner.state.stage, runner.state.site) == (Stage.REASONING, Site.LOADING)
    with pytest.raises(JobRunningError, match="running"):
        await runner.start()
    with pytest.raises(JobRunningError):
        async with runner.upload():
            pass
    release.set()
    await runner.wait()
    assert runner.state.generation == 1
    assert (await run_job(runner)).generation == 2


async def test_refuses_second_job_while_starting(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a job can't start while another is still recording its start."""
    runner = await open_runner(data_dir)
    entered, release = threading.Event(), threading.Event()
    claim = runner._store.claim

    def blocked(*args: Any) -> JobState:
        entered.set()
        release.wait(timeout=5)
        return claim(*args)

    monkeypatch.setattr(runner._store, "claim", blocked)
    first = asyncio.create_task(runner.start())
    assert await asyncio.to_thread(entered.wait, 5)
    with pytest.raises(JobRunningError):
        await runner.start()
    release.set()
    assert (await first).generation == 1
    await runner.wait()
    assert runner.state.site == Site.AVAILABLE


async def test_start_not_stored(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a job whose start can't be stored doesn't start, and frees the runner."""
    runner = await open_runner(data_dir)
    write = JobStore._write

    def broken(connection: sqlite3.Connection, state: JobState) -> None:
        write(connection, state)  # rolled back
        monkeypatch.setattr(JobStore, "_write", staticmethod(write))
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(JobStore, "_write", staticmethod(broken))
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        await runner.start()
    assert runner.state == JobState() == JobStore(data_dir / STATE_DB).read()
    assert (await run_job(runner)).site == Site.AVAILABLE


async def test_upload_stage(data_dir: Path) -> None:
    """Check that the upload block runs in the uploading stage, and the other stages only after it."""
    runner = await open_runner(data_dir)
    async with runner.upload() as context:
        assert context.generation == 1
        assert runner.state.stage == Stage.UPLOADING
        assert runner.state.site == Site.LOADING
    await runner.wait()
    assert runner.state.stage == Stage.READY


async def test_reasoner_choice(data_dir: Path) -> None:
    """Check that a job's reasoner reaches its stage functions and its stored state, and the next job's replaces it."""
    seen: list[Reasoner] = []

    async def reasoning(context: JobContext) -> None:
        seen.append(context.reasoner)

    runner = await open_runner(data_dir, {Stage.REASONING: reasoning})
    assert runner.state.reasoner == Reasoner.NONE
    async with runner.upload(Reasoner.HERMIT) as context:
        assert context.reasoner == runner.state.reasoner == Reasoner.HERMIT
        assert JobStore(data_dir / STATE_DB).read().reasoner == Reasoner.HERMIT
    await runner.wait()
    assert seen == [Reasoner.HERMIT]
    assert (await open_runner(data_dir)).state.reasoner == Reasoner.HERMIT
    await runner.start(Reasoner.ELK)
    await runner.wait()
    assert seen == [Reasoner.HERMIT, Reasoner.ELK]
    assert runner.state.reasoner == Reasoner.ELK


def test_store_migrates_version_1(data_dir: Path) -> None:
    """Check that a version 1 database migrates, its job read as having no reasoner and no upload recorded."""
    path = data_dir / STATE_DB
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            f"BEGIN;\n{browl.jobs.MIGRATIONS[0]}\nPRAGMA user_version = 1;\n"
            "INSERT INTO job VALUES (1, 4, 'ready', '2026-01-02T03:04:05+00:00', '2026-01-02T03:04:06+00:00',"
            " NULL, NULL, 4);\nCOMMIT;"
        )
    state = JobStore(path).read()
    assert (state.generation, state.reasoner) == (4, Reasoner.NONE)
    assert (state.upload_name, state.upload_syntax, state.upload_bytes) == (None, None, None)
    assert (state.imports, state.triples) == ((), None)
    assert schema_version(path) == len(browl.jobs.MIGRATIONS)


def test_store_migrates_version_2(data_dir: Path) -> None:
    """Check that a version 2 database migrates, its job keeping its upload and read as having no imports or count."""
    path = data_dir / STATE_DB
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            f"BEGIN;\n{browl.jobs.MIGRATIONS[0]}\n{browl.jobs.MIGRATIONS[1]}\nPRAGMA user_version = 2;\n"
            "INSERT INTO job VALUES (1, 4, 'ready', '2026-01-02T03:04:05+00:00', '2026-01-02T03:04:06+00:00',"
            " NULL, NULL, 4, 'elk', 'o.ttl', 'Turtle', 123);\nCOMMIT;"
        )
    state = JobStore(path).read()
    assert (state.generation, state.reasoner) == (4, Reasoner.ELK)
    assert (state.upload_name, state.upload_syntax, state.upload_bytes) == ("o.ttl", "Turtle", 123)
    assert (state.imports, state.triples) == ((), None)
    assert schema_version(path) == len(browl.jobs.MIGRATIONS)


async def test_stage_results_recorded(data_dir: Path) -> None:
    """Check that the imports and triple counts a stage sets are stored, also when a later stage fails."""
    imports = [ImportResult("http://a/", None, None, 3), ImportResult("file:/x", "http://a/", "not allowed")]

    async def listing(context: JobContext) -> None:
        context.imports = imports

    async def loading(context: JobContext) -> None:
        context.triples = 7
        raise StageError("boom")

    runner = await open_runner(data_dir, {Stage.IMPORTS: listing, Stage.LOADING: loading})
    await runner.start()
    await runner.wait()
    stored = JobStore(data_dir / STATE_DB).read()
    assert (stored.failed_stage, stored.imports, stored.triples) == (Stage.LOADING, tuple(imports), 7)
    assert stored.dropped_imports == [imports[1]]
    assert imports[1].dropped and not imports[0].dropped


async def test_upload_recorded(data_dir: Path) -> None:
    """Check that an upload's name and syntax are stored at the job's start, and its size when the block ends."""
    runner = await open_runner(data_dir)
    async with runner.upload(Reasoner.NONE, "onto.ttl", "Turtle") as context:
        stored = JobStore(data_dir / STATE_DB).read()
        assert (stored.upload_name, stored.upload_syntax, stored.upload_bytes) == ("onto.ttl", "Turtle", None)
        context.upload_bytes = 1234
    await runner.wait()
    stored = JobStore(data_dir / STATE_DB).read()
    assert (stored.stage, stored.upload_name, stored.upload_bytes) == (Stage.READY, "onto.ttl", 1234)
    await runner.start()
    await runner.wait()
    assert (runner.state.upload_name, runner.state.upload_bytes) == (None, None)


async def test_upload_recorded_on_failure(data_dir: Path) -> None:
    """Check that a failed upload's size so far is stored with its failure."""
    runner = await open_runner(data_dir)
    with pytest.raises(StageError):
        async with runner.upload(Reasoner.NONE, "onto.ttl", "Turtle") as context:
            context.upload_bytes = 99
            raise StageError("cut short")
    await runner.close()
    stored = JobStore(data_dir / STATE_DB).read()
    assert (stored.failed_stage, stored.upload_bytes) == (Stage.UPLOADING, 99)


async def test_upload_failure(data_dir: Path) -> None:
    """Check that an error in the upload block fails the job in the uploading stage, and frees the runner."""
    runner = await open_runner(data_dir)
    with pytest.raises(OSError, match="disk full"):
        async with runner.upload():
            raise OSError("disk full")
    assert runner.state.failed_stage == Stage.UPLOADING
    assert runner.state.reason == "internal error: OSError: disk full"
    assert (await run_job(runner)).site == Site.AVAILABLE


async def upload_slowly(runner: JobRunner) -> None:
    """Take a second over an upload."""
    async with runner.upload():
        await asyncio.sleep(1)


@pytest.mark.parametrize("scope", ["anyio", "asyncio", "task"])
async def test_upload_cancelled(data_dir: Path, scope: str) -> None:
    """Check that an upload cancelled from outside is recorded as cancelled, and frees the runner.

    It's cancelled by a cancel scope, a time limit, or cancelling the request's task, as a server may when a client
    disconnects. An anyio cancel scope also cancels the failure record, which must still stop the site serving.
    """
    runner = await open_runner(data_dir)
    if scope == "anyio":
        with anyio.move_on_after(0.05):
            await upload_slowly(runner)
    elif scope == "asyncio":
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await upload_slowly(runner)
    else:
        task = asyncio.create_task(upload_slowly(runner))
        async with asyncio.timeout(5):
            while not runner.state.running:
                await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    state = runner.state
    assert (state.failed_stage, state.reason, state.site) == (Stage.UPLOADING, CANCELLED, Site.NOT_AVAILABLE)
    await runner.close()
    assert (await open_runner(data_dir)).state == state
    assert (await run_job(runner)).site == Site.AVAILABLE


async def test_upload_time_limit(data_dir: Path) -> None:
    """Check that a time limit inside the upload block names the operation that timed out."""
    runner = await open_runner(data_dir)
    with pytest.raises(StageError):
        async with runner.upload(), time_limit(0.05, "Reading the upload"):
            await asyncio.sleep(1)
    assert runner.state.reason == "Reading the upload timed out after 0.05 s"


async def fuseki_timeout() -> None:
    """Raise the timeout of a request to a Fuseki that doesn't answer in time."""

    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(slow), timeout=7.5) as client:
        await client.post("http://user:secret@fuseki:3030/$/datasets?dbName=gen-2")


async def other_timeout() -> None:
    """Raise a timeout from inside a time limit that isn't reached."""
    async with time_limit(10, "Building the store"):
        raise TimeoutError("lock wait")


async def time_limit_reached() -> None:
    """Exceed a time limit."""
    async with time_limit(0.01, "Fuseki HTTP request POST /$/datasets"):
        await asyncio.sleep(1)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (StageError("time limit"), "time limit"),
        (ValueError("bad value"), "internal error: ValueError: bad value"),
        (KeyError(), "internal error: KeyError"),
        (TimeoutError(), "timed out; the app's log shows where"),
        (other_timeout, "timed out: lock wait"),
        (time_limit_reached, "Fuseki HTTP request POST /$/datasets timed out after 0.01 s"),
        (fuseki_timeout, "HTTP request POST http://fuseki:3030/$/datasets timed out after 7.5 s (ReadTimeout)"),
        (httpx2.ConnectTimeout("no route"), "HTTP request timed out (ConnectTimeout)"),
    ],
)
async def test_stage_failure(data_dir: Path, error: Exception | Callable[[], Awaitable[None]], reason: str) -> None:
    """Check that a failing stage records the stage and reason, keeping the previous ready generation."""
    fail = False

    async def reasoning(context: JobContext) -> None:
        if not fail:
            return
        if isinstance(error, Exception):
            raise error
        await error()

    runner = await open_runner(data_dir, {Stage.REASONING: reasoning})
    await run_job(runner)
    fail = True
    state = await run_job(runner)
    assert (state.generation, state.failed_stage, state.reason) == (2, Stage.REASONING, reason)
    assert state.ready_generation == 1
    assert state.finished_at is not None
    assert state.site == Site.NOT_AVAILABLE
    assert (await open_runner(data_dir)).state == state


async def test_failure_not_stored(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a failure the database can't record still stops the site serving, and doesn't block the next job."""
    runner: JobRunner
    write = JobStore.write

    async def loading(context: JobContext) -> None:
        if context.generation == 1:

            def broken(state: JobState) -> None:
                monkeypatch.setattr(runner._store, "write", write.__get__(runner._store))
                raise sqlite3.OperationalError("disk I/O error")

            monkeypatch.setattr(runner._store, "write", broken)
            raise StageError("boom")

    runner = await open_runner(data_dir, {Stage.LOADING: loading})
    state = await run_job(runner)
    assert (state.failed_stage, state.reason, state.site) == (Stage.LOADING, "boom", Site.NOT_AVAILABLE)
    assert JobStore(data_dir / STATE_DB).read().running
    monkeypatch.setattr(browl.jobs, "STATE_TTL_S", 0)
    assert (await runner.current()) == state
    assert (await run_job(runner)).generation == 2
    assert (await runner.current()).site == Site.AVAILABLE


async def test_failure_record_interrupted(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a job that ends without recording its failure is still failed, so the site doesn't stay loading."""

    async def reasoning(context: JobContext) -> None:
        raise StageError("boom")

    async def interrupted(error: BaseException) -> None:
        raise RuntimeError("failure record interrupted")

    runner = await open_runner(data_dir, {Stage.REASONING: reasoning})
    monkeypatch.setattr(runner, "_fail", interrupted)
    await runner.start()
    with pytest.raises(RuntimeError, match="interrupted"):
        await runner.wait()
    state = runner.state
    assert (state.failed_stage, state.reason, state.site) == (Stage.REASONING, STOPPED_UNRECORDED, Site.NOT_AVAILABLE)
    await runner.close()
    assert (await open_runner(data_dir)).state == state


async def test_restart_fails_running_job(data_dir: Path) -> None:
    """Check that a job left running by a previous process is recorded as failed in its stage."""
    JobStore(data_dir / STATE_DB).write(
        JobState(generation=3, stage=Stage.LOADING, started_at=STARTED, ready_generation=2)
    )
    state = (await open_runner(data_dir)).state
    assert (state.failed_stage, state.reason, state.site) == (Stage.LOADING, INTERRUPTED_BY_RESTART, Site.NOT_AVAILABLE)
    assert (await open_runner(data_dir)).state == state


async def test_close_cancels_job(data_dir: Path) -> None:
    """Check that closing the runner cancels a running job's task and records the job as interrupted."""
    entered = asyncio.Event()

    async def forever(context: JobContext) -> None:
        entered.set()
        await asyncio.Event().wait()

    runner = await open_runner(data_dir, {Stage.PRE_CACHING: forever})
    await runner.start()
    async with asyncio.timeout(5):
        await entered.wait()
    task = runner._task
    await runner.close()
    assert task is not None and task.cancelled()
    state = (await open_runner(data_dir)).state
    assert (state.failed_stage, state.reason) == (Stage.PRE_CACHING, INTERRUPTED_BY_SHUTDOWN)


async def test_wait_cancelled(data_dir: Path) -> None:
    """Check that cancelling a caller waiting for a job doesn't cancel the job."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(context: JobContext) -> None:
        entered.set()
        await release.wait()

    runner = await open_runner(data_dir, {Stage.REASONING: blocked})
    await runner.start()
    waiter = asyncio.create_task(runner.wait())
    async with asyncio.timeout(5):
        await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await runner.wait()
    assert runner.state.site == Site.AVAILABLE


async def test_register_replaces(data_dir: Path) -> None:
    """Check that registering a function for a stage replaces the one registered before."""
    seen: list[str] = []

    async def first(context: JobContext) -> None:
        seen.append("first")

    async def second(context: JobContext) -> None:
        seen.append("second")

    runner = await open_runner(data_dir, {Stage.IMPORTS: first})
    runner.register(Stage.IMPORTS, second)
    await run_job(runner)
    assert seen == ["second"]


@pytest.mark.parametrize("stage", [Stage.UPLOADING, Stage.READY])
async def test_register_refuses_stage(data_dir: Path, stage: Stage) -> None:
    """Check that only the background stages take functions."""

    async def noop(context: JobContext) -> None:
        pass

    with pytest.raises(ValueError, match=str(stage)):
        (await open_runner(data_dir)).register(stage, noop)


async def test_close_right_after_start(data_dir: Path) -> None:
    """Check that closing the runner before its job's task has run a step still records the job as interrupted."""
    runner = await open_runner(data_dir)
    await runner.start()
    await runner.close()
    state = runner.state
    assert (state.failed_stage, state.reason) == (Stage.UPLOADING, INTERRUPTED_BY_SHUTDOWN)
    assert (await open_runner(data_dir)).state == state
    assert (await run_job(await open_runner(data_dir))).site == Site.AVAILABLE  # the job lock was released


@pytest.mark.parametrize("claimed", [True, False])
async def test_claim_cancelled(data_dir: Path, monkeypatch: pytest.MonkeyPatch, claimed: bool) -> None:
    """Check that a job claimed after its start was cancelled is failed as cancelled; either way the runner is freed."""
    runner = await open_runner(data_dir)
    entered, release = threading.Event(), threading.Event()
    claim = runner._store.claim

    def blocked(*args: Any) -> JobState:
        entered.set()
        release.wait(timeout=5)
        if not claimed:
            raise sqlite3.OperationalError("database is locked")
        return claim(*args)

    monkeypatch.setattr(runner._store, "claim", blocked)
    task = asyncio.create_task(runner.start())
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    async with asyncio.timeout(5):
        while runner._busy:
            await asyncio.sleep(0.001)
    state = runner.state
    if claimed:
        assert (state.generation, state.failed_stage, state.reason) == (1, Stage.UPLOADING, CANCELLED)
    else:
        assert state == JobState()
    assert (await open_runner(data_dir)).state == state
    monkeypatch.undo()
    assert (await run_job(runner)).generation == (2 if claimed else 1)


async def test_workers_share_state(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that runners on one `DATA_DIR`, as in several server workers, share the job state through the database.

    One worker's job is refused in the others, and a worker starting up while it runs leaves it running.
    """
    release = asyncio.Event()

    async def blocked(context: JobContext) -> None:
        await release.wait()

    first, second = await open_runner(data_dir, {Stage.REASONING: blocked}), await open_runner(data_dir)
    assert (await second.current()).site == Site.NOT_AVAILABLE
    await first.start()
    assert (await first.current()).site == Site.LOADING  # its own change shows at once
    assert (await second.current()).site == Site.NOT_AVAILABLE  # read less than STATE_TTL_S ago
    monkeypatch.setattr(browl.jobs, "STATE_TTL_S", 0)
    assert (await second.current()).site == Site.LOADING
    with pytest.raises(JobRunningError, match="another server worker"):
        await second.start()
    assert (await open_runner(data_dir)).state.running
    release.set()
    await first.wait()
    assert (await second.current()) == first.state
    assert (await run_job(second)).generation == 2
    assert (await first.current()).generation == 2


async def test_refresh_keeps_newer_change(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a database read that a change by the runner overtakes doesn't overwrite that change."""
    runner = await open_runner(data_dir)
    entered, release = threading.Event(), threading.Event()
    read = runner._store.read

    def blocked() -> JobState:
        state = read()
        entered.set()
        release.wait(timeout=5)
        return state

    monkeypatch.setattr(browl.jobs, "STATE_TTL_S", 0)
    monkeypatch.setattr(runner._store, "read", blocked)
    refresh = asyncio.create_task(runner.current())
    assert await asyncio.to_thread(entered.wait, 5)
    await run_job(runner)
    release.set()
    assert (await refresh).site == Site.AVAILABLE


async def test_refresh_reads_once(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that requests that find the state stale together read the database once between them."""
    runner = await open_runner(data_dir)
    reads: list[JobState] = []
    read = runner._store.read

    def counted() -> JobState:
        reads.append(read())
        return reads[-1]

    monkeypatch.setattr(runner._store, "read", counted)
    runner._read_at -= browl.jobs.STATE_TTL_S
    await asyncio.gather(*(runner.current() for _ in range(3)))
    assert reads == [JobState()]


async def test_long_reason_cut_short(data_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Check that a long failure reason is stored cut short, and logged in full."""
    message = "x" * (browl.jobs.MAX_REASON_CHARS * 2)

    async def reasoning(context: JobContext) -> None:
        raise StageError(message)

    state = await run_job(await open_runner(data_dir, {Stage.REASONING: reasoning}))
    assert state.reason == f"{message[: browl.jobs.MAX_REASON_CHARS]}… (cut short; the app's log has it all)"
    assert message in caplog.text


async def test_upload_progress(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a running upload's size so far is recorded once `UPLOAD_PROGRESS_S` has passed, if it has grown."""
    runner = await open_runner(data_dir)
    writes: list[JobState] = []
    write = runner._store.write
    monkeypatch.setattr(runner._store, "write", lambda state: (writes.append(state), write(state))[1])
    async with runner.upload(Reasoner.NONE, "onto.ttl", "Turtle") as context:
        context.upload_bytes = 5
        await runner.record_upload_progress(context)  # too soon after the claim
        assert runner.state.upload_bytes is None
        monkeypatch.setattr(browl.jobs, "UPLOAD_PROGRESS_S", 0)
        await runner.record_upload_progress(context)
        await runner.record_upload_progress(context)  # not grown
        stored = JobStore(data_dir / STATE_DB).read()
        assert (stored.stage, stored.finished_at, stored.upload_bytes) == (Stage.UPLOADING, None, 5)
        assert runner.state.upload_bytes == 5
        context.upload_bytes = 10
    await runner.wait()
    assert [state.upload_bytes for state in writes if state.stage == Stage.UPLOADING] == [5]
    assert JobStore(data_dir / STATE_DB).read().upload_bytes == 10


async def test_upload_progress_write_fails(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Check that a failed progress write is logged and skipped, leaving the upload to carry on."""
    monkeypatch.setattr(browl.jobs, "UPLOAD_PROGRESS_S", 0)
    runner = await open_runner(data_dir)
    write = runner._store.write

    def failing(state: JobState) -> None:
        if state.stage == Stage.UPLOADING:
            raise sqlite3.OperationalError("database is locked")
        write(state)

    monkeypatch.setattr(runner._store, "write", failing)
    async with runner.upload() as context:
        context.upload_bytes = 5
        await runner.record_upload_progress(context)
    await runner.wait()
    assert runner.state.stage == Stage.READY
    assert "Couldn't record load job 1's upload progress" in caplog.text


async def test_upload_progress_write_before_end(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a progress write still running when the upload is cancelled can't overwrite the job's end."""
    monkeypatch.setattr(browl.jobs, "UPLOAD_PROGRESS_S", 0)
    runner = await open_runner(data_dir)
    release = threading.Event()
    write = runner._store.write
    loop = asyncio.get_running_loop()

    def slow(state: JobState) -> None:
        if state.stage == Stage.UPLOADING and state.finished_at is None:
            loop.call_soon_threadsafe(scope.cancel)
            release.wait(timeout=5)
        write(state)

    monkeypatch.setattr(runner._store, "write", slow)
    # anyio's cancellation, as Starlette's, cancels every await in the scope, even the upload's wait for the write.
    with anyio.CancelScope() as scope:
        async with runner.upload() as context:
            context.upload_bytes = 5
            await runner.record_upload_progress(context)
    # The end can't be stored while the progress write runs. On a slow machine this wait could end before a wrongly
    # unordered end is stored, letting the test pass when it shouldn't, but never failing it when it should pass.
    done, _ = await asyncio.wait(runner._writes, timeout=0.1)
    assert not done
    release.set()
    await runner.close()
    stored = JobStore(data_dir / STATE_DB).read()
    assert (stored.failed_stage, stored.reason, stored.upload_bytes) == (Stage.UPLOADING, CANCELLED, 5)


async def test_on_ready_runs_under_lock(data_dir: Path) -> None:
    """Check that the after-ready work runs once the job is recorded ready, while the job lock is still held."""
    seen: list[tuple[int, Stage | None, bool]] = []
    runner: JobRunner

    async def on_ready(context: JobContext) -> None:
        other = JobLock(data_dir / JOB_LOCK)
        seen.append((context.generation, JobStore(data_dir / STATE_DB).read().stage, other.acquire()))
        other.release()

    runner = await JobRunner.open(make_settings(), on_ready=on_ready)
    state = await run_job(runner)
    assert seen == [(1, Stage.READY, False)]
    assert state.stage == Stage.READY
    assert (await run_job(runner)).ready_generation == 2  # the lock was released


async def test_on_ready_failure(data_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Check that failed after-ready work is logged, and the job stays ready."""

    async def on_ready(context: JobContext) -> None:
        raise OSError("can't delete")

    runner = await JobRunner.open(make_settings(), on_ready=on_ready)
    state = await run_job(runner)
    assert (state.stage, state.ready_generation, state.failed_stage) == (Stage.READY, 1, None)
    assert "Load job 1 is ready, but its after-ready work failed" in caplog.text
    assert "can't delete" in caplog.text
    assert (await run_job(runner)).ready_generation == 2


async def test_on_ready_cancelled(data_dir: Path) -> None:
    """Check that a shutdown during the after-ready work waits for it, and leaves the job ready."""
    started = asyncio.Event()
    release = asyncio.Event()
    finished: list[int] = []

    async def on_ready(context: JobContext) -> None:
        started.set()
        await asyncio.shield(release.wait())
        finished.append(context.generation)

    runner = await JobRunner.open(make_settings(), on_ready=on_ready)
    await runner.start()
    await started.wait()
    closing_task = asyncio.create_task(runner.close())
    await asyncio.sleep(0.01)
    assert not closing_task.done()
    release.set()
    await closing_task
    assert finished == [1]
    assert JobStore(data_dir / STATE_DB).read().stage == Stage.READY
    assert runner.state.stage == Stage.READY
