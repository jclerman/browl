"""The reasoning stage (DESIGN.md §2.4): ROBOT run as a subprocess, held to the reasoner time and heap limits (O-8).

ROBOT runs as `java -Xmx… -XX:+ExitOnOutOfMemoryError <no-network options> -jar robot.jar -vv …` in its own process
group, in the sandbox if there is one (P-30), which is stopped at the time limit, if the job's volume runs low on
space, or if the job is cancelled. A failure's reason says which limit was hit, or what else went wrong, using the
output signatures S-2 found (A-6, `failure_reason`).
"""

import asyncio
import logging
import re
import signal
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from browl.jobs import JobContext
from browl.jobtypes import Reasoner, StageError
from browl.process import NO_NETWORK_JAVA_OPTIONS, Completed, java, run_process
from browl.settings import Settings

logger = logging.getLogger(__name__)

SHORTCUTS_QUERY = Path(__file__).parent / "sparql" / "remove-superclass-shortcuts.rq"
"""The SPARQL update that removes shortcut `rdfs:subClassOf` links after reasoning."""

AXIOM_GENERATORS = "SubClass EquivalentClass ClassAssertion"
"""The inferred axioms `robot reason` adds. Without EquivalentClass, it drops a subclass cycle's axioms as redundant and
the classes' equivalence is lost."""

ROBOT_REASONERS = {Reasoner.ELK: "ELK", Reasoner.HERMIT: "HermiT"}

REASONED = "reasoned.ttl"
"""The reasoned ontology's file name, beside the upload."""

PARTIAL = "reasoned.partial.ttl"
"""ROBOT's output while it's written, renamed to `REASONED` once complete; ROBOT picks the syntax by its suffix."""

UNSATISFIABLE_SHOWN = 10
"""How many unsatisfiable classes a failure's reason names."""

OUT_OF_MEMORY = "Terminating due to java.lang.OutOfMemoryError"
OUT_OF_MEMORY_STATUS = 3  # the JVM's exit status with -XX:+ExitOnOutOfMemoryError; ROBOT itself exits 1 on any error
KILLED_STATUS = -signal.SIGKILL
"""How a tool that the container's memory limit stopped ends: the kernel kills it without a word."""
STACK_OVERFLOW = "StackOverflowError"
INCONSISTENT = "The ontology is inconsistent"
UNSATISFIABLE = re.compile(r"There are (\d+) unsatisfiable classes")
UNSATISFIABLE_CLASS = re.compile(r"unsatisfiable: (\S+)")
PARSE_ERROR = "INVALID ONTOLOGY FILE ERROR"

LOADING = "Loading the ontology"
"""The reasoning stage's progress until ROBOT names a step."""

ROBOT_STEPS = {
    "Starting reasoning": "Starting the reasoner",
    "Checking for inconsistencies": "Checking for inconsistencies",
    "Checking for unsatisfiable classes": "Checking for unsatisfiable classes",
    "Checking for unsatisfiable object properties": "Checking for unsatisfiable object properties",
    "Precomputing class hierarchy": "Precomputing the class hierarchy",
    "Finding equivalencies": "Finding equivalent classes",
    "Using these axiom generators": "Adding the inferred axioms",
    "Removing redundant subclass axioms": "Removing redundant subclass axioms",
    "Materializing imported ontologies": "Gathering the imports",
    "Materializing...": "Reasoning over the properties",
    "Materialization complete": "Adding the inferred links",
}
"""ROBOT's `-vv` log messages that start a step (S-2, research on Uberon), by their start, and the step's detail."""

SUBCOMMAND_DETAILS = {"reason": None, "materialize": "Materializing", "query": "Removing superclass shortcuts"}
"""The ROBOT subcommands browl runs (`reason_args`), and how a step's detail names each but the first."""

_LOG_MESSAGE = re.compile(r"^\S+ \S+ +[A-Z]+ +\S+ - (.*)$")
_MATERIALIZING = re.compile(r"Materializing parents of class (\d+)/(\d+)")
_SUBCOMMAND_TOOK = re.compile(r"Subcommand Timing: (\S+) took")


@dataclass
class RobotOutput:
    """What browl keeps of ROBOT's output besides its last lines: the lines that tell failures apart.

    Attributes:
        unsatisfiable: The first `UNSATISFIABLE_SHOWN` unsatisfiable classes ROBOT listed.
        signals: The failure signatures seen (`OUT_OF_MEMORY`, `STACK_OVERFLOW`, `INCONSISTENT`, `PARSE_ERROR`).
        unsatisfiable_count: How many unsatisfiable classes ROBOT reported; None if it reported none.
    """

    unsatisfiable: list[str] = field(default_factory=list)
    signals: set[str] = field(default_factory=set)
    unsatisfiable_count: int | None = None

    def add(self, line: str) -> None:
        """Look at a line of output."""
        for signal_ in (OUT_OF_MEMORY, STACK_OVERFLOW, INCONSISTENT, PARSE_ERROR):
            if signal_ in line:
                self.signals.add(signal_)
        if match := UNSATISFIABLE.search(line):
            self.unsatisfiable_count = int(match[1])
        elif (match := UNSATISFIABLE_CLASS.search(line)) and len(self.unsatisfiable) < UNSATISFIABLE_SHOWN:
            self.unsatisfiable.append(match[1])


class RobotProgress:
    """Follow a ROBOT run's step from its `-vv` output, reporting each step's detail, and how far it got if known.

    ROBOT names its steps, but gives no percentage, except while materializing ("class 120/15000"). A subcommand
    that logs no step of its own, as `query` doesn't, is known to start when the one before it logs its time.

    Args:
        args: ROBOT's arguments, whose subcommands it follows (`SUBCOMMAND_DETAILS`).
        report: Called with each step's detail, and how far it has got, from 0 to 1, or None.
    """

    def __init__(self, args: Sequence[str], report: Callable[[str, float | None], None]) -> None:
        """Report that the ontology is loading."""
        self._subcommands = [arg for arg in args if arg in SUBCOMMAND_DETAILS]
        self._subcommand = 0
        self._report = report
        report(LOADING, None)

    def add(self, line: str) -> None:
        """Look at a line of output."""
        if (match := _LOG_MESSAGE.match(line)) is None:
            return
        message = match[1].strip()
        if match := _SUBCOMMAND_TOOK.match(message):
            if match[1] in self._subcommands:
                self._subcommand = self._subcommands.index(match[1]) + 1
                if self._subcommand < len(self._subcommands):
                    self._report(self._detail(None), None)
        elif match := _MATERIALIZING.match(message):
            done, total = int(match[1]), int(match[2])
            detail = self._detail(f"Adding the inferred links, class {done:,} of {total:,}")
            self._report(detail, done / total if total else None)
        else:
            for start, detail in ROBOT_STEPS.items():
                if message.startswith(start):
                    self._report(self._detail(detail), None)
                    return

    def _detail(self, step: str | None) -> str:
        """Return a step's detail, named for its subcommand, if any, but the first's; or the subcommand's alone."""
        index = min(self._subcommand, len(self._subcommands) - 1)
        name = SUBCOMMAND_DETAILS[self._subcommands[index]] if index > 0 else None
        if name is None:
            return step or ""
        return name if step is None else f"{name}: {step[0].lower()}{step[1:]}"


def failure_reason(completed: Completed, output: RobotOutput, settings: Settings) -> str:
    """Return why a reasoning run failed, checking for each kind of failure in S-2's order (A-6).

    Args:
        completed: How ROBOT ended.
        output: What was seen in its output.
        settings: The settings, for the limits.
    """
    if reason := completed.limit_reason("reasoning", f"REASONER_TIMEOUT_S ({settings.reasoner_timeout_s} s)"):
        return reason
    status = completed.status
    if status == OUT_OF_MEMORY_STATUS and OUT_OF_MEMORY in output.signals:
        return f"memory limit: reasoning needed more than REASONER_MAX_HEAP ({settings.reasoner_max_heap})"
    if status == KILLED_STATUS:
        return killed_reason("reasoning")
    if STACK_OVERFLOW in output.signals:
        return "stack overflow: the reasoner recursed too deeply"
    if INCONSISTENT in output.signals:
        return "inconsistent ontology"
    if output.unsatisfiable_count is not None:
        shown = ", ".join(output.unsatisfiable)
        more = output.unsatisfiable_count - len(output.unsatisfiable)
        return f"{output.unsatisfiable_count} unsatisfiable classes: {shown}{f' and {more} more' if more > 0 else ''}"
    if PARSE_ERROR in output.signals:
        return f"parse error: ROBOT couldn't read the ontology:\n{completed.output}"
    return f"reasoner failed (ROBOT exit status {status}):\n{completed.output}"


def killed_reason(activity: str) -> str:
    """Return why a tool was killed with SIGKILL, which it wasn't sent at a limit: most likely, out of memory.

    Args:
        activity: What it did, as a noun, e.g. "reasoning".
    """
    return f"killed: {activity} most likely ran out of memory: the sandbox's (SANDBOX_MEM_LIMIT) or the host's"


def reason_args(reasoner: Reasoner, source: Path, output: Path, settings: Settings, catalog: Path) -> list[str]:
    """Return ROBOT's arguments for a reasoning run: reason, materialize if configured, then remove shortcuts."""
    name = ROBOT_REASONERS[reasoner]
    args = ["reason", "--catalog", str(catalog), "--input", str(source), "--reasoner", name]
    args += ["--axiom-generators", AXIOM_GENERATORS]
    if settings.materialize_properties:
        args += ["materialize", "--reasoner", name]
        for prop in settings.materialize_properties:
            args += ["--term", prop]
    return [*args, "query", "--update", str(SHORTCUTS_QUERY), "--output", str(output)]


async def run_robot(
    args: Sequence[str],
    directory: Path,
    settings: Settings,
    report: Callable[[str, float | None], None] | None = None,
) -> None:
    """Run ROBOT with the arguments, within the reasoner limits, in a process group stopped however this ends.

    It runs in the sandbox, if there is one (`SANDBOX_SOCKET`), and is stopped if `directory`, which it writes into,
    runs low on space. If `report` is given, it's called with each step ROBOT logs (`RobotProgress`).

    Raises:
        StageError: ROBOT failed; the reason says how (`failure_reason`).
    """
    command = [
        java(),
        f"-Xmx{settings.reasoner_max_heap}",
        "-XX:+ExitOnOutOfMemoryError",
        *NO_NETWORK_JAVA_OPTIONS,
        "-jar",
        str(settings.robot_jar),
        "-vv",
        *args,
    ]
    output = RobotOutput()
    on_line = output.add
    if report is not None:
        progress = RobotProgress(args, report)

        def on_line(line: str) -> None:
            output.add(line)
            progress.add(line)

    completed = await run_process(
        command,
        on_line,
        timeout_s=settings.reasoner_timeout_s,
        watch=[directory],
        name="ROBOT",
        sandbox=settings.sandbox_socket,
    )
    if not completed.ok:
        raise StageError(failure_reason(completed, output, settings))


async def robot_to_file(
    args: Sequence[str],
    partial: Path,
    output: Path,
    settings: Settings,
    report: Callable[[str, float | None], None] | None = None,
) -> None:
    """Run ROBOT writing to `partial`, renamed to `output` once it succeeds, so a stopped run leaves no partial file.

    Raises:
        StageError: ROBOT failed.
    """
    try:
        await run_robot(args, output.parent, settings, report)
        await asyncio.to_thread(partial.replace, output)
    except BaseException:
        await asyncio.to_thread(partial.unlink, missing_ok=True)
        raise


async def reasoning_stage(context: JobContext) -> None:
    """Classify the ontology with the chosen reasoner, if any, setting the context's `reasoned` file.

    It reasons over the upload, or the Turtle an OFN upload was converted to (`JobContext.converted`). ROBOT resolves
    imports only through the catalog the imports stage wrote (`browl.imports`); one missing from it fails the run, and
    with `NO_NETWORK_JAVA_OPTIONS` ROBOT can't fetch it from the web.

    Raises:
        StageError: Reasoning failed (`failure_reason`).
    """
    source = context.converted or context.upload
    if source is None or context.reasoner is Reasoner.NONE:
        return
    if context.catalog is None:
        raise StageError("internal error: the imports stage wrote no catalog")
    partial, reasoned = source.with_name(PARTIAL), source.with_name(REASONED)
    args = reason_args(context.reasoner, source, partial, context.settings, context.catalog)
    await robot_to_file(args, partial, reasoned, context.settings, context.report)
    context.reasoned = reasoned
