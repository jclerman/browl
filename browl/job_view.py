"""How the admin status shows a load job (A-5, A-6): a box per stage, labelled by its state, and durations."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NamedTuple

from browl.jobs import JobState, Stage
from browl.jobtypes import Reasoner


class StepState(StrEnum):
    """A stage's state in a job, which its box's label, colors and icon show."""

    PENDING = "pending"
    CURRENT = "current"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class StageLabels(NamedTuple):
    """A stage's labels, one per state but skipped, and the tooltip that says what the stage does."""

    pending: str
    current: str
    done: str
    failed: str
    tooltip: str


STAGE_LABELS = {
    Stage.UPLOADING: StageLabels("Upload", "Uploading", "Uploaded", "Upload Failed", "Receiving the ontology file"),
    Stage.IMPORTS: StageLabels(
        "Fetch Imports",
        "Fetching Imports",
        "Imports Fetched",
        "Import Fetch Failed",
        "Fetching the ontologies it imports and listing their triples",
    ),
    Stage.REASONING: StageLabels(
        "Infer Axioms",
        "Inferring Axioms",
        "Axioms Inferred",
        "Inference Failed",
        "Adding the axioms the chosen reasoner infers",
    ),
    Stage.LOADING: StageLabels(
        "Store Triples",
        "Storing Triples",
        "Triples Stored",
        "Storing Failed",
        "Writing the triples into a new Fuseki store and indexing them",
    ),
    Stage.PRE_CACHING: StageLabels(
        "Pre-Cache",
        "Pre-Caching",
        "Pre-Cached",
        "Pre-Caching Failed",
        "Computing whole-ontology data and the search index",
    ),
    Stage.READY: StageLabels(
        "Publish", "Publishing", "Published", "Publishing Failed", "Switching the site over to this load"
    ),
}
"""Each stage's labels: the labels read as what's to do, being done, or done (the internal names, e.g. "loading",
would clash with "Load status")."""

NO_REASONER = StageLabels(
    "No Reasoner", "No Reasoner", "No Reasoner", "No Reasoner", "No reasoner was chosen, so no axioms are inferred"
)
"""The reasoning stage's labels when the job has no reasoner: it's skipped."""

REASONER_NAMES = {Reasoner.ELK: "ELK", Reasoner.HERMIT: "HermiT"}
"""The reasoners' names, shown under the reasoning stage's label."""


@dataclass(frozen=True)
class Step:
    """A stage's box in the status.

    Attributes:
        stage: The stage.
        state: Its state in the job.
        label: Its label for that state.
        tooltip: What the stage does.
        note: A second, smaller line, such as the reasoner's name; or None.
    """

    stage: Stage
    state: StepState
    label: str
    tooltip: str
    note: str | None = None


def job_steps(job: JobState) -> list[Step]:
    """Return a box per stage for a job that has started, in the order they run.

    A ready job's stages are all done, its last ("Publish") included. The reasoning stage of a job with no reasoner is
    skipped, unless it failed there.

    >>> from datetime import UTC
    >>> now = datetime.now(UTC)
    >>> [(step.state, step.label) for step in job_steps(JobState(1, Stage.LOADING, now))][2:4]
    [(<StepState.SKIPPED: 'skipped'>, 'No Reasoner'), (<StepState.CURRENT: 'current'>, 'Storing Triples')]
    """
    if job.stage is None:
        raise ValueError("the job hasn't started")
    current = list(Stage).index(job.stage)
    ready = job.stage is Stage.READY and not job.running and job.failed_stage is None
    steps = []
    for index, stage in enumerate(Stage):
        if index < current or ready:
            state = StepState.DONE
        elif index > current:
            state = StepState.PENDING
        elif job.failed_stage is not None:
            state = StepState.FAILED
        else:
            state = StepState.CURRENT
        labels, note = STAGE_LABELS[stage], None
        if stage is Stage.REASONING:
            note = REASONER_NAMES.get(job.reasoner)
            if note is None and state is not StepState.FAILED:
                labels, state = NO_REASONER, StepState.SKIPPED
        label = labels.pending if state is StepState.SKIPPED else getattr(labels, state)
        steps.append(Step(stage, state, label, labels.tooltip, note))
    return steps


def format_duration(seconds: float) -> str:
    """Return a duration, rounded down to whole seconds, in at most its two largest units.

    >>> [format_duration(s) for s in (0.4, 59, 72, 3600, 3725)]
    ['0 s', '59 s', '1 min 12 s', '1 h', '1 h 2 min']
    """
    whole = int(seconds)
    hours, minutes, secs = whole // 3600, whole // 60 % 60, whole % 60
    if hours:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    if minutes:
        return f"{minutes} min {secs} s" if secs else f"{minutes} min"
    return f"{secs} s"


def job_elapsed(job: JobState, now: datetime) -> str | None:
    """Return how long a job ran, or has run so far; None if it hasn't started."""
    if job.started_at is None:
        return None
    return format_duration(((job.finished_at or now) - job.started_at).total_seconds())
