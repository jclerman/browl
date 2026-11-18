"""The tool sandbox's runner (P-30): runs ROBOT and the OFN lister for the app, in a container with no network.

`python -m browl.sandbox SOCKET_PATH` serves runs on a Unix socket that only its user can use. A run is one connection:
the client sends one JSON line, `{"command": [...], "env": {...}, "name": "..."}`; the runner starts the command as
`browl.process.run_process` does, in its own process group with `tool_environment()` and `env`, and sends each output
line as `{"line": "..."}`. Once the process ends, or the client closes its side of the connection, the runner stops
what's left of the group and sends `{"status": <exit status>}`. A request it can't run gets `{"error": "..."}`.

The client (`run_process` with `sandbox`) keeps the time limit and the free-space watch.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from browl.process import READ_BYTES, output_lines, start_process, stop_group

logger = logging.getLogger(__name__)


class Request(BaseModel):
    """A client's request to run a command.

    Attributes:
        command: The program and its arguments.
        env: Variables it gets besides the runner's `ENVIRONMENT` ones.
        name: What to call it in the log.
    """

    model_config = ConfigDict(extra="forbid")

    command: list[str] = Field(min_length=1)
    env: dict[str, str] = {}
    name: str = "process"


class Runner:
    """Runs each connection's command, and stops every process group it started when it shuts down.

    Attributes:
        processes: The processes running.
    """

    def __init__(self) -> None:  # noqa: D107
        self.processes: set[asyncio.subprocess.Process] = set()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Serve one run."""
        try:
            await self._run(reader, writer)
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    def kill_all(self) -> None:
        """Kill every process group still running."""
        for process in self.processes:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)

    async def _run(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line.endswith(b"\n"):
                raise ValueError("the connection closed before the request ended")
            request = Request.model_validate_json(line)
            process = await start_process(request.command, request.env)
        except (ValueError, OSError) as error:  # pydantic's ValidationError is a ValueError
            logger.warning("Refused a request: %s", error)
            with suppress(OSError):
                await _send(writer, {"error": str(error)})
            return
        name = request.name
        logger.info("Running %s (pid %d): %s", name, process.pid, " ".join(request.command))
        self.processes.add(process)
        output = asyncio.ensure_future(_forward(process, writer))
        hangup = asyncio.ensure_future(_hangup(reader))
        try:
            await asyncio.wait({output, hangup}, return_when=asyncio.FIRST_COMPLETED)
            if hangup.done():
                logger.info("%s (pid %d): the client hung up; stopping it", name, process.pid)
        finally:
            await stop_group(process, name)
            self.processes.discard(process)
            for task in (output, hangup):
                task.cancel()
            await asyncio.gather(output, hangup, return_exceptions=True)
        logger.info("%s (pid %d) ended with status %s", name, process.pid, process.returncode)
        with suppress(OSError):
            await _send(writer, {"status": process.returncode})


async def _send(writer: asyncio.StreamWriter, message: dict[str, object]) -> None:
    writer.write(json.dumps(message).encode() + b"\n")
    await writer.drain()  # so a slow client holds the process back, rather than its output piling up here


async def _forward(process: asyncio.subprocess.Process, writer: asyncio.StreamWriter) -> None:
    assert process.stdout is not None
    async for line in output_lines(process.stdout):
        await _send(writer, {"line": line})
    await process.wait()


async def _hangup(reader: asyncio.StreamReader) -> None:
    """Return when the client closes its side of the connection, or the connection fails."""
    while await reader.read(READ_BYTES):
        pass


async def serve(socket: Path) -> None:
    """Serve runs on the socket until SIGTERM or SIGINT, then kill every process group still running."""
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_ in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signal_, stopping.set)
    runner = Runner()
    socket.unlink(missing_ok=True)  # left by a runner that was killed
    umask = os.umask(0o177)  # the socket is made 0600, for the app's user only, before anyone can connect
    try:
        server = await asyncio.start_unix_server(runner.handle, socket)
    finally:
        os.umask(umask)
    logger.info("Serving runs on %s", socket)
    try:
        await stopping.wait()
    finally:
        server.close()  # not wait_closed(), which waits for the runs
        runner.kill_all()
    logger.info("Stopped")


def main() -> None:
    """Serve runs on the socket the command line names."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    [socket] = sys.argv[1:]
    asyncio.run(serve(Path(socket)))


if __name__ == "__main__":
    main()
