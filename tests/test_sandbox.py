"""Tests for the tool sandbox (P-30): its runner, and `run_process` running a command through it."""

import asyncio
import json
import logging
import os
import signal
import socket as socket_
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest

import browl.ofn
import browl.process
import browl.robot
from browl.jobtypes import Reasoner, StageError
from browl.process import run_process
from browl.robot import reasoning_stage
from browl.settings import Settings
from tests.conftest import find_robot_jar
from tests.helpers import StaticServer, make_settings, running_in_group, serve_runs, wait_for
from tests.test_robot import job

pytestmark = pytest.mark.anyio

STARTUP_S = 10.0
"""How long the runner, started as a subprocess, gets to start serving."""


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
    async with serve_runs(short_dir) as socket:
        completed, lines = await sandboxed(socket, "echo one; echo two >&2; printf 'Bär'")
        assert completed.ok
        assert lines == ["one", "two", "Bär"]
        completed, _ = await sandboxed(socket, "exit 3")
        assert completed.status == 3
        assert not completed.ok


@pytest.mark.parametrize(
    "script",
    [
        "echo $$ > {pid}; sleep 60 &",  # the shell exits; the sleep it started holds the output open
        "echo $$ > {pid}; trap '' TERM; sleep 60 & wait",  # the shell ignores SIGTERM, so gets SIGKILL
    ],
)
async def test_time_limit_stops_group(short_dir: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    """Check that a run past its time limit is stopped, its whole process group too."""
    monkeypatch.setattr(browl.process, "TERM_GRACE_S", 0.2)
    pid = short_dir / "pid"
    async with serve_runs(short_dir) as socket:
        for timeout_s in (0.2, 1, 5):  # longer if the shell was too slow to start, as on a loaded machine
            completed, _ = await sandboxed(socket, script.format(pid=pid), timeout_s=timeout_s)
            if pid.exists() and pid.read_text():
                break
    assert completed.timed_out
    assert completed.status is not None
    assert running_in_group(int(pid.read_text())) == []


async def test_ended_run_stops_group(short_dir: Path) -> None:
    """Check that what's left of a run's process group, once its leader has exited, is stopped."""
    async with serve_runs(short_dir) as socket:
        completed, lines = await sandboxed(socket, "sleep 60 >/dev/null 2>&1 & echo $$")
    assert completed.ok
    assert running_in_group(int(lines[0])) == []


async def test_hangup_stops_group(short_dir: Path) -> None:
    """Check that the runner stops a run's process group when its client disconnects."""
    pid = short_dir / "pid"
    async with serve_runs(short_dir) as socket:
        _, writer = await asyncio.open_unix_connection(socket)
        request = {"command": ["sh", "-c", f"sleep 60 & echo $$ > {pid}; wait"]}
        writer.write(json.dumps(request).encode() + b"\n")
        pgid = await wait_for(pid)
        writer.close()
        assert await gone(pgid)


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
    async with serve_runs(short_dir) as socket:
        reader, writer = await asyncio.open_unix_connection(socket)
        writer.write(request_)
        writer.write_eof()
        reply = await reader.read()
        writer.close()
    assert list(json.loads(reply)) == ["error"]


async def test_empty_connection(short_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Check that a connection with no request, as the health check makes, is closed without a word."""
    async with serve_runs(short_dir) as socket:
        reader, writer = await asyncio.open_unix_connection(socket)
        writer.write_eof()
        assert await reader.read() == b""
        writer.close()
    assert caplog.records == []


async def test_refused_run_fails(short_dir: Path) -> None:
    """Check that a run the sandbox refuses fails, saying why."""
    async with serve_runs(short_dir) as socket:
        with pytest.raises(StageError, match=r"^the tool sandbox couldn't run process: .*No such file"):
            await run_process(["/nonexistent/program"], timeout_s=10, sandbox=socket)


async def test_no_status(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a run whose status never comes ends anyway, with no status."""
    monkeypatch.setattr(browl.process, "TERM_GRACE_S", 0.1)
    monkeypatch.setattr(browl.process, "SANDBOX_STOP_EXTRA_S", 0.1)

    released = asyncio.Event()

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await released.wait()

    socket = short_dir / "socket"
    server = await asyncio.start_unix_server(silent, socket)
    try:
        completed, _ = await sandboxed(socket, "true", timeout_s=0.2)
    finally:
        released.set()
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
        await reader.readline()  # unread, it would make Linux reset the connection (`test_connection_reset`)
        writer.write(reply)
        writer.close()

    socket = short_dir / "socket"
    server = await asyncio.start_unix_server(malformed, socket)
    try:
        with pytest.raises(StageError, match=r"^the tool sandbox sent a malformed reply while process ran$"):
            await sandboxed(socket, "true")
    finally:
        server.close()


async def test_connection_reset(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a connection reset while reading replies fails the run as a closed connection."""

    async def reset(self: browl.process._Remote) -> bool:
        raise ConnectionResetError(104, "Connection reset by peer")

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    monkeypatch.setattr(browl.process._Remote, "_read", reset)
    socket = short_dir / "socket"
    server = await asyncio.start_unix_server(accept, socket)
    try:
        with pytest.raises(StageError, match=r"^the tool sandbox closed the connection while process ran$"):
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


async def test_robot_in_sandbox(
    java: None, site: StaticServer, short_dir: Path, data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Check that ROBOT runs through the sandbox: it fails on an import missing from the catalog, naming it."""
    path = data_dir / "uploads" / "1" / "ontology.ttl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        f"<http://example.org/o> a owl:Ontology ; owl:imports <{site.url}/missing> .\n"
    )
    async with serve_runs(short_dir) as socket:
        settings = make_settings(ROBOT_JAR=str(find_robot_jar()), SANDBOX_SOCKET=str(socket))
        context = job(settings, Reasoner.ELK, path)
        with (
            caplog.at_level(logging.INFO, "browl.sandbox"),
            pytest.raises(StageError, match=f"Could not load imported ontology: <{site.url}/missing>"),
        ):
            await reasoning_stage(context)
    assert site.requested == []
    assert any(record.name == "browl.sandbox" and "Running ROBOT" in record.message for record in caplog.records)


@pytest.mark.parametrize(
    ("module", "call"),
    [
        (
            browl.robot,
            lambda out, settings: browl.robot.robot_to_file([], out / "o.partial.ttl", out / "o.ttl", settings),
        ),
        (browl.ofn, lambda out, settings: browl.ofn.convert_ofn(out.parent / "o.ofn", out / "o.ttl", settings)),
    ],
)
async def test_tools_use_sandbox(
    module: object,
    call: Callable[[Path, Settings], Awaitable[object]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data_dir: Path,
) -> None:
    """Check that ROBOT and the OFN lister run in the sandbox, watching the volume of the directory they write into."""
    calls: list[dict[str, object]] = []

    async def run_process(command: list[str], *args: object, **kwargs: object) -> browl.process.Completed:
        calls.append(kwargs)
        return browl.process.Completed(1)

    monkeypatch.setattr(module, "run_process", run_process)
    with pytest.raises(StageError):
        await call(tmp_path / "out", make_settings(SANDBOX_SOCKET="/sandbox/socket"))
    [kwargs] = calls
    assert kwargs["sandbox"] == Path("/sandbox/socket")
    assert kwargs["watch"] == [tmp_path / "out"]
