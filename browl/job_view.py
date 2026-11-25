"""How the admin status shows a load job (A-5, A-6), and its progress (P-31).

A box per stage, labelled by its state, with how long it took; what the running stage is doing; and the work that runs
in the background once the job is ready.
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, NamedTuple

from browl.jobs import JobState, Stage
from browl.jobtypes import Reasoner

if TYPE_CHECKING:  # not imported, as pre-caching's stage imports this
    from browl.precache.usage_table import Progress


class StepState(StrEnum):
    """A stage's state in a job, which its box's label, colors and icon show."""

    PENDING = "pending"
    CURRENT = "current"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class StageLabels(NamedTuple):
    """A stage's labels, one per state but skipped, and the tooltip that says what the stage does.

    The tooltip is a command ("Receive the ontology file."), so it reads the same whatever the stage's state.
    """

    pending: str
    current: str
    done: str
    failed: str
    tooltip: str


STAGE_LABELS = {
    Stage.UPLOADING: StageLabels("Upload", "Uploading", "Uploaded", "Upload Failed", "Receive the ontology file."),
    Stage.IMPORTS: StageLabels(
        "Read Ontology",
        "Reading Ontology",
        "Ontology Read",
        "Ontology Read Failed",
        "Parse the ontology, count its triples, and fetch its imports.",
    ),
    Stage.REASONING: StageLabels(
        "Infer Axioms",
        "Inferring Axioms",
        "Axioms Inferred",
        "Inference Failed",
        "Add the axioms the chosen reasoner infers.",
    ),
    Stage.LOADING: StageLabels(
        "Store Triples",
        "Storing Triples",
        "Triples Stored",
        "Storing Failed",
        "Write the triples into a new Fuseki store and index them.",
    ),
    Stage.PRE_CACHING: StageLabels(
        "Pre-Cache",
        "Pre-Caching",
        "Pre-Cached",
        "Pre-Caching Failed",
        "Compute whole-ontology data and the search index.",
    ),
    Stage.READY: StageLabels(
        "Publish", "Publishing", "Published", "Publishing Failed", "Switch the site over to this load."
    ),
}
"""Each stage's labels: the labels read as what's to do, being done, or done (the internal names, e.g. "loading",
would clash with "Load status")."""

NO_REASONER = StageLabels(
    "No Reasoner", "No Reasoner", "No Reasoner", "No Reasoner", "No reasoner was chosen, so no axioms are inferred."
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
        time: How long it took, if it's done or failed, or has run so far, if it's current; None if not known.
    """

    stage: Stage
    state: StepState
    label: str
    tooltip: str
    note: str | None = None
    time: str | None = None


@dataclass(frozen=True)
class Activity:
    """What some work is doing, for the status's details panel: a running stage, or work in the background.

    Attributes:
        label: What the work is, e.g. "Storing Triples".
        elapsed: How long it has run so far; None if not known.
        detail: What it's doing now, e.g. "Checking for unsatisfiable classes"; None if it hasn't said.
        percent: How far it has got, from 0 to 100; None if it can't tell.
    """

    label: str
    elapsed: str | None = None
    detail: str | None = None
    percent: float | None = None


def job_steps(job: JobState, now: datetime | None = None) -> list[Step]:
    """Return a box per stage for a job that has started, in the order they run.

    A ready job's stages are all done, its last ("Publish") included. The reasoning stage of a job with no reasoner is
    skipped, unless it failed there. A done or failed stage shows how long it took, and the current one how long it
    has run so far, at `now` (the current time if not given).

    >>> from datetime import UTC
    >>> now = datetime.now(UTC)
    >>> [(step.state, step.label) for step in job_steps(JobState(1, Stage.LOADING, now))][2:4]
    [(<StepState.SKIPPED: 'skipped'>, 'No Reasoner'), (<StepState.CURRENT: 'current'>, 'Storing Triples')]
    """
    if job.stage is None:
        raise ValueError("the job hasn't started")
    now = datetime.now(UTC) if now is None else now
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
        steps.append(Step(stage, state, label, labels.tooltip, note, _step_time(job, stage, state, now)))
    return steps


def _step_time(job: JobState, stage: Stage, state: StepState, now: datetime) -> str | None:
    if state is StepState.CURRENT and job.stage_started_at is not None:
        return format_duration((now - job.stage_started_at).total_seconds())
    if state in (StepState.DONE, StepState.FAILED) and (seconds := job.stage_seconds.get(stage)) is not None:
        return format_duration(seconds)
    return None


def running_activity(job: JobState, steps: list[Step]) -> Activity | None:
    """Return what the running job's current stage is doing; None if the job isn't running."""
    if not job.running:
        return None
    for step in steps:
        if step.state is StepState.CURRENT:
            return Activity(step.label, step.time, job.detail, _percent(job.fraction))
    return None


def usage_table_activity(progress: "Progress", now: float | None = None) -> Activity:
    """Return what a usage table build (P-27c) is doing, from its progress, at `now` (`time.monotonic`, if not given).

    >>> from browl.precache.usage_table import Progress
    >>> activity = usage_table_activity(Progress(started=0.0, triples=200, scanned=50), now=72.0)
    >>> activity.elapsed, activity.detail, activity.percent
    ('1 min 12 s', 'Scanned 50 of 200 triples (25%)', 25.0)
    """
    now = time.monotonic() if now is None else now
    if progress.scanned_at is not None:
        detail, percent = "Writing the table", None
    elif progress.triples:
        percent = _percent(progress.scanned / progress.triples)
        detail = f"Scanned {count_of(progress.scanned, progress.triples, 'triples')}"
    else:
        detail, percent = "Counting triples", None
    return Activity("Building the usage table", format_duration(now - progress.started), detail, percent)


def count_of(done: int, total: int | None, noun: str) -> str:
    """Return a count of a total, with its percentage, or just the count if the total isn't known.

    >>> count_of(640_000, 1_167_135, "triples"), count_of(640_000, None, "triples")
    ('640,000 of 1,167,135 triples (55%)', '640,000 triples')
    """
    if not total:
        return f"{done:,} {noun}"
    return f"{done:,} of {total:,} {noun} ({min(100, round(100 * done / total))}%)"


def _percent(fraction: float | None) -> float | None:
    return None if fraction is None else round(100 * min(max(fraction, 0.0), 1.0), 1)


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
