"""The reasoning stage (DESIGN.md §2.4): ROBOT run as a subprocess, held to the reasoner time and heap limits (O-8).

ROBOT runs as `java -Xmx… -XX:+ExitOnOutOfMemoryError -jar robot.jar -vv …` in its own process group, which is stopped
at the time limit, if the data volume runs low on space, or if the job is cancelled. A failure's reason says which limit
was hit, or what else went wrong, using the output signatures S-2 found (A-6, `failure_reason`).
"""

import asyncio
import logging
import re
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from browl.jobs import JobContext, Reasoner, StageError
from browl.process import java, run_process
from browl.settings import Settings
from browl.upload import FREE_SPACE_MARGIN_BYTES

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

TAIL_LINES = 20
"""How many of ROBOT's last output lines a failure's reason quotes."""

UNSATISFIABLE_SHOWN = 10
"""How many unsatisfiable classes a failure's reason names."""

OUT_OF_MEMORY = "Terminating due to java.lang.OutOfMemoryError"
OUT_OF_MEMORY_STATUS = 3  # the JVM's exit status with -XX:+ExitOnOutOfMemoryError; ROBOT itself exits 1 on any error
STACK_OVERFLOW = "StackOverflowError"
INCONSISTENT = "The ontology is inconsistent"
UNSATISFIABLE = re.compile(r"There are (\d+) unsatisfiable classes")
UNSATISFIABLE_CLASS = re.compile(r"unsatisfiable: (\S+)")
PARSE_ERROR = "INVALID ONTOLOGY FILE ERROR"


@dataclass
class RobotOutput:
    """What browl keeps of ROBOT's output: its last lines and the lines that tell failures apart.

    Attributes:
        tail: The last `TAIL_LINES` lines.
        unsatisfiable: The first `UNSATISFIABLE_SHOWN` unsatisfiable classes ROBOT listed.
        signals: The failure signatures seen (`OUT_OF_MEMORY`, `STACK_OVERFLOW`, `INCONSISTENT`, `PARSE_ERROR`).
        unsatisfiable_count: How many unsatisfiable classes ROBOT reported; None if it reported none.
    """

    tail: deque[str] = field(default_factory=lambda: deque(maxlen=TAIL_LINES))
    unsatisfiable: list[str] = field(default_factory=list)
    signals: set[str] = field(default_factory=set)
    unsatisfiable_count: int | None = None

    def add(self, line: str) -> None:
        """Keep a line of output."""
        self.tail.append(line)
        for signal_ in (OUT_OF_MEMORY, STACK_OVERFLOW, INCONSISTENT, PARSE_ERROR):
            if signal_ in line:
                self.signals.add(signal_)
        if match := UNSATISFIABLE.search(line):
            self.unsatisfiable_count = int(match[1])
        elif (match := UNSATISFIABLE_CLASS.search(line)) and len(self.unsatisfiable) < UNSATISFIABLE_SHOWN:
            self.unsatisfiable.append(match[1])


def failure_reason(
    status: int | None, output: RobotOutput, timed_out: bool, settings: Settings, out_of_space: bool = False
) -> str:
    """Return why a reasoning run failed, checking for each kind of failure in S-2's order (A-6).

    Args:
        status: ROBOT's exit status; negative for a signal.
        output: What was kept of its output.
        timed_out: Whether it was stopped at the time limit.
        settings: The settings, for the limits.
        out_of_space: Whether it was stopped because the data volume ran low on space.
    """
    if timed_out:
        return f"time limit: reasoning took longer than REASONER_TIMEOUT_S ({settings.reasoner_timeout_s} s)"
    if out_of_space:
        return f"disk space: the data volume fell below {FREE_SPACE_MARGIN_BYTES:,} bytes free while reasoning ran"
    if status == OUT_OF_MEMORY_STATUS and OUT_OF_MEMORY in output.signals:
        return f"memory limit: reasoning needed more than REASONER_MAX_HEAP ({settings.reasoner_max_heap})"
    if STACK_OVERFLOW in output.signals:
        return "stack overflow: the reasoner recursed too deeply"
    if INCONSISTENT in output.signals:
        return "inconsistent ontology"
    if output.unsatisfiable_count is not None:
        shown = ", ".join(output.unsatisfiable)
        more = output.unsatisfiable_count - len(output.unsatisfiable)
        return f"{output.unsatisfiable_count} unsatisfiable classes: {shown}{f' and {more} more' if more > 0 else ''}"
    tail = "\n".join(output.tail)
    if PARSE_ERROR in output.signals:
        return f"parse error: ROBOT couldn't read the ontology:\n{tail}"
    return f"reasoner failed (ROBOT exit status {status}):\n{tail}"


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


def convert_args(source: Path, output: Path, catalog: Path) -> list[str]:
    """Return ROBOT's arguments for converting an ontology to the syntax its output's suffix names."""
    return ["convert", "--catalog", str(catalog), "--input", str(source), "--output", str(output)]


async def run_robot(args: Sequence[str], settings: Settings) -> None:
    """Run ROBOT with the arguments, within the reasoner limits, in a process group stopped however this ends.

    Raises:
        StageError: ROBOT failed; the reason says how (`failure_reason`).
    """
    command = [
        java(),
        f"-Xmx{settings.reasoner_max_heap}",
        "-XX:+ExitOnOutOfMemoryError",
        "-jar",
        str(settings.robot_jar),
        "-vv",
        *args,
    ]
    output = RobotOutput()
    completed = await run_process(
        command, output.add, timeout_s=settings.reasoner_timeout_s, watch=[settings.data_dir], name="ROBOT"
    )
    if not completed.ok:
        raise StageError(
            failure_reason(completed.status, output, completed.timed_out, settings, completed.out_of_space)
        )


async def robot_to_file(args: Sequence[str], partial: Path, output: Path, settings: Settings) -> None:
    """Run ROBOT writing to `partial`, renamed to `output` once it succeeds, so a stopped run leaves no partial file.

    Raises:
        StageError: ROBOT failed.
    """
    try:
        await run_robot(args, settings)
        await asyncio.to_thread(partial.replace, output)
    except BaseException:
        await asyncio.to_thread(partial.unlink, missing_ok=True)
        raise


async def reasoning_stage(context: JobContext) -> None:
    """Classify the upload with the chosen reasoner, setting the context's `reasoned` file.

    Without a reasoner, an OWL Functional Syntax upload, which the TDB2 loader can't read, is converted to an RDF
    syntax instead, also set as `reasoned`; any other upload is left as it is. ROBOT resolves imports only through the
    catalog the imports stage wrote (`browl.imports`), never from their IRIs.

    Raises:
        StageError: Reasoning or the conversion failed (`failure_reason`).
    """
    if context.upload is None or (context.reasoner is Reasoner.NONE and context.upload.suffix != ".ofn"):
        return
    if context.catalog is None:
        raise StageError("internal error: the imports stage wrote no catalog")
    partial, reasoned = context.upload.with_name(PARTIAL), context.upload.with_name(REASONED)
    if context.reasoner is Reasoner.NONE:
        args = convert_args(context.upload, partial, context.catalog)
    else:
        args = reason_args(context.reasoner, context.upload, partial, context.settings, context.catalog)
    await robot_to_file(args, partial, reasoned, context.settings)
    context.reasoned = reasoned
