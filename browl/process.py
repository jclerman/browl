"""Run a load job's tools (ROBOT, Jena's loader and parser) as subprocesses, each in its own process group.

A process group is stopped however the run ends: at its time limit, when the data volume runs low on space, or when
the job is cancelled.
"""

import asyncio
import codecs
import logging
import os
import shutil
import signal
from collections import deque
from collections.abc import Callable, Coroutine, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from browl.upload import FREE_SPACE_MARGIN_BYTES

logger = logging.getLogger(__name__)

TERM_GRACE_S = 5.0
"""How long a process gets to exit after SIGTERM before it's sent SIGKILL."""

SPACE_CHECK_S = 1.0
"""How often free space is checked while a process runs; it's stopped below `FREE_SPACE_MARGIN_BYTES`."""

READ_BYTES = 65536
"""How much of a process's output is read at a time."""

MAX_LINE_CHARS = 2000
"""The longest output line kept; a longer one is cut short."""

TAIL_LINES = 20
"""How many of a process's last output lines are kept, for a failure's reason."""

NO_NETWORK_JAVA_OPTIONS = (
    "-Dhttp.proxyHost=127.0.0.1",
    "-Dhttp.proxyPort=1",
    "-Dhttps.proxyHost=127.0.0.1",
    "-Dhttps.proxyPort=1",
    "-Dhttp.nonProxyHosts=",
)
"""JVM options that send every HTTP(S) request through a closed port, so the OWLAPI fetches nothing itself: defense in
depth behind the catalog, until P-30's sandbox. They don't stop `file:` reads."""

ENVIRONMENT = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "JAVA_HOME", "JENA_HOME", "PYTHONPATH")
"""The only variables of the app's environment a tool gets, besides coverage's (`COVERAGE_*`, so tests measure the
Python ones), so a `file:///proc/self/environ` import can't read the app's settings."""


def tool_environment() -> dict[str, str]:
    """Return the environment a tool gets: the app's `ENVIRONMENT` variables, and any `COVERAGE_*` ones."""
    return {key: value for key, value in os.environ.items() if key in ENVIRONMENT or key.startswith("COVERAGE_")}


@dataclass(frozen=True)
class Completed:
    """How a process ended.

    Attributes:
        status: Its exit status; negative for a signal; None if it was stopped before it could be read.
        timed_out: Whether it was stopped at its time limit.
        out_of_space: Whether it was stopped because a watched volume ran low on space.
        tail: Its last `TAIL_LINES` lines of output (of stderr, if its standard output was read as chunks).
    """

    status: int | None
    timed_out: bool = False
    out_of_space: bool = False
    tail: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether it ran to the end and succeeded."""
        return self.status == 0 and not self.timed_out and not self.out_of_space

    @property
    def output(self) -> str:
        """Its last lines, one string."""
        return "\n".join(self.tail)

    def limit_reason(self, activity: str, time_limit: str) -> str | None:
        """Return why it was stopped at a limit, worded alike for every tool (A-6); None if it wasn't.

        Args:
            activity: What it did, as a noun, e.g. "reasoning" or "riot".
            time_limit: Its time limit, e.g. "REASONER_TIMEOUT_S (3600 s)".
        """
        if self.timed_out:
            return f"time limit: {activity} took longer than {time_limit}"
        if self.out_of_space:
            return f"disk space: a volume fell below {FREE_SPACE_MARGIN_BYTES:,} bytes free while {activity} ran"
        return None


def java() -> str:
    """Return the Java executable: `JAVA_HOME`'s, else `java` on the PATH."""
    home = os.environ.get("JAVA_HOME")
    return str(Path(home) / "bin" / "java") if home else "java"


def free_bytes(path: Path) -> int:
    """Return the free space on the file system that holds the path."""
    return shutil.disk_usage(path).free


async def run_process(
    command: Sequence[str],
    on_line: Callable[[str], None] | None = None,
    *,
    timeout_s: float,
    watch: Sequence[Path] = (),
    on_stdout: Callable[[bytes], None] | None = None,
    env: Mapping[str, str] | None = None,
    name: str = "process",
) -> Completed:
    """Run a command in its own process group, until it ends, `timeout_s` passes, or a watched volume fills up.

    Args:
        command: The program and its arguments.
        on_line: Called with each line of its output (each of stderr's, if `on_stdout` is given). Each line is logged,
            and the last ones kept in `Completed.tail`.
        timeout_s: The time limit.
        watch: Directories whose volumes must keep `FREE_SPACE_MARGIN_BYTES` free while it runs.
        on_stdout: Called with each chunk of its standard output, which then isn't read as lines.
        env: Variables it gets besides the app's `ENVIRONMENT` ones.
        name: What to call it in the log.
    """
    logger.info("Running %s", " ".join(command))
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE if on_stdout else asyncio.subprocess.STDOUT,
        env={**tool_environment(), **(env or {})},
        start_new_session=True,  # its own process group, so it and anything it starts can be stopped together
    )

    tail: deque[str] = deque(maxlen=TAIL_LINES)

    def line(text: str) -> None:
        tail.append(text)
        if on_line is not None:
            on_line(text)
        logger.info("%s: %s", name, text)

    assert process.stdout is not None
    reads = [_read_lines(process.stdout, line) if on_stdout is None else _read_chunks(process.stdout, on_stdout)]
    if process.stderr is not None:
        reads.append(_read_lines(process.stderr, line))
    run = asyncio.ensure_future(_finish(process, reads))
    timed_out = out_of_space = False
    try:
        try:
            async with asyncio.timeout(timeout_s):
                out_of_space = not await _watch_space(run, watch)
        except TimeoutError:
            timed_out = True
    finally:
        run.cancel()
        await _despite_cancellation(asyncio.ensure_future(_stop(process, name)))
    return Completed(process.returncode, timed_out, out_of_space, tuple(tail))


async def _despite_cancellation(task: asyncio.Future[None]) -> None:
    """Wait for a task to end, even if cancelled meanwhile, any number of times; then raise the cancellation, if any.

    So a cancelled job still stops its process before its stage cleans up, e.g. deletes the store the process writes.
    """
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancelled = error
    task.result()
    if cancelled is not None:
        raise cancelled


async def _finish(process: asyncio.subprocess.Process, reads: list[Coroutine[None, None, None]]) -> None:
    await asyncio.gather(*reads)
    await process.wait()


async def _watch_space(run: asyncio.Future[None], directories: Sequence[Path]) -> bool:
    """Wait for the run; return False as soon as a directory's volume has less than `FREE_SPACE_MARGIN_BYTES` free."""
    while True:
        done, _ = await asyncio.wait({run}, timeout=SPACE_CHECK_S)
        if done:
            run.result()
            return True
        for directory in directories:
            if await asyncio.to_thread(free_bytes, directory) < FREE_SPACE_MARGIN_BYTES:
                return False


async def _read_chunks(stream: asyncio.StreamReader, on_chunk: Callable[[bytes], None]) -> None:
    while chunk := await stream.read(READ_BYTES):
        on_chunk(chunk)


async def _read_lines(stream: asyncio.StreamReader, on_line: Callable[[str], None]) -> None:
    """Pass on each line of output, read in chunks, so no line is too long to read; long lines are cut short."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")  # keeps a character split between reads whole
    rest = ""
    while chunk := await stream.read(READ_BYTES):
        lines = (rest + decoder.decode(chunk)).split("\n")
        rest = lines.pop()[:MAX_LINE_CHARS]
        for line in lines:
            on_line(line.rstrip()[:MAX_LINE_CHARS])
    if rest := rest + decoder.decode(b"", final=True):
        on_line(rest.rstrip())


async def _stop(process: asyncio.subprocess.Process, name: str) -> None:
    """Stop whatever is left in a process group: SIGTERM, then SIGKILL once its leader exits or `TERM_GRACE_S` passes.

    The group is signalled even after its leader exits, as a process it started could outlive it. A group's ID isn't
    reused while any process is left in it. macOS refuses to signal a group of zombies (EPERM), which is as good as
    gone.
    """
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return  # nothing left
    try:
        async with asyncio.timeout(TERM_GRACE_S):
            await process.wait()
    except TimeoutError:
        logger.warning("%s (pid %d) didn't exit on SIGTERM", name, process.pid)
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()
