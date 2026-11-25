"""Shared fixtures, the skip rules for tests that need Java, Fuseki or a browser, and the marks for them.

- `java`: Java and `robot.jar` (`ROBOT_JAR`, else `make robot-jar`'s, else the settings' default), e.g. to run ROBOT in
  a child process. On macOS, when `JAVA_HOME` is unset, it's set to Java 21 if that's installed. `jvm` also starts the
  JVM in the test process, for the OWLAPI.
- `fuseki_url`: the Fuseki at `BROWL_TEST_FUSEKI_URL`, or one started from `docker/fuseki` with Docker, built unless
  `BROWL_TEST_FUSEKI_IMAGE` names an image (as CI does): one per worker, on the host's own network on Linux, its logs
  saved in `test-logs/`. `fuseki_base` also needs Jena's tools (`JENA_HOME`, else
  `make jena`'s) and the Fuseki's base directory: a temporary one for a Fuseki the tests start, else
  `BROWL_TEST_FUSEKI_BASE`.
- `browser`, which pytest-playwright's `page` uses: Playwright's headless Chromium (`make browser`).

Tests whose needs are missing are skipped locally, and fail in CI (`CI` set). These fixtures give the tests that use
them a mark (`FIXTURE_MARKS`; see docs/CODING-STANDARDS.md).
"""

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2
import jpype
import pytest
from fastapi import FastAPI
from playwright.sync_api import Browser, BrowserType, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from browl import owlapi
from browl.settings import Settings
from tests.helpers import LiveServer, StaticServer

ROOT = Path(__file__).parent.parent
DATA = Path(__file__).parent / "data"
FUSEKI_IMAGE = "browl-fuseki-test"
FUSEKI_START_TIMEOUT_S = 60
FUSEKI_PORT_ATTEMPTS = 3
FUSEKI_LOGS = ROOT / "test-logs"  # the logs of the Fuseki containers the tests ran; CI keeps them if a test fails
ROBOT_JAR_CACHE = ROOT / ".cache" / "robot.jar"  # where `make robot-jar` puts it
JENA_CACHE = ROOT / ".cache" / "jena"  # where `make jena` puts it
MACOS_JAVA_HOME = "/usr/libexec/java_home"
FIXTURE_MARKS = {"java": "java", "fuseki_url": "fuseki", "browser": "browser"}
"""The mark each of these fixtures gives the tests that use it."""

pytest_plugins = ["pytester"]  # for `test_marks.py`, which runs pytest on tests of its own


STRAY_MARKS = pytest.StashKey[str]()
"""Why a test fails: it has a mark of `FIXTURE_MARKS` without the fixture that gives it."""


@pytest.hookimpl(tryfirst=True)  # before `-m` deselects by the marks
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark each test that uses a fixture of `FIXTURE_MARKS`; record why one with the mark but not the fixture fails.

    Such a test fails when it runs (`pytest_runtest_setup`), not at collection, so the run names it under xdist too.
    """
    for item in items:
        fixtures = getattr(item, "fixturenames", ())
        stray = []
        for fixture, mark in FIXTURE_MARKS.items():
            if fixture in fixtures:
                item.add_marker(mark)
            elif item.get_closest_marker(mark):
                stray.append(f"`{mark}` without the `{fixture}` fixture")
        if stray:
            item.stash[STRAY_MARKS] = f"marked {', '.join(stray)}; use the fixture, which gives the mark"


@pytest.hookimpl(tryfirst=True)  # before its fixtures are set up
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Fail a test marked without the fixture that gives the mark."""
    if reason := item.stash.get(STRAY_MARKS, None):
        pytest.fail(reason, pytrace=False)


@pytest.fixture
def anyio_backend() -> str:
    """Run the async tests on asyncio, as the app does."""
    return "asyncio"


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """Return a new directory with a path short enough for a Unix socket (macOS allows about 104 bytes)."""
    directory = Path(tempfile.mkdtemp(prefix="browl-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return an empty directory, set as `DATA_DIR` in the environment."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def site() -> Iterator[StaticServer]:
    """Yield a local HTTP server with fixed responses (`StaticServer`), recording the paths requested."""
    server = StaticServer()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def live_server() -> Iterator[Callable[[FastAPI], LiveServer]]:
    """Yield a function that serves an app on a free local port (`LiveServer`); each server stops after the test."""
    servers: list[LiveServer] = []

    def serve(app: FastAPI) -> LiveServer:
        servers.append(LiveServer(app))
        return servers[-1]

    yield serve
    errors = []
    for server in servers:
        try:
            server.close()
        except RuntimeError as error:
            errors.append(error)
    if errors:
        raise errors[0]


# pytest-playwright (0.10) keeps one Playwright for the whole session, but its sync API marks its event loop as running
# on the main thread until it stops, and async tests can't run meanwhile. These overrides scope it, and the fixtures
# that depend on it, to the test module, otherwise as the plugin's own: keep them in step with the plugin's.

BROWSER_MISSING = ("Executable doesn't exist", "Host system is missing dependencies")
"""What Playwright's launch errors say when the browser, or on Linux its system libraries, aren't installed."""


@pytest.fixture(scope="module")
def playwright(_pw_api_request_contexts: Any) -> Iterator[Playwright]:
    """Start Playwright for one test module."""
    started = sync_playwright().start()
    if _pw_api_request_contexts:
        _pw_api_request_contexts.instrument(started)
    try:
        yield started
    finally:
        started.stop()


@pytest.fixture(scope="module")
def browser_type(playwright: Playwright, browser_name: str) -> BrowserType:
    """Return the browser type to test with, Chromium by default (pytest-playwright's `--browser`)."""
    browser_type: BrowserType = getattr(playwright, browser_name)
    return browser_type


@pytest.fixture(scope="module")
def launch_browser(
    browser_type_launch_args: dict[str, Any], browser_type: BrowserType, connect_options: dict[str, Any] | None
) -> Callable[..., Browser]:
    """Return a function that launches the browser, or connects to one if `connect_options` says where."""

    def launch(**kwargs: Any) -> Browser:
        options = {**browser_type_launch_args, **kwargs}
        if not connect_options:
            return browser_type.launch(**options)
        headers = {"x-playwright-launch-options": json.dumps(options), **(connect_options.get("headers") or {})}
        return browser_type.connect(**{**connect_options, "headers": headers})

    return launch


@pytest.fixture(scope="module")
def browser_context_args(
    pytestconfig: pytest.Config,
    playwright: Playwright,
    device: str | None,
    base_url: str | None,
    _pw_artifacts_folder: tempfile.TemporaryDirectory[str],
) -> dict[str, Any]:
    """Return new browser contexts' options: the device's, the base URL, and where to record video."""
    args: dict[str, Any] = dict(playwright.devices[device]) if device else {}
    if base_url:
        args["base_url"] = base_url
    if pytestconfig.getoption("--video") in ("on", "retain-on-failure"):
        args["record_video_dir"] = _pw_artifacts_folder.name
    return args


@pytest.fixture(scope="module")
def browser(launch_browser: Callable[[], Browser]) -> Iterator[Browser]:
    """Launch the browser, or skip the test if it, or its system libraries, aren't installed."""
    try:
        launched = launch_browser()
    except PlaywrightError as error:
        if not any(text in error.message for text in BROWSER_MISSING):
            raise
        skip_or_fail(f"no browser for Playwright (run `make browser`; see CONTRIBUTING.md): {error.message[:200]}")
    try:
        yield launched
    finally:
        launched.close()


def find_robot_jar() -> Path:
    """Return the `robot.jar` the tests use: `ROBOT_JAR`, else `make robot-jar`'s, else the settings' default."""
    if path := os.environ.get("ROBOT_JAR"):
        return Path(path)
    if ROBOT_JAR_CACHE.is_file():
        return ROBOT_JAR_CACHE
    return Path(Settings.model_fields["robot_jar"].default)


def find_jena_home() -> Path:
    """Return the Jena tools the tests use: `JENA_HOME`, else `make jena`'s, else the settings' default."""
    if path := os.environ.get("JENA_HOME"):
        return Path(path)
    if (JENA_CACHE / "bin").is_dir():
        return JENA_CACHE
    return Path(Settings.model_fields["jena_home"].default)


@pytest.fixture(scope="session")
def jena_home() -> Path:
    """Return Jena's tools, or skip the test if they're missing (run `make jena`)."""
    home = find_jena_home()
    if not (home / "bin" / "tdb2.tdbloader").is_file():
        skip_or_fail(f"no Jena tools at {home} (run `make jena`, or set JENA_HOME)")
    return home


def default_java_home() -> None:
    """On macOS, set `JAVA_HOME` to Java 21 when it's unset and Java 21 is installed; JPype reads it at JVM start.

    Without it, JPype can pick an older Java on a Mac with several.
    """
    if os.environ.get("JAVA_HOME") or not Path(MACOS_JAVA_HOME).is_file():
        return
    found = subprocess.run([MACOS_JAVA_HOME, "-v", "21"], check=False, capture_output=True, text=True)
    if found.returncode == 0 and found.stdout.strip():
        os.environ["JAVA_HOME"] = found.stdout.strip()


def missing_jvm() -> str | None:
    """Set `JAVA_HOME` (`default_java_home`), then return why the JVM can't start, or None if it can."""
    default_java_home()
    if jpype.isJVMStarted():
        return None
    if not find_robot_jar().is_file():
        return f"no robot.jar at {find_robot_jar()} (run `make robot-jar`, or set ROBOT_JAR)"
    try:
        jvm_path = jpype.getDefaultJVMPath()
    except jpype.JVMNotFoundException as error:
        return f"no Java found (set JAVA_HOME): {error}"
    return None if Path(jvm_path).exists() else f"no JVM library at {jvm_path} (set JAVA_HOME)"


def skip_or_fail(reason: str) -> None:
    """Skip the test locally, but fail it in CI, which must run every test."""
    if os.environ.get("CI"):
        pytest.fail(f"CI must run every test: {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="session")
def robot_jar() -> Path:
    """Return the `robot.jar` the tests use (see `find_robot_jar`)."""
    return find_robot_jar()


@pytest.fixture(scope="session")
def java() -> None:
    """Skip the test if Java or `robot.jar` is missing; on macOS, set `JAVA_HOME`, which child processes inherit."""
    reason = missing_jvm()
    if reason:
        skip_or_fail(reason)


@pytest.fixture(scope="session")
def jvm(java: None) -> None:
    """Start the JVM in the test process, on the main thread as it must be."""
    owlapi.start_jvm(find_robot_jar(), "1g")


def _docker(*args: str) -> str:
    """Run a Docker command and return its output; raise with its stderr if it fails."""
    command = ["docker", *args]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"`{shlex.join(command)}` failed with exit status {result.returncode}:\n{result.stderr}")
    return result.stdout.strip()


class FusekiExitedError(RuntimeError):
    """A Fuseki container the tests started stopped before it answered."""

    def __init__(self, container: str) -> None:
        """Record the container's logs, which say why."""
        self.logs = _container_logs(container)
        super().__init__(f"the Fuseki container stopped before it answered; its logs:\n{self.logs}")


def _wait_for_fuseki(url: str, container: str | None = None) -> None:
    """Wait until the Fuseki at a URL answers; raise `FusekiExitedError` if its container, if given, stops first."""
    deadline = time.monotonic() + FUSEKI_START_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if httpx2.get(f"{url}/$/ping", timeout=2).is_success:
                return
        except httpx2.HTTPError:
            pass
        if container is not None and _docker("inspect", "--format", "{{.State.Running}}", container) != "true":
            raise FusekiExitedError(container)
        time.sleep(0.5)
    raise TimeoutError(f"Fuseki at {url} didn't answer within {FUSEKI_START_TIMEOUT_S} s")


def _container_logs(container: str) -> str:
    """Return a container's output, stdout and stderr together, or why it couldn't be read."""
    result = subprocess.run(
        ["docker", "logs", container], check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    return result.stdout if result.returncode == 0 else f"(`docker logs` failed: {result.stdout.strip()})"


def _free_port() -> int:
    """Return a local port that's free now; something else may take it before it's used."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _start_fuseki(image: str, base: Path, host_network: bool) -> tuple[str, str]:
    """Start a Fuseki container from an image, with `base` as its base directory; return the container and its URL.

    On the host's own network (`host_network`), the container adds no network interface, which would make Chromium
    fail the requests under way in other workers' browser tests (`net::ERR_NETWORK_CHANGED`). Its port is one that was
    free a moment ago; if something takes it first, Fuseki exits, and another is tried, up to `FUSEKI_PORT_ATTEMPTS`.
    Otherwise Docker publishes the container's port 3030 on a free local port.
    """
    run = [
        "run",
        "--detach",
        "--pull",
        "never",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--volume",
        f"{base}:/fuseki",
    ]
    attempt = 1
    while True:
        port = _free_port() if host_network else None
        if port is None:
            network = ["--publish", "127.0.0.1::3030"]
        else:
            network = ["--network", "host", "--env", f"FUSEKI_PORT={port}", "--env", "FUSEKI_HOST=127.0.0.1"]
        container = _docker(*run, *network, image)
        try:
            address = (
                f"127.0.0.1:{port}" if port is not None else _docker("port", container, "3030/tcp").splitlines()[0]
            )
            url = f"http://{address}"
            _wait_for_fuseki(url, container)
        except FusekiExitedError as error:
            _remove_container(container)
            if not host_network or attempt == FUSEKI_PORT_ATTEMPTS or "Address already in use" not in error.logs:
                raise
            attempt += 1
            continue
        except BaseException:
            _remove_container(container)
            raise
        return container, url


def _remove_container(container: str) -> None:
    subprocess.run(["docker", "rm", "--force", container], check=False, capture_output=True)


def fuseki_docker() -> tuple[str, bool]:
    """Return the Fuseki image for the tests, and whether its containers can run on the host's own network.

    The image is `BROWL_TEST_FUSEKI_IMAGE`, else one built from `docker/fuseki`. The host's network is the tests' own
    where Docker runs containers on this machine: on Linux, but not in Docker Desktop's VM.
    """
    info = None
    if shutil.which("docker") is not None:
        info = subprocess.run(
            ["docker", "info", "--format", "{{.OperatingSystem}}"], check=False, capture_output=True, text=True
        )
    if info is None or info.returncode:
        skip_or_fail("no Fuseki: set BROWL_TEST_FUSEKI_URL, or make Docker available")
    assert info is not None
    image = os.environ.get("BROWL_TEST_FUSEKI_IMAGE")
    if not image:
        image = FUSEKI_IMAGE
        _docker("build", "--quiet", "--tag", image, str(ROOT / "docker" / "fuseki"))
    return image, sys.platform == "linux" and "Docker Desktop" not in info.stdout


@pytest.fixture(scope="session")
def fuseki_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path | None]]:
    """Yield a Fuseki's URL and its base directory as the tests see it, None if unknown.

    That's `BROWL_TEST_FUSEKI_URL` and `BROWL_TEST_FUSEKI_BASE`, or a Fuseki started in Docker (`fuseki_docker`,
    `_start_fuseki`), whose base is a temporary directory, and which runs as the tests' user, so it can open and delete
    the stores the tests build there. Its logs are saved in `FUSEKI_LOGS` when it's removed, one file per worker.
    """
    if url := os.environ.get("BROWL_TEST_FUSEKI_URL", "").rstrip("/"):
        _wait_for_fuseki(url)
        base = os.environ.get("BROWL_TEST_FUSEKI_BASE")
        yield url, Path(base) if base else None
        return
    image, host_network = fuseki_docker()
    base_dir = tmp_path_factory.mktemp("fuseki")
    container, url = _start_fuseki(image, base_dir, host_network)
    try:
        yield url, base_dir
    finally:
        FUSEKI_LOGS.mkdir(parents=True, exist_ok=True)
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        (FUSEKI_LOGS / f"fuseki-{worker}.log").write_text(_container_logs(container))
        _remove_container(container)


@pytest.fixture(scope="session")
def fuseki_url(fuseki_server: tuple[str, Path | None]) -> str:
    """Return the URL of a Fuseki for the tests (`fuseki_server`)."""
    return fuseki_server[0]


@pytest.fixture(scope="session")
def fuseki_base(fuseki_server: tuple[str, Path | None], jena_home: Path) -> Path:
    """Return the base directory of the tests' Fuseki, where tests build stores; skip the test if it's unknown."""
    base = fuseki_server[1]
    if base is None:
        skip_or_fail("BROWL_TEST_FUSEKI_URL is set but BROWL_TEST_FUSEKI_BASE isn't")
    assert base is not None
    return base


@pytest.fixture(scope="session")
def family_dataset(fuseki_url: str) -> Iterator[str]:
    """Yield the name of an in-memory Fuseki dataset holding `tests/data/family.ttl`, removed afterwards."""
    name = f"test-{uuid.uuid4().hex[:8]}"
    with httpx2.Client(base_url=fuseki_url, timeout=30) as client:
        client.post("/$/datasets", data={"dbName": name, "dbType": "mem"}).raise_for_status()
        try:
            client.post(
                f"/{name}/data",
                content=(DATA / "family.ttl").read_bytes(),
                headers={"Content-Type": "text/turtle"},
            ).raise_for_status()
            yield name
        finally:
            client.delete(f"/$/datasets/{name}")
