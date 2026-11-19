"""Helpers shared by the tests."""

import asyncio
import http.server
import os
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from collections.abc import AsyncIterator, Coroutine, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import uvicorn
from fastapi import FastAPI

from browl import derived
from browl.app import create_app
from browl.fuseki import END_VARIABLE, TSV
from browl.imports import OWL_IMPORTS
from browl.jobs import JobContext, StageFunction
from browl.jobtypes import ImportResult, Reasoner
from browl.precache.core import Hook
from browl.precache.entities import Reserved, fill_hash_prefix, insert_entities
from browl.precache.keys import build_keys
from browl.precache.stage import HOOKS
from browl.sandbox import Runner
from browl.settings import Settings
from browl.urls import URLMapper


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


class LiveServer:
    """An app served by Uvicorn on a free local port, in a thread with its own event loop, for browser tests.

    A browser can't reach the in-process `TestClient`. The server runs in this process, so tests can still reach into
    the app, running its coroutines with `call`.

    Attributes:
        app: The app.
        url: Its base URL, with no trailing slash.
    """

    def __init__(self, app: FastAPI) -> None:
        """Start serving the app, and return once it's ready."""
        self.app = app
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", timeout_graceful_shutdown=1)
        self._server = uvicorn.Server(config)
        self._loop = asyncio.new_event_loop()
        # A daemon, so a server that won't stop can't keep pytest from exiting.
        self._thread = threading.Thread(target=self._loop.run_until_complete, args=(self._server.serve(),), daemon=True)
        self._thread.start()
        deadline = time.monotonic() + WAIT_S
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                self.close()
                raise RuntimeError("the live server didn't start")
            time.sleep(0.01)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    def call[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        """Run a coroutine on the server's event loop, as the app's own code runs, and return its result."""
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(WAIT_S)

    def close(self) -> None:
        """Stop serving, running the app's shutdown."""
        self._server.should_exit = True
        self._thread.join(WAIT_S)
        if self._thread.is_alive():
            raise RuntimeError("the live server didn't stop")
        self._loop.close()


def turtle(iri: str, *imports: str, extra: str = "") -> str:
    """Return a Turtle ontology with the IRI and imports."""
    lines = [f"<{iri}> a <http://www.w3.org/2002/07/owl#Ontology> ."]
    lines += [f"<{iri}> <{OWL_IMPORTS}> <{imported}> ." for imported in imports]
    return "\n".join([*lines, extra, ""])


@asynccontextmanager
async def serve_runs(directory: Path) -> AsyncIterator[Path]:
    """Serve the sandbox's runs in this process, on this event loop, on a socket in the directory, which it yields."""
    socket = directory / "socket"
    runner = Runner()
    server = await asyncio.start_unix_server(runner.handle, socket)
    try:
        yield socket
    finally:
        server.close()
        runner.kill_all()


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


# Stubs and helpers for the pre-caching tests: a stubbed Fuseki's answers, a job, and its derived database.

EX = "http://example.org/"
ONTOLOGY = f"{EX}onto"
VERSION = f"{EX}onto/1.0"
OWL = "http://www.w3.org/2002/07/owl#"
VERSION_IRI = f"<{OWL}versionIRI>"
VERSION_INFO = f"<{OWL}versionInfo>"
DCTERMS_TITLE = "<http://purl.org/dc/terms/title>"
DC_TITLE = "<http://purl.org/dc/elements/1.1/title>"
LABEL = "<http://www.w3.org/2000/01/rdf-schema#label>"
HEADER = f"?ontology\t?property\t?value\t?{END_VARIABLE}\n"
END = "\t\t\ttrue\n"


ONTOLOGY_RESULT = (
    f"{HEADER}<{ONTOLOGY}>\t{VERSION_IRI}\t<{VERSION}>\t\n"
    f'<{ONTOLOGY}>\t{DCTERMS_TITLE}\t"Onto"@en\t\n<{ONTOLOGY}>\t{VERSION_INFO}\t"1.0"\t\n{END}'
)
LOADED_AT = datetime(2026, 11, 17, tzinfo=UTC)
ENTITY_HEADER = f"?entity\t?type\t?{END_VARIABLE}\n"
ENTITY_END = "\t\ttrue\n"
ENTITY_RESULT = f"{ENTITY_HEADER}<{EX}onto#A>\t<{OWL}Class>\t\n{ENTITY_END}"


NAME_HEADER = f"?entity\t?name\t?property\t?value\t?{END_VARIABLE}\n"
NAME_END = "\t\t\t\ttrue\n"
NAME_RESULT = f'{NAME_HEADER}<{EX}onto#A>\t{LABEL}\t{LABEL}\t"A"@en\t\n{NAME_END}'


def answer(query: str) -> str:
    """Return a stubbed Fuseki's answer to a query: to the entity list, the name values query, else the header's."""
    if "?entity ?type" in query:
        return ENTITY_RESULT
    if "?entity ?name" in query:
        return NAME_RESULT
    return ONTOLOGY_RESULT


def fuseki(*bodies: str, queries: list[str] | None = None) -> httpx2.AsyncClient:
    """Return an HTTP client whose stubbed Fuseki answers the queries with these TSV bodies in turn, recording them.

    Without bodies, each query gets its `answer`; the last body answers the rest of the queries.
    """
    answers = list(bodies)

    async def handler(request: httpx2.Request) -> httpx2.Response:
        query = urllib.parse.parse_qs(request.content.decode())["query"][0]
        if queries is not None:
            queries.append(query)
        if not answers:
            body = answer(query)
        else:
            body = answers.pop(0) if len(answers) > 1 else answers[0]
        return httpx2.Response(200, headers={"Content-Type": TSV}, content=body)

    return httpx2.AsyncClient(base_url="http://fuseki:3030", transport=httpx2.MockTransport(handler))


def job(settings: Settings, generation: int, imports: list[ImportResult] | None = None) -> JobContext:
    """Return a job's context as the loading stage leaves it, with an empty upload."""
    upload = settings.data_dir / "uploads" / str(generation) / "ontology.ttl"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.touch()
    return JobContext(generation, settings, Reasoner.ELK, upload=upload, imports=imports or [], started_at=LOADED_AT)


def connect(settings: Settings, generation: int, hooks: Sequence[Hook] = HOOKS) -> sqlite3.Connection:
    """Open a generation's derived database, built with these settings and hooks (`derived.connect`)."""
    return derived.connect(settings.data_dir, generation, build_keys(settings, hooks))


def derived_stage(
    entities: Sequence[tuple[str, derived.EntityType]] = (),
    ontology_iri: str | None = None,
    title: str | None = None,
    names: Mapping[str, tuple[str | None, str | None]] | None = None,
) -> StageFunction:
    """Return a load job stage that builds the job's derived database with these entities, and the ontology if given.

    Their URLs come from the job's settings (`URLMapper`), and the rows and `hash_prefix` are written as the entities
    hook writes them (`browl.precache.entities`). `names` gives IRIs' display names and CURIEs.
    """

    def build(context: JobContext) -> None:
        settings = context.settings
        mapper = URLMapper(settings.base_url, settings.reserved_prefix)
        meta = derived.Meta(context.generation, context.reasoner, LOADED_AT, ontology_iri=ontology_iri, title=title)
        rows = [*([(ontology_iri, derived.EntityType.ONTOLOGY)] if ontology_iri else []), *entities]
        with derived.build(settings.data_dir, meta, build_keys(settings, HOOKS)) as connection:
            insert_entities(rows, mapper, Reserved(), connection)
            fill_hash_prefix(connection)
            for iri, (display_name, curie) in (names or {}).items():
                connection.execute(
                    "UPDATE entity SET display_name = ?, curie = ? WHERE iri = ?", (display_name, curie, iri)
                )

    async def stage(context: JobContext) -> None:
        await asyncio.to_thread(build, context)

    return stage
