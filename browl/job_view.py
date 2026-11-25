"""How the admin status and the "loading" page show a load job (A-5, A-6), and its progress (P-31).

A box per stage, labelled by its state, with how long it took; what the running stage is doing; and the work that runs
in the background once the job is ready.
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple

from browl.jobs import JobState, Stage
from browl.jobtypes import Reasoner
from browl.progress_text import count_of, format_duration

if TYPE_CHECKING:  # not imported: pre-caching imports `browl.pages` (via `browl.loading`), which imports this
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

USAGE_TABLE_STEPS = 3
"""The usage table build's steps (`usage_table_activity`)."""

REASONER_NAMES = {Reasoner.ELK: "ELK", Reasoner.HERMIT: "HermiT"}
"""The reasoners' names, shown under the reasoning stage's label."""


@dataclass(frozen=True)
class Step:
    """A stage's box in the stepper.

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
    """What some work is doing, for a details panel: a running stage, or work in the background.

    Attributes:
        label: What the work is, e.g. "Storing Triples".
        elapsed: How long it has run so far; None if not known.
        detail: What it's doing now, e.g. "Checking for unsatisfiable classes"; None if it hasn't said.
        percent: How far it has got, from 0 to 100; None if it can't tell.
        iri: The IRI it's working on, e.g. the import being fetched, for admins only; None if none.
    """

    label: str
    elapsed: str | None = None
    detail: str | None = None
    percent: float | None = None
    iri: str | None = None


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


def running_activity(job: JobState, steps: list[Step], with_iri: bool = False) -> Activity | None:
    """Return what the running job's current stage is doing; None if the job isn't running.

    Args:
        job: The job state.
        steps: Its stages' boxes (`job_steps`).
        with_iri: Whether to include the IRI the stage is working on (`JobState.detail_iri`), for the admin status;
            the "loading" page is public, so it leaves the IRI out.
    """
    if not job.running:
        return None
    for step in steps:
        if step.state is StepState.CURRENT:
            iri = job.detail_iri if with_iri else None
            return Activity(step.label, step.time, job.detail, _percent(job.fraction), iri)
    return None


def usage_table_activity(progress: "Progress", now: float | None = None) -> Activity:
    """Return what a usage table build (P-27c) is doing, from its progress, at `now` (`time.monotonic`, if not given).

    Its steps are counting the triples, scanning them and writing the table.

    >>> from browl.precache.usage_table import Progress
    >>> activity = usage_table_activity(Progress(started=0.0, triples=200, scanned=50), now=72.0)
    >>> activity.elapsed, activity.detail, activity.percent
    ('1 min 12 s', 'Step 2 of 3: Scanned 50 of 200 triples (25%)', 25.0)
    """
    now = time.monotonic() if now is None else now
    percent = None
    if progress.scanned_at is not None:
        step, doing = 3, "Writing the table"
    elif progress.triples:
        percent = _percent(progress.scanned / progress.triples)
        step, doing = 2, f"Scanned {count_of(progress.scanned, progress.triples, 'triples')}"
    else:
        step, doing = 1, "Counting triples"
    detail = f"Step {step} of {USAGE_TABLE_STEPS}: {doing}"
    return Activity("Building the usage table", format_duration(now - progress.started), detail, percent)


def _percent(fraction: float | None) -> float | None:
    return None if fraction is None else round(100 * min(max(fraction, 0.0), 1.0), 1)


def progress_context(job: JobState, with_iri: bool = False) -> dict[str, Any]:
    """Return what a job's progress renders from, for the admin status and the "loading" page.

    That's the job, its stages' boxes (`steps`), how long it has run (`elapsed`) and what it's doing (`activity`).

    Args:
        job: The job state.
        with_iri: Whether the activity includes the IRI it's working on (`running_activity`); the "loading" page is
            public, so it leaves it out.
    """
    now = datetime.now(UTC)
    steps = [] if job.stage is None else job_steps(job, now)
    return {
        "job": job,
        "steps": steps,
        "elapsed": job_elapsed(job, now),
        "activity": running_activity(job, steps, with_iri),
    }


def job_elapsed(job: JobState, now: datetime) -> str | None:
    """Return how long a job ran, or has run so far; None if it hasn't started.

    That's its stages' times added up, the running one's so far, so a rebuild after a restart doesn't count the time
    the job sat ready before it, nor an interrupted job the app's downtime (`browl.jobs._recover` records no time for
    its stage). A state stored before stage times were recorded gives its start to its end.
    """
    if job.started_at is None:
        return None
    if job.stage_started_at is None:
        return format_duration(((job.finished_at or now) - job.started_at).total_seconds())
    # While a rebuild redoes a stage, its earlier time is left out for the new one so far.
    seconds = sum(time for stage, time in job.stage_seconds.items() if not (job.running and stage == job.stage))
    if job.running and job.stage_started_at is not None:
        seconds += max(0.0, (now - job.stage_started_at).total_seconds())
    return format_duration(seconds)
