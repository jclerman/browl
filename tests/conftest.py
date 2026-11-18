"""Shared fixtures, and the skip rules for tests that need a JVM or Fuseki.

`jvm` tests need Java and `robot.jar`: `ROBOT_JAR`, else the one `make robot-jar` downloads, else the settings'
default path. JPype finds Java through `JAVA_HOME`, so point it at Java 21; on macOS, when it's unset, the tests set it
to Java 21 if that's installed. `fuseki` tests need a Fuseki: the one at `BROWL_TEST_FUSEKI_URL`, or one the
tests build and start from `docker/fuseki` with Docker. Tests that build stores for it (`fuseki_base`) also need Jena's
tools (`JENA_HOME`, else the ones `make jena` downloads) and its base directory: for the Fuseki the tests start, a
temporary directory mounted as its base; for one at `BROWL_TEST_FUSEKI_URL`, `BROWL_TEST_FUSEKI_BASE`. Locally,
tests whose needs are missing are skipped; in CI
(`CI` set) they fail instead, so CI runs every test. Mark such tests `jvm` or `fuseki`, and have them use the `jvm` or
`fuseki_url` fixture.
"""

import http.server
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx2
import jpype
import pytest

from browl import owlapi
from browl.settings import Settings
from tests.helpers import Site

ROOT = Path(__file__).parent.parent
DATA = Path(__file__).parent / "data"
FUSEKI_IMAGE = "browl-fuseki-test"
FUSEKI_START_TIMEOUT_S = 60
ROBOT_JAR_CACHE = ROOT / ".cache" / "robot.jar"  # where `make robot-jar` puts it
JENA_CACHE = ROOT / ".cache" / "jena"  # where `make jena` puts it
MACOS_JAVA_HOME = "/usr/libexec/java_home"


@pytest.fixture
def anyio_backend() -> str:
    """Run the async tests on asyncio, as the app does."""
    return "asyncio"


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return an empty directory, set as `DATA_DIR` in the environment."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def site() -> Iterator[Site]:
    """Yield a local HTTP server with fixed responses (`Site`)."""
    server = Site()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def http_server() -> Iterator[tuple[str, list[str]]]:
    """Yield the base URL of a local HTTP server, and the list of paths it's asked for; it answers each with 404."""
    requests: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_error(404)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()


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
    """Return why the JVM can't start, or None if it can."""
    if jpype.isJVMStarted():
        return None
    if not find_robot_jar().is_file():
        return f"no robot.jar at {find_robot_jar()} (run `make robot-jar`, or set ROBOT_JAR)"
    default_java_home()
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
def jvm() -> None:
    """Start the JVM, on the main thread as it must be, or skip the test if it can't start."""
    reason = missing_jvm()
    if reason:
        skip_or_fail(reason)
    owlapi.start_jvm(find_robot_jar(), "1g")


def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


def _wait_for_fuseki(url: str) -> None:
    deadline = time.monotonic() + FUSEKI_START_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if httpx2.get(f"{url}/$/ping", timeout=2).is_success:
                return
        except httpx2.HTTPError:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"Fuseki at {url} didn't answer within {FUSEKI_START_TIMEOUT_S} s")


@pytest.fixture(scope="session")
def fuseki_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path | None]]:
    """Yield a Fuseki's URL and its base directory as the tests see it, None if unknown.

    That's `BROWL_TEST_FUSEKI_URL` and `BROWL_TEST_FUSEKI_BASE`, or a Fuseki started in Docker from browl's image,
    whose base is a temporary directory, and which runs as the tests' user, so it can open and delete the stores the
    tests build there.
    """
    if url := os.environ.get("BROWL_TEST_FUSEKI_URL", "").rstrip("/"):
        _wait_for_fuseki(url)
        base = os.environ.get("BROWL_TEST_FUSEKI_BASE")
        yield url, Path(base) if base else None
        return
    if (
        shutil.which("docker") is None
        or subprocess.run(["docker", "info"], check=False, capture_output=True).returncode
    ):
        skip_or_fail("no Fuseki: set BROWL_TEST_FUSEKI_URL, or make Docker available")
    base = tmp_path_factory.mktemp("fuseki")
    _docker("build", "--quiet", "--tag", FUSEKI_IMAGE, str(ROOT / "docker" / "fuseki"))
    container = _docker(
        "run",
        "--detach",
        "--rm",
        "--publish",
        "127.0.0.1::3030",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--volume",
        f"{base}:/fuseki",
        FUSEKI_IMAGE,
    )
    try:
        url = f"http://{_docker('port', container, '3030/tcp').splitlines()[0]}"
        _wait_for_fuseki(url)
        yield url, base
    finally:
        subprocess.run(["docker", "rm", "--force", container], check=False, capture_output=True)


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
