"""The reasoning stage (DESIGN.md §2.4): ROBOT run as a subprocess, held to the reasoner time and heap limits (O-8).

ROBOT runs as `java -Xmx… -XX:+ExitOnOutOfMemoryError -jar robot.jar -vv …` in its own process group, which is killed
at the time limit or if the job is cancelled. A failure's reason says which limit was hit, or what else went wrong
(A-6), as S-2 found each to show (`failure_reason`).
"""

import asyncio
import logging
import os
import re
import signal
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from browl.jobs import JobContext, Reasoner, StageError
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

TERM_GRACE_S = 5.0
"""How long ROBOT gets to exit after SIGTERM before it's sent SIGKILL."""

TAIL_LINES = 20
"""How many of ROBOT's last output lines a failure's reason quotes."""

MAX_LINE_CHARS = 2000
"""The longest output line kept; a longer one is cut short."""

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
        line = line.rstrip()[:MAX_LINE_CHARS]
        self.tail.append(line)
        for signal_ in (OUT_OF_MEMORY, STACK_OVERFLOW, INCONSISTENT, PARSE_ERROR):
            if signal_ in line:
                self.signals.add(signal_)
        if match := UNSATISFIABLE.search(line):
            self.unsatisfiable_count = int(match[1])
        elif (match := UNSATISFIABLE_CLASS.search(line)) and len(self.unsatisfiable) < UNSATISFIABLE_SHOWN:
            self.unsatisfiable.append(match[1])


def failure_reason(status: int | None, output: RobotOutput, timed_out: bool, settings: Settings) -> str:
    """Return why a reasoning run failed, checking for each kind of failure in S-2's order (A-6).

    Args:
        status: ROBOT's exit status; negative for a signal.
        output: What was kept of its output.
        timed_out: Whether it was stopped at the time limit.
        settings: The settings, for the limits.
    """
    if timed_out:
        return f"time limit: reasoning took longer than REASONER_TIMEOUT_S ({settings.reasoner_timeout_s} s)"
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


def java() -> str:
    """Return the Java executable: `JAVA_HOME`'s, else `java` on the PATH."""
    home = os.environ.get("JAVA_HOME")
    return str(Path(home) / "bin" / "java") if home else "java"


def reason_args(reasoner: Reasoner, source: Path, output: Path, settings: Settings) -> list[str]:
    """Return ROBOT's arguments for a reasoning run: reason, materialize if configured, then remove shortcuts."""
    name = ROBOT_REASONERS[reasoner]
    args = ["reason", "--input", str(source), "--reasoner", name, "--axiom-generators", AXIOM_GENERATORS]
    if settings.materialize_properties:
        args += ["materialize", "--reasoner", name]
        for prop in settings.materialize_properties:
            args += ["--term", prop]
    return [*args, "query", "--update", str(SHORTCUTS_QUERY), "--output", str(output)]


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
    logger.info("Running %s", " ".join(command))
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # its own process group, so the JVM and anything it starts can be stopped together
    )
    output = RobotOutput()
    timed_out = False
    try:
        try:
            async with asyncio.timeout(settings.reasoner_timeout_s):
                await _read(process, output)
                await process.wait()
        except TimeoutError:
            timed_out = True
    finally:
        # Shielded: a cancelled job still stops ROBOT, even if cancelled again meanwhile.
        await asyncio.shield(_stop(process))
    if timed_out or process.returncode != 0:
        raise StageError(failure_reason(process.returncode, output, timed_out, settings))


async def _read(process: asyncio.subprocess.Process, output: RobotOutput) -> None:
    """Keep and log ROBOT's output lines, in chunks, so no line is too long to read."""
    assert process.stdout is not None
    partial = ""
    while chunk := await process.stdout.read(65536):
        lines = (partial + chunk.decode(errors="replace")).split("\n")
        partial = lines.pop()[: MAX_LINE_CHARS + 1]
        for line in lines:
            _keep(line, output)
    if partial:
        _keep(partial, output)


def _keep(line: str, output: RobotOutput) -> None:
    output.add(line)
    logger.info("ROBOT: %s", output.tail[-1])


async def _stop(process: asyncio.subprocess.Process) -> None:
    """Stop the process group if the process still runs: SIGTERM, then SIGKILL after `TERM_GRACE_S`."""
    if process.returncode is not None:
        return
    for sent in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sent)
        except ProcessLookupError:
            break
        try:
            async with asyncio.timeout(TERM_GRACE_S):
                await process.wait()
            return
        except TimeoutError:
            logger.warning("ROBOT (pid %d) didn't exit on %s", process.pid, sent.name)
    await process.wait()


async def reasoning_stage(context: JobContext) -> None:
    """Classify the upload with the chosen reasoner, setting the context's `reasoned` file; skip it if none was chosen.

    The output is written to a temporary file and renamed, so a run stopped mid-write leaves no partial ontology.

    Raises:
        StageError: Reasoning failed (`failure_reason`).
    """
    if context.reasoner is Reasoner.NONE or context.upload is None:
        return
    partial = context.upload.with_name(PARTIAL)
    try:
        await run_robot(reason_args(context.reasoner, context.upload, partial, context.settings), context.settings)
        reasoned = context.upload.with_name(REASONED)
        await asyncio.to_thread(partial.replace, reasoned)
    finally:
        await asyncio.to_thread(partial.unlink, missing_ok=True)
    context.reasoned = reasoned
