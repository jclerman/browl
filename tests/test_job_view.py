"""Tests for how the admin status shows a load job's stages."""

from datetime import UTC, datetime

import pytest

from browl.job_view import StepState, job_steps
from browl.jobs import JobState, Stage
from browl.jobtypes import Reasoner

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
