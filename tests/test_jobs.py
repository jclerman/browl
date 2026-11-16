"""Tests for the load job: its persistent state, schema migrations, and the runner."""

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import browl.jobs
from browl.jobs import (
    INTERRUPTED_BY_RESTART,
    INTERRUPTED_BY_SHUTDOWN,
    STATE_DB,
    JobContext,
    JobRunner,
    JobRunningError,
    JobState,
    JobStore,
    Stage,
    StageError,
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


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (StageError("time limit"), "time limit"),
        (ValueError("bad value"), "internal error: ValueError: bad value"),
        (KeyError(), "internal error: KeyError"),
    ],
)
async def test_stage_failure(data_dir: Path, error: Exception, reason: str) -> None:
    """Check that a failing stage records the stage and reason, keeping the previous ready generation."""
    fail = False

    async def reasoning(context: JobContext) -> None:
        if fail:
            raise error

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
    """Check that a failure the database can't record still stops the site serving."""
    runner: JobRunner

    async def loading(context: JobContext) -> None:
        def broken(state: JobState) -> None:
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(runner._store, "write", broken)
        raise StageError("boom")

    runner = await open_runner(data_dir, LOADING=loading)
    state = await run_job(runner)
    assert (state.failed_stage, state.reason, state.site) == (Stage.LOADING, "boom", "not available")


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
