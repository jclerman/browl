"""Run a load job's tools (ROBOT, Jena's loader and parser) as subprocesses, each in its own process group.

A process group is stopped however the run ends: at its time limit, when a watched volume runs low on space, or when
the job is cancelled. ROBOT and the OFN lister run in the tool sandbox (`browl.sandbox`), when there is one; the
time limit and the free-space watch stay here.
"""

import asyncio
import codecs
import json
import logging
import os
import shutil
import signal
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, model_validator

from browl.jobs import StageError
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

SANDBOX_STOP_EXTRA_S = 5.0
"""How much longer than `TERM_GRACE_S` the sandbox gets to report a stopped run's status."""

NO_NETWORK_JAVA_OPTIONS = (
    "-Dhttp.proxyHost=127.0.0.1",
    "-Dhttp.proxyPort=1",
    "-Dhttps.proxyHost=127.0.0.1",
    "-Dhttps.proxyPort=1",
    "-Dhttp.nonProxyHosts=",
)
"""JVM options that send every HTTP(S) request through a closed port, so the OWLAPI fetches nothing itself: defense in
depth behind the catalog and the sandbox. They don't stop `file:` reads."""

ENVIRONMENT = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "JAVA_HOME", "JENA_HOME", "PYTHONPATH")
"""The only variables of the app's (or the sandbox's) environment a tool gets, besides coverage's (`COVERAGE_*`, so
tests measure the Python ones), so a `file:///proc/self/environ` import can't read the app's settings."""


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
    sandbox: Path | None = None,
) -> Completed:
    """Run a command in its own process group, until it ends, `timeout_s` passes, or a watched volume fills up.

    Args:
        command: The program and its arguments.
        on_line: Called with each line of its output (each of stderr's, if `on_stdout` is given). Each line is logged,
            and the last ones kept in `Completed.tail`.
        timeout_s: The time limit.
        watch: Directories whose volumes must keep `FREE_SPACE_MARGIN_BYTES` free while it runs.
        on_stdout: Called with each chunk of its standard output, which then isn't read as lines.
        env: Variables it gets besides the `ENVIRONMENT` ones of the app, or of the sandbox.
        name: What to call it in the log.
        sandbox: The tool sandbox's socket (`browl.sandbox`), to run it there rather than here.

    Raises:
        ValueError: Both `on_stdout` and `sandbox` were given.
        StageError: The sandbox couldn't be reached, or didn't run it.
    """
    tail: deque[str] = deque(maxlen=TAIL_LINES)

    def line(text: str) -> None:
        tail.append(text)
        if on_line is not None:
            on_line(text)
        logger.info("%s: %s", name, text)

    run: _Run
    if sandbox is None:
        logger.info("Running %s", " ".join(command))
        process = await start_process(command, env or {}, stderr_apart=on_stdout is not None)
        run = _Local(process, name, line, on_stdout)
    elif on_stdout is not None:
        raise ValueError("on_stdout can't be used with a sandbox")
    else:
        logger.info("Running %s in the sandbox", " ".join(command))
        run = await _Remote.connect(sandbox, {"command": list(command), "env": dict(env or {}), "name": name}, line)
    finishing = asyncio.ensure_future(run.finish())
    timed_out = out_of_space = False
    try:
        try:
            async with asyncio.timeout(timeout_s):
                out_of_space = not await _watch_space(finishing, watch)
        except TimeoutError:
            timed_out = True
    finally:
        finishing.cancel()
        await _despite_cancellation(asyncio.ensure_future(_end(finishing, run)))
    return Completed(run.status, timed_out, out_of_space, tuple(tail))


async def start_process(
    command: Sequence[str], env: Mapping[str, str], *, stderr_apart: bool = False
) -> asyncio.subprocess.Process:
    """Start a command in its own process group, with `tool_environment()` and `env`, reading its output.

    Args:
        command: The program and its arguments.
        env: Variables it gets besides the `ENVIRONMENT` ones.
        stderr_apart: Read its stderr apart from its standard output, rather than merged into it.
    """
    return await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE if stderr_apart else asyncio.subprocess.STDOUT,
        env={**tool_environment(), **env},
        start_new_session=True,  # its own process group, so it and anything it starts can be stopped together
    )


class Reply(BaseModel):
    """A message from the sandbox about a run: one of an output line, its exit status, or why it couldn't run.

    Attributes:
        line: A line of its output.
        status: Its exit status.
        error: Why it couldn't run.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    line: str | None = None
    status: int | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _one_field(self) -> Self:
        if [self.line, self.status, self.error].count(None) != 2:
            raise ValueError("a reply must have exactly one field")
        return self


class _Run(Protocol):
    """A process run here or in the sandbox, as `run_process` supervises it."""

    @property
    def status(self) -> int | None:
        """Its exit status, once known."""

    async def finish(self) -> None:
        """Pass on its output until it ends."""

    async def stop(self) -> None:
        """Stop whatever is left of its process group."""


class _Local:
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        name: str,
        on_line: Callable[[str], None],
        on_stdout: Callable[[bytes], None] | None,
    ) -> None:
        self.process, self.name, self.on_line, self.on_stdout = process, name, on_line, on_stdout

    @property
    def status(self) -> int | None:
        return self.process.returncode

    async def finish(self) -> None:
        stdout, stderr = self.process.stdout, self.process.stderr
        assert stdout is not None
        reads = [_read_lines(stdout, self.on_line) if self.on_stdout is None else _read_chunks(stdout, self.on_stdout)]
        if stderr is not None:
            reads.append(_read_lines(stderr, self.on_line))
        await asyncio.gather(*reads)
        await self.process.wait()

    async def stop(self) -> None:
        await stop_group(self.process, self.name)


class _Remote:
    """A run in the sandbox: its output lines, then its status, arrive as JSON lines (`browl.sandbox`)."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, name: str, on_line: Callable[[str], None]
    ) -> None:
        self.reader, self.writer, self.name, self.on_line = reader, writer, name, on_line
        self.status: int | None = None

    @classmethod
    async def connect(cls, socket: Path, request: Mapping[str, object], on_line: Callable[[str], None]) -> Self:
        """Send the sandbox a request to run a command.

        Raises:
            StageError: The sandbox couldn't be reached.
        """
        try:
            reader, writer = await asyncio.open_unix_connection(socket)
        except OSError as error:
            raise StageError(f"the tool sandbox isn't available at {socket}: {error}") from error
        writer.write(json.dumps(request).encode() + b"\n")
        return cls(reader, writer, str(request["name"]), on_line)

    async def finish(self) -> None:
        if not await self._read():
            raise StageError(f"the tool sandbox closed the connection while {self.name} ran")

    async def stop(self) -> None:
        """Ask the sandbox to stop the run, by closing the request's side of the connection, and wait for its status."""
        try:
            if self.status is None:
                with suppress(OSError, RuntimeError):  # the connection is already closed
                    self.writer.write_eof()
                try:
                    async with asyncio.timeout(TERM_GRACE_S + SANDBOX_STOP_EXTRA_S):
                        await self._read()
                except (TimeoutError, OSError, StageError) as error:
                    logger.warning(
                        "No exit status from the tool sandbox for %s: %s", self.name, str(error) or "timed out"
                    )
        finally:
            self.writer.close()
            with suppress(OSError):
                await self.writer.wait_closed()

    async def _read(self) -> bool:
        """Pass on output lines until the status arrives; return False if the connection ends first.

        Raises:
            StageError: The sandbox refused the run.
        """
        while message := await self.reader.readline():
            try:
                reply = Reply.model_validate_json(message)
            except ValueError as error:  # pydantic's ValidationError is a ValueError
                raise StageError(f"the tool sandbox sent a malformed reply while {self.name} ran") from error
            if reply.line is not None:
                self.on_line(reply.line)
            elif reply.status is not None:
                self.status = reply.status
                return True
            else:
                raise StageError(f"the tool sandbox couldn't run {self.name}: {reply.error}")
        return False


async def _end(finishing: asyncio.Future[None], run: _Run) -> None:
    await asyncio.wait({finishing})  # so it no longer reads the output when stop() does
    await run.stop()


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
    async for line in output_lines(stream):
        on_line(line)


async def output_lines(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    """Yield each line of output, read in chunks, so no line is too long to read; long lines are cut short."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")  # keeps a character split between reads whole
    rest = ""
    while chunk := await stream.read(READ_BYTES):
        lines = (rest + decoder.decode(chunk)).split("\n")
        rest = lines.pop()[:MAX_LINE_CHARS]
        for line in lines:
            yield line.rstrip()[:MAX_LINE_CHARS]
    if rest := rest + decoder.decode(b"", final=True):
        yield rest.rstrip()


async def stop_group(process: asyncio.subprocess.Process, name: str) -> None:
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
