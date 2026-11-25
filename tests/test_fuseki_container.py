"""Tests for how `tests/conftest.py` starts the tests' Fuseki containers."""

import socket
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest

from tests import conftest
from tests.conftest import fuseki_docker, skip_or_fail


@pytest.fixture
def taken_port() -> Iterator[int]:
    """Yield a local port something is listening on."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        yield listener.getsockname()[1]


def test_taken_port_tried_again(
    fuseki_url: str, taken_port: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that a Fuseki on the host's network whose port was taken meanwhile gets another, and answers there.

    It uses `fuseki_url` for its mark: it needs Docker as the other Fuseki tests do. It runs only on the host's network,
    as in CI.
    """
    image, host_network = fuseki_docker()
    if not host_network:
        skip_or_fail("containers can't use the host's network here; that needs Docker on Linux, not Docker Desktop")
    ports = iter([taken_port])
    free_port = conftest._free_port
    monkeypatch.setattr(conftest, "_free_port", lambda: next(ports, None) or free_port())
    container, url = conftest._start_fuseki(image, tmp_path, host_network=True)
    try:
        assert url.startswith("http://127.0.0.1:")
        assert url != f"http://127.0.0.1:{taken_port}"
        assert httpx2.get(f"{url}/$/ping", timeout=5).is_success
    finally:
        conftest._remove_container(container)
