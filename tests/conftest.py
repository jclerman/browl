"""Shared fixtures, and the skip rules for tests that need a JVM or Fuseki.

`jvm` tests need Java and `robot.jar` (`ROBOT_JAR`, or the settings' default path); JPype finds Java through
`JAVA_HOME`, so point it at Java 21. `fuseki` tests need a Fuseki: the one at `BROWL_TEST_FUSEKI_URL`, or one the
tests build and start from `docker/fuseki` with Docker. Locally, tests whose needs are missing are skipped; in CI
(`CI` set) they fail instead, so CI runs every test. Mark such tests `jvm` or `fuseki`, and have them use the `jvm` or
`fuseki_url` fixture.
"""

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx2
import jpype
import pytest

from browl import owlapi
from browl.settings import Settings

ROOT = Path(__file__).parent.parent
DATA = Path(__file__).parent / "data"
FUSEKI_IMAGE = "browl-fuseki-test"
FUSEKI_START_TIMEOUT_S = 60


def robot_jar() -> Path:
    """Return the `robot.jar` the tests use: `ROBOT_JAR`, or the settings' default."""
    default = Settings.model_fields["robot_jar"].default
    return Path(os.environ.get("ROBOT_JAR", default))


def missing_jvm() -> str | None:
    """Return why the JVM can't start, or None if it can."""
    if jpype.isJVMStarted():
        return None
    if not robot_jar().is_file():
        return f"no robot.jar at {robot_jar()} (set ROBOT_JAR)"
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
def jvm() -> None:
    """Start the JVM, on the main thread as it must be, or skip the test if it can't start."""
    reason = missing_jvm()
    if reason:
        skip_or_fail(reason)
    owlapi.start_jvm(robot_jar(), "1g")


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
def fuseki_url() -> Iterator[str]:
    """Yield the URL of a Fuseki for the tests: `BROWL_TEST_FUSEKI_URL`, or one started in Docker from browl's image."""
    if url := os.environ.get("BROWL_TEST_FUSEKI_URL", "").rstrip("/"):
        _wait_for_fuseki(url)
        yield url
        return
    if (
        shutil.which("docker") is None
        or subprocess.run(["docker", "info"], check=False, capture_output=True).returncode
    ):
        skip_or_fail("no Fuseki: set BROWL_TEST_FUSEKI_URL, or make Docker available")
    _docker("build", "--quiet", "--tag", FUSEKI_IMAGE, str(ROOT / "docker" / "fuseki"))
    container = _docker("run", "--detach", "--rm", "--publish", "127.0.0.1::3030", FUSEKI_IMAGE)
    try:
        url = f"http://{_docker('port', container, '3030/tcp').splitlines()[0]}"
        _wait_for_fuseki(url)
        yield url
    finally:
        subprocess.run(["docker", "rm", "--force", container], check=False, capture_output=True)


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
