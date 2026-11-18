"""Helpers shared by the tests."""

import asyncio
import http.server
import os
import subprocess
import threading
from pathlib import Path

import httpx2
from fastapi import FastAPI

from browl.app import create_app
from browl.imports import OWL_IMPORTS
from browl.settings import Settings


def make_settings(**overrides: object) -> Settings:
    """Return settings for tests: authentication off, DATA_DIR from the environment, and any overrides."""
    defaults = {"BASE_URL": "http://example.org/", "AUTH_DISABLED": True, "DATA_DIR": os.environ["DATA_DIR"]}
    return Settings.model_validate(defaults | overrides)


def make_app(
    fuseki_transport: httpx2.AsyncBaseTransport | None = None, real_stages: bool = False, **overrides: object
) -> FastAPI:
    """Return an app that doesn't start the JVM, with `make_settings(**overrides)` and the Fuseki transport given.

    Its load jobs run no background stages, unless `real_stages`, when they run the app's own.
    """
    return create_app(
        make_settings(**overrides),
        fuseki_transport=fuseki_transport,
        start_jvm=False,
        stages=None if real_stages else {},
    )


class StaticServer:
    """A local HTTP server with fixed responses, recording the paths requested.

    Attributes:
        url: Its base URL.
        pages: Each path's status, headers and body.
        requested: The paths requested, in order.
    """

    def __init__(self) -> None:
        """Start serving on a free port."""
        self.pages: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.requested: list[str] = []
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                site.requested.append(self.path)
                status, headers, body = site.pages.get(self.path, (404, {}, b"not found"))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        # A short poll interval: `shutdown()` waits up to one, 0.5 s by default, in each test's teardown.
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def page(self, path: str, body: str | bytes, content_type: str = "text/turtle", status: int = 200) -> str:
        """Serve a page, returning its URL."""
        self.pages[path] = (status, {"Content-Type": content_type}, body.encode() if isinstance(body, str) else body)
        return self.url + path

    def close(self) -> None:
        """Stop serving."""
        self._server.shutdown()
        self._server.server_close()


def turtle(iri: str, *imports: str, extra: str = "") -> str:
    """Return a Turtle ontology with the IRI and imports."""
    lines = [f"<{iri}> a <http://www.w3.org/2002/07/owl#Ontology> ."]
    lines += [f"<{iri}> <{OWL_IMPORTS}> <{imported}> ." for imported in imports]
    return "\n".join([*lines, extra, ""])


WAIT_S = 10.0
"""How long `wait_for` waits."""


async def wait_for(path: Path, run: asyncio.Future[object] | None = None) -> int:
    """Wait for a file to hold a number, such as a pid a run writes, and return it.

    Raises:
        TimeoutError: It took longer than `WAIT_S`.
        AssertionError: The run ended first (or its exception, if it failed).
    """
    async with asyncio.timeout(WAIT_S):
        while not path.exists() or not path.read_text().strip():
            if run is not None and run.done():
                raise AssertionError(f"the run ended first: {run.result()!r}")  # or raises how it failed
            await asyncio.sleep(0.02)
    return int(path.read_text())


def running_in_group(pgid: int) -> list[str]:
    """Return the `ps` lines of the processes still running in the process group, ignoring zombies.

    A zombie has exited, but stays in its group until its parent reaps it: here a child of the killed shell, which is
    reparented to a process that may never reap it (as on GitHub's runners), so `os.killpg(pgid, 0)` still succeeds.
    """
    ps = subprocess.run(["ps", "-A", "-o", "pgid=,stat=,pid=,command="], check=True, capture_output=True, text=True)
    return [line for line in ps.stdout.splitlines() if int(line.split()[0]) == pgid and line.split()[1][0] != "Z"]
