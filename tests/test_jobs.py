"""Tests for the load job: its persistent state, schema migrations, and the runner."""

import asyncio
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import anyio
import httpx2
import pytest

import browl.jobs
from browl.jobs import (
    CANCELLED,
    INTERRUPTED_BY_RESTART,
    INTERRUPTED_BY_SHUTDOWN,
    STATE_DB,
    STOPPED_UNRECORDED,
    JobContext,
    JobRunner,
    JobRunningError,
    JobState,
    JobStore,
    Stage,
    StageError,
    time_limit,
)
from tests.test_app import make_settings

pytestmark = pytest.mark.anyio

STARTED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


async def open_runner(data_dir: Path, **stages: browl.jobs.StageFunction) -> JobRunner:
    """Open a runner on the database in `data_dir`, with stage functions named after their stages' members."""
    return await JobRunner.open(make_settings(DATA_DIR=str(data_dir)), {Stage[name]: f for name, f in stages.items()})


async def run_job(runner: JobRunner) -> JobState:
    """Start a job, wait for it to finish, and return the runner's state."""
    await runner.start()
    await runner.wait()
    return runner.state


def test_store_migrates_new_database(data_dir: Path) -> None:
    """Check that a new database is created at the latest schema version, with no job."""
    store = JobStore(data_dir / "sub" / STATE_DB)
    assert store.version == len(browl.jobs.MIGRATIONS) == 1
    assert store.read() == JobState()


def test_store_migrates_forward(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that reopening a database runs only the migrations past its version, keeping its data."""
    path = data_dir / STATE_DB
    state = JobState(generation=1, stage=Stage.READY, started_at=STARTED, finished_at=STARTED, ready_generation=1)
    JobStore(path).write(state)
    monkeypatch.setattr(browl.jobs, "MIGRATIONS", [*browl.jobs.MIGRATIONS, "CREATE TABLE admin (email TEXT);"])
    store = JobStore(path)
    assert store.version == 2
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
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
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
        (JobState(), "not available"),
        (JobState(generation=1, stage=Stage.LOADING, started_at=STARTED), "loading"),
        (JobState(generation=2, stage=Stage.IMPORTS, started_at=STARTED, ready_generation=1), "loading"),
        (
            JobState(generation=1, stage=Stage.READY, started_at=STARTED, finished_at=STARTED, ready_generation=1),
            "available",
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
            "not available",
        ),
    ],
    ids=["never-loaded", "first-load", "reload", "ready", "failed"],
)
def test_site(state: JobState, site: str) -> None:
    """Check what the site serves in each job state."""
    assert state.site == site


async def test_runs_stages_in_order(data_dir: Path) -> None:
    """Check that a job records each stage before running its function, in order, and ends ready."""
    seen: list[tuple[Stage, Stage | None, int]] = []
    runner: JobRunner

    def stage(name: Stage) -> browl.jobs.StageFunction:
        async def run(context: JobContext) -> None:
            seen.append((name, runner.state.stage, context.generation))

        return run

    runner = await open_runner(data_dir, IMPORTS=stage(Stage.IMPORTS), LOADING=stage(Stage.LOADING))
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
    assert state.started_at is not None and state.finished_at is not None and state.started_at <= state.finished_at
    assert state.site == "available"
    assert (await run_job(runner)).ready_generation == 2


async def test_state_persists(data_dir: Path) -> None:
    """Check that a reopened runner reads the state the previous one stored."""
    state = await run_job(await open_runner(data_dir))
    assert (await open_runner(data_dir)).state == state


async def test_refuses_second_job(data_dir: Path) -> None:
    """Check that a job can't start while one runs, and can once it has finished."""
    release = asyncio.Event()

    async def blocked(context: JobContext) -> None:
        await release.wait()

    runner = await open_runner(data_dir, REASONING=blocked)
    await runner.start()
    await asyncio.sleep(0)
    assert runner.state.site == "loading"
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

    def blocked(started_at: datetime) -> JobState:
        entered.set()
        release.wait(timeout=5)
        return claim(started_at)

    monkeypatch.setattr(runner._store, "claim", blocked)
    first = asyncio.create_task(runner.start())
    assert await asyncio.to_thread(entered.wait, 5)
    with pytest.raises(JobRunningError):
        await runner.start()
    release.set()
    assert (await first).generation == 1
    await runner.wait()
    assert runner.state.site == "available"


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
    assert (await run_job(runner)).site == "available"


async def test_upload_stage(data_dir: Path) -> None:
    """Check that the upload block runs in the uploading stage, and the other stages only after it."""
    runner = await open_runner(data_dir)
    async with runner.upload() as context:
        assert context.generation == 1
        assert runner.state.stage == Stage.UPLOADING
        assert runner.state.site == "loading"
    await runner.wait()
    assert runner.state.stage == Stage.READY


async def test_upload_failure(data_dir: Path) -> None:
    """Check that an error in the upload block fails the job in the uploading stage, and frees the runner."""
    runner = await open_runner(data_dir)
    with pytest.raises(OSError, match="disk full"):
        async with runner.upload():
            raise OSError("disk full")
    assert runner.state.failed_stage == Stage.UPLOADING
    assert runner.state.reason == "internal error: OSError: disk full"
    assert (await run_job(runner)).site == "available"


async def upload_slowly(runner: JobRunner) -> None:
    """Take a second over an upload."""
    async with runner.upload():
        await asyncio.sleep(1)


@pytest.mark.parametrize("scope", ["anyio", "asyncio"])
async def test_upload_cancelled(data_dir: Path, scope: str) -> None:
    """Check that an upload cancelled from outside, by a cancel scope or time limit, is recorded as cancelled.

    An anyio cancel scope also cancels the failure record, which must still stop the site serving.
    """
    runner = await open_runner(data_dir)
    if scope == "anyio":
        with anyio.move_on_after(0.05):
            await upload_slowly(runner)
    else:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await upload_slowly(runner)
    state = runner.state
    assert (state.failed_stage, state.reason, state.site) == (Stage.UPLOADING, CANCELLED, "not available")
    await runner.close()
    assert (await open_runner(data_dir)).state == state
    assert (await run_job(runner)).site == "available"


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

    runner = await open_runner(data_dir, REASONING=reasoning)
    await run_job(runner)
    fail = True
    state = await run_job(runner)
    assert (state.generation, state.failed_stage, state.reason) == (2, Stage.REASONING, reason)
    assert state.ready_generation == 1
    assert state.finished_at is not None
    assert state.site == "not available"
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

    runner = await open_runner(data_dir, LOADING=loading)
    state = await run_job(runner)
    assert (state.failed_stage, state.reason, state.site) == (Stage.LOADING, "boom", "not available")
    assert JobStore(data_dir / STATE_DB).read().running
    monkeypatch.setattr(browl.jobs, "STATE_TTL_S", 0)
    assert (await runner.current()) == state
    assert (await run_job(runner)).generation == 2
    assert (await runner.current()).site == "available"


async def test_failure_record_interrupted(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a job that ends without recording its failure is still failed, so the site doesn't stay loading."""

    async def reasoning(context: JobContext) -> None:
        raise StageError("boom")

    async def interrupted(error: BaseException) -> None:
        raise RuntimeError("failure record interrupted")

    runner = await open_runner(data_dir, REASONING=reasoning)
    monkeypatch.setattr(runner, "_fail", interrupted)
    await runner.start()
    with pytest.raises(RuntimeError, match="interrupted"):
        await runner.wait()
    state = runner.state
    assert (state.failed_stage, state.reason, state.site) == (Stage.REASONING, STOPPED_UNRECORDED, "not available")
    await runner.close()
    assert (await open_runner(data_dir)).state == state


async def test_restart_fails_running_job(data_dir: Path) -> None:
    """Check that a job left running by a previous process is recorded as failed in its stage."""
    JobStore(data_dir / STATE_DB).write(
        JobState(generation=3, stage=Stage.LOADING, started_at=STARTED, ready_generation=2)
    )
    state = (await open_runner(data_dir)).state
    assert (state.failed_stage, state.reason, state.site) == (Stage.LOADING, INTERRUPTED_BY_RESTART, "not available")
    assert (await open_runner(data_dir)).state == state


async def test_close_cancels_job(data_dir: Path) -> None:
    """Check that closing the runner cancels a running job and records it as interrupted."""

    async def forever(context: JobContext) -> None:
        await asyncio.Event().wait()

    runner = await open_runner(data_dir, PRE_CACHING=forever)
    await runner.start()
    while runner.state.stage != Stage.PRE_CACHING:
        await asyncio.sleep(0.001)
    await runner.close()
    state = (await open_runner(data_dir)).state
    assert (state.failed_stage, state.reason) == (Stage.PRE_CACHING, INTERRUPTED_BY_SHUTDOWN)


@pytest.mark.parametrize("stage", [Stage.UPLOADING, Stage.READY])
async def test_register_refuses_stage(data_dir: Path, stage: Stage) -> None:
    """Check that only the background stages take functions."""

    async def noop(context: JobContext) -> None:
        pass

    with pytest.raises(ValueError, match=str(stage)):
        (await open_runner(data_dir)).register(stage, noop)


@pytest.mark.parametrize("claimed", [True, False])
async def test_claim_cancelled(data_dir: Path, monkeypatch: pytest.MonkeyPatch, claimed: bool) -> None:
    """Check that a job claimed after its start was cancelled is failed as cancelled; either way the runner is freed."""
    runner = await open_runner(data_dir)
    entered, release = threading.Event(), threading.Event()
    claim = runner._store.claim

    def blocked(started_at: datetime) -> JobState:
        entered.set()
        release.wait(timeout=5)
        if not claimed:
            raise sqlite3.OperationalError("database is locked")
        return claim(started_at)

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

    first, second = await open_runner(data_dir, REASONING=blocked), await open_runner(data_dir)
    assert (await second.current()).site == "not available"
    await first.start()
    assert (await first.current()).site == "loading"  # its own change shows at once
    assert (await second.current()).site == "not available"  # read less than STATE_TTL_S ago
    monkeypatch.setattr(browl.jobs, "STATE_TTL_S", 0)
    assert (await second.current()).site == "loading"
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
    assert (await refresh).site == "available"


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
