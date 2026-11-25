"""Tests for how the admin status shows a load job's stages."""

from datetime import UTC, datetime, timedelta

import pytest

from browl.job_view import Activity, StepState, job_steps, running_activity, usage_table_activity
from browl.jobs import JobState, Stage
from browl.jobtypes import Reasoner
from browl.precache.usage_table import Progress

NOW = datetime.now(UTC)


def test_reasoner_named() -> None:
    """Check that a job with a reasoner shows the reasoning stage as inferring axioms, with the reasoner's name."""
    step = job_steps(JobState(1, Stage.REASONING, NOW, reasoner=Reasoner.HERMIT))[2]
    assert (step.state, step.label, step.note) == (StepState.CURRENT, "Inferring Axioms", "HermiT")


def test_rebuild_running() -> None:
    """Check that a rebuild, running in pre-caching under its ready generation, shows that stage as current."""
    steps = job_steps(JobState(1, Stage.PRE_CACHING, NOW, ready_generation=1, reasoner=Reasoner.ELK))
    assert [step.state for step in steps] == [StepState.DONE] * 4 + [StepState.CURRENT, StepState.PENDING]


def test_not_started() -> None:
    """Check that a job that hasn't started has no stages to show."""
    with pytest.raises(ValueError, match="hasn't started"):
        job_steps(JobState())


def test_step_times() -> None:
    """Check that done and failed boxes show how long their stage took, the current one its time so far, others none."""
    seconds = {Stage.UPLOADING: 3.0, Stage.IMPORTS: 72.0, Stage.REASONING: 0.1}
    running = JobState(1, Stage.LOADING, NOW, stage_started_at=NOW - timedelta(seconds=5), stage_seconds=seconds)
    assert [step.time for step in job_steps(running, NOW)] == ["3 s", "1 min 12 s", None, "5 s", None, None]
    failed = JobState(1, Stage.REASONING, NOW, NOW, Stage.REASONING, "boom", Reasoner.ELK, stage_seconds=seconds)
    assert [step.time for step in job_steps(failed, NOW)][:4] == ["3 s", "1 min 12 s", "0 s", None]


def test_step_time_unknown() -> None:
    """Check that a state stored before stage times were recorded shows none."""
    assert {step.time for step in job_steps(JobState(1, Stage.LOADING, NOW), NOW)} == {None}


def test_running_activity() -> None:
    """Check that the running stage's activity has its label, time so far, detail and percentage, clamped to 0-100."""
    state = JobState(
        1, Stage.LOADING, NOW, stage_started_at=NOW - timedelta(seconds=4), detail="Added 5 triples", fraction=1.5
    )
    assert running_activity(state, job_steps(state, NOW)) == Activity(
        "Storing Triples", "4 s", "Added 5 triples", 100.0
    )
    ended = JobState(1, Stage.READY, NOW, NOW, ready_generation=1)
    assert running_activity(ended, job_steps(ended, NOW)) is None


@pytest.mark.parametrize(
    ("progress", "detail", "percent"),
    [
        (Progress(started=0.0), "Step 1 of 3: Counting triples", None),
        (Progress(started=0.0, triples=0), "Step 1 of 3: Counting triples", None),
        (Progress(started=0.0, triples=3, scanned=1), "Step 2 of 3: Scanned 1 of 3 triples (33%)", 33.3),
        (Progress(started=0.0, triples=3, scanned=3, scanned_at=5.0), "Step 3 of 3: Writing the table", None),
    ],
)
def test_usage_table_activity(progress: Progress, detail: str, percent: float | None) -> None:
    """Check the usage table build's activity in each of its phases."""
    assert usage_table_activity(progress, now=3.0) == Activity("Building the usage table", "3 s", detail, percent)
