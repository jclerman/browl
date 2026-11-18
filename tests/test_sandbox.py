"""Tests for the tool sandbox (P-30): its runner, and `run_process` running a command through it."""

import asyncio
import json
import logging
import os
import shutil
import signal
import socket as socket_
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import browl.ofn
import browl.process
import browl.robot
from browl.jobs import Reasoner, StageError
from browl.process import MAX_LINE_CHARS, run_process
from browl.robot import reasoning_stage
from browl.sandbox import Runner
from tests.conftest import find_robot_jar
from tests.helpers import StaticServer, make_settings, running_in_group, wait_for
from tests.test_robot import job

pytestmark = pytest.mark.anyio

STARTUP_S = 10.0
"""How long the runner, started as a subprocess, gets to start serving."""


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """Return a new directory with a path short enough for a Unix socket (macOS allows about 104 bytes)."""
    directory = Path(tempfile.mkdtemp(prefix="browl-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


@asynccontextmanager
async def runner(directory: Path) -> AsyncIterator[Path]:
    """Serve runs in this process, on this event loop, yielding the socket."""
    socket = directory / "socket"
    runner = Runner()
    server = await asyncio.start_unix_server(runner.handle, socket)
    try:
        yield socket
    finally:
        server.close()
        runner.kill_all()


async def sandboxed(socket: Path, script: str, **kwargs: object) -> tuple[browl.process.Completed, list[str]]:
    """Run a shell script through the sandbox, returning how it ended and its output lines."""
    lines: list[str] = []
    completed = await run_process(["sh", "-c", script], lines.append, sandbox=socket, **{"timeout_s": 10, **kwargs})
    return completed, lines


async def gone(pgid: int) -> bool:
    """Return whether a process group stops within a few seconds."""
    for _ in range(100):
        if not running_in_group(pgid):
            return True
        await asyncio.sleep(0.05)
    return False


async def test_output_and_status(short_dir: Path) -> None:
    """Check that output lines, stdout's and stderr's, and the exit status come through."""
    async with runner(short_dir) as socket:
        completed, lines = await sandboxed(socket, "echo one; echo two >&2; printf 'Bär'")
        assert completed.ok
        assert lines == ["one", "two", "Bär"]
        completed, _ = await sandboxed(socket, "exit 3")
        assert completed.status == 3
        assert not completed.ok


async def test_long_line_cut(short_dir: Path) -> None:
    """Check that a long output line is cut short."""
    async with runner(short_dir) as socket:
        _, lines = await sandboxed(socket, "printf '%05000d\\nend\\n'")
    assert lines == ["0" * MAX_LINE_CHARS, "end"]


@pytest.mark.parametrize(
    "script",
    [
        "echo $$ > {pid}; sleep 60 &",  # the shell exits; the sleep it started holds the output open
        "trap '' TERM; sleep 60 & echo $$ > {pid}; wait",  # the shell ignores SIGTERM, so gets SIGKILL
    ],
)
async def test_time_limit_stops_group(short_dir: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    """Check that a run past its time limit is stopped, its whole process group too."""
    monkeypatch.setattr(browl.process, "TERM_GRACE_S", 0.2)
    pid = short_dir / "pid"
    async with runner(short_dir) as socket:
        completed, _ = await sandboxed(socket, script.format(pid=pid), timeout_s=0.5)
    assert completed.timed_out
    assert completed.status is not None
    assert running_in_group(int(pid.read_text())) == []


async def test_cancel_stops_group(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a cancelled run's process group is stopped before `run_process` returns, even if cancelled again."""
    monkeypatch.setattr(browl.process, "TERM_GRACE_S", 1.0)
    pid = short_dir / "pid"
    async with runner(short_dir) as socket:
        task = asyncio.create_task(sandboxed(socket, f"trap '' TERM; sleep 60 & echo $$ > {pid}; wait"))
        pgid = await wait_for(pid, task)
        task.cancel()
        await asyncio.sleep(0.2)  # SIGTERM sent and ignored
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert running_in_group(pgid) == []


async def test_ended_run_stops_group(short_dir: Path) -> None:
    """Check that what's left of a run's process group, once its leader has exited, is stopped."""
    async with runner(short_dir) as socket:
        completed, lines = await sandboxed(socket, "sleep 60 >/dev/null 2>&1 & echo $$")
    assert completed.ok
    assert running_in_group(int(lines[0])) == []


async def test_hangup_stops_group(short_dir: Path) -> None:
    """Check that the runner stops a run's process group when its client disconnects."""
    pid = short_dir / "pid"
    async with runner(short_dir) as socket:
        _, writer = await asyncio.open_unix_connection(socket)
        request = {"command": ["sh", "-c", f"sleep 60 & echo $$ > {pid}; wait"]}
        writer.write(json.dumps(request).encode() + b"\n")
        pgid = await wait_for(pid)
        writer.close()
        assert await gone(pgid)


async def test_low_space_stops(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the free-space watch, which runs in the app, stops a run in the sandbox."""
    pid = short_dir / "pid"
    margin = browl.process.FREE_SPACE_MARGIN_BYTES
    monkeypatch.setattr(browl.process, "free_bytes", lambda path: margin - bool(pid.exists() and pid.read_text()))
    monkeypatch.setattr(browl.process, "SPACE_CHECK_S", 0.05)
    async with runner(short_dir) as socket:
        completed, _ = await sandboxed(socket, f"sleep 60 & echo $$ > {pid}; wait", watch=[short_dir])
    assert completed.out_of_space
    assert running_in_group(int(pid.read_text())) == []


async def test_unavailable(short_dir: Path) -> None:
    """Check that a run fails, naming the socket, when the sandbox isn't there."""
    socket = short_dir / "socket"
    with pytest.raises(StageError, match=f"^the tool sandbox isn't available at {socket}: "):
        await sandboxed(socket, "true")


async def test_on_stdout_refused(short_dir: Path) -> None:
    """Check that standard output can't be read as chunks through the sandbox."""
    with pytest.raises(ValueError, match="on_stdout"):
        await run_process(["true"], timeout_s=1, on_stdout=print, sandbox=short_dir / "socket")


@pytest.mark.parametrize(
    "request_",
    [
        b"not json\n",
        b'{"command": []}\n',
        b'{"command": "ls"}\n',
        b'{"command": ["ls"], "env": {"A": 1}}\n',
        b'{"command": ["ls"], "cwd": "/"}\n',
        b'{"command": ["/nonexistent/program"]}\n',
        b'{"command": ["ls"]}',  # no newline: the client closed before ending the request
    ],
)
async def test_bad_request(short_dir: Path, request_: bytes) -> None:
    """Check that a request the runner can't run gets an error, and the connection is closed."""
    async with runner(short_dir) as socket:
        reader, writer = await asyncio.open_unix_connection(socket)
        writer.write(request_)
        writer.write_eof()
        reply = await reader.read()
        writer.close()
    assert list(json.loads(reply)) == ["error"]


async def test_empty_connection(short_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Check that a connection with no request, as the health check makes, is closed without a word."""
    async with runner(short_dir) as socket:
        reader, writer = await asyncio.open_unix_connection(socket)
        writer.write_eof()
        assert await reader.read() == b""
        writer.close()
    assert caplog.records == []


async def test_refused_run_fails(short_dir: Path) -> None:
    """Check that a run the sandbox refuses fails, saying why."""
    async with runner(short_dir) as socket:
        with pytest.raises(StageError, match=r"^the tool sandbox couldn't run process: .*No such file"):
            await run_process(["/nonexistent/program"], timeout_s=10, sandbox=socket)


async def test_no_status(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a run whose status never comes ends anyway, with no status."""
    monkeypatch.setattr(browl.process, "TERM_GRACE_S", 0.1)
    monkeypatch.setattr(browl.process, "SANDBOX_STOP_EXTRA_S", 0.1)

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(60)

    socket = short_dir / "socket"
    server = await asyncio.start_unix_server(silent, socket)
    try:
        completed, _ = await sandboxed(socket, "true", timeout_s=0.2)
    finally:
        server.close()
    assert completed.timed_out
    assert completed.status is None


@pytest.mark.parametrize(
    "reply",
    [
        b"garbage\n",
        b'{"line": "cut',
        b"{}\n",
        b"[]\n",
        b'{"status": false}\n',
        b'{"status": null}\n',
        b'{"line": "a", "status": 0}\n',
    ],
)
async def test_malformed_reply(short_dir: Path, reply: bytes) -> None:
    """Check that a run fails, saying so, when the sandbox's reply isn't one it sends."""

    async def malformed(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(reply)
        writer.close()

    socket = short_dir / "socket"
    server = await asyncio.start_unix_server(malformed, socket)
    try:
        with pytest.raises(StageError, match=r"^the tool sandbox sent a malformed reply while process ran$"):
            await sandboxed(socket, "true")
    finally:
        server.close()


@pytest.fixture
def runner_process(short_dir: Path) -> Iterator[tuple[subprocess.Popen[bytes], Path]]:
    """Start the runner as the sandbox does, `python -m browl.sandbox`, with an environment of its own.

    It starts over a socket left by a runner that was killed, and is ready once it accepts a connection, as the
    sandbox's health check finds.
    """
    socket = short_dir / "socket"
    with socket_.socket(socket_.AF_UNIX) as stale:
        stale.bind(str(socket))
    env = {**os.environ, "LANG": "runner-lang", "BROWL_SECRET": "runner-secret"}
    process = subprocess.Popen([sys.executable, "-m", "browl.sandbox", str(socket)], env=env)
    try:
        deadline = time.monotonic() + STARTUP_S
        while True:
            try:
                with socket_.socket(socket_.AF_UNIX) as client:
                    client.connect(str(socket))
                break
            except OSError:
                assert process.poll() is None, "the runner exited"
                assert time.monotonic() < deadline, "the runner didn't start"
                time.sleep(0.02)
        yield process, socket
    finally:
        process.terminate()
        process.wait()


async def test_runner_environment(
    runner_process: tuple[subprocess.Popen[bytes], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that a tool gets the runner's `ENVIRONMENT` variables and those requested, but none of the app's."""
    _, socket = runner_process
    assert socket.stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("LANG", "app-lang")
    monkeypatch.setenv("BROWL_SECRET", "s3cret")
    lines: list[str] = []
    command = ["sh", "-c", 'echo "lang=$LANG secret=$BROWL_SECRET added=$ADDED"']
    completed = await run_process(command, lines.append, timeout_s=10, env={"ADDED": "yes"}, sandbox=socket)
    assert completed.ok
    assert lines == ["lang=runner-lang secret= added=yes"]


async def test_runner_shutdown(runner_process: tuple[subprocess.Popen[bytes], Path], short_dir: Path) -> None:
    """Check that the runner, on SIGTERM, kills the process groups it runs and exits, and that their runs fail."""
    process, socket = runner_process
    pid = short_dir / "pid"
    task = asyncio.create_task(sandboxed(socket, f"trap '' TERM; sleep 60 & echo $$ > {pid}; wait"))
    pgid = await wait_for(pid, task)
    process.send_signal(signal.SIGTERM)
    assert await asyncio.to_thread(process.wait, browl.process.TERM_GRACE_S / 2) == 0  # it didn't wait for SIGTERM
    with pytest.raises(StageError, match=r"^the tool sandbox closed the connection while process ran$"):
        await task
    assert await gone(pgid)


@pytest.mark.jvm
@pytest.mark.usefixtures("jvm")
async def test_robot_in_sandbox(
    site: StaticServer, short_dir: Path, data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Check that ROBOT runs through the sandbox: it fails on an import missing from the catalog, naming it."""
    path = data_dir / "uploads" / "1" / "ontology.ttl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        f"<http://example.org/o> a owl:Ontology ; owl:imports <{site.url}/missing> .\n"
    )
    async with runner(short_dir) as socket:
        settings = make_settings(ROBOT_JAR=str(find_robot_jar()), SANDBOX_SOCKET=str(socket))
        context = job(settings, Reasoner.ELK, path)
        with (
            caplog.at_level(logging.INFO, "browl.sandbox"),
            pytest.raises(StageError, match=f"Could not load imported ontology: <{site.url}/missing>"),
        ):
            await reasoning_stage(context)
    assert site.requested == []
    assert any(record.name == "browl.sandbox" and "Running ROBOT" in record.message for record in caplog.records)


@pytest.mark.parametrize("tool", ["robot", "ofn"])
async def test_tools_use_sandbox(tool: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """Check that ROBOT and the OFN lister run in the sandbox, watching the volume of the directory they write into."""
    calls: list[dict[str, object]] = []

    async def run_process(command: list[str], *args: object, **kwargs: object) -> browl.process.Completed:
        calls.append(kwargs)
        return browl.process.Completed(1)

    monkeypatch.setattr(browl.robot if tool == "robot" else browl.ofn, "run_process", run_process)
    settings = make_settings(SANDBOX_SOCKET="/sandbox/socket")
    with pytest.raises(StageError):
        if tool == "robot":
            await browl.robot.robot_to_file(
                [], tmp_path / "out" / "o.partial.ttl", tmp_path / "out" / "o.ttl", settings
            )
        else:
            await browl.ofn.convert_ofn(tmp_path / "o.ofn", tmp_path / "out" / "o.ttl", settings)
    [call] = calls
    assert call["sandbox"] == Path("/sandbox/socket")
    assert call["watch"] == [tmp_path / "out"]
