"""Helpers shared by the tests."""

import asyncio
import contextlib
import http.server
import os
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Coroutine, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import jpype
import uvicorn
from fastapi import FastAPI

from browl import derived, search
from browl.app import create_app
from browl.derived import DerivedReader
from browl.fuseki import END_VARIABLE, TSV, FusekiClient
from browl.imports import OWL_IMPORTS
from browl.jobs import JobContext, StageFunction
from browl.jobtypes import ImportResult, Reasoner
from browl.loading import dataset_name
from browl.names import name_property_iris, normalize
from browl.precache import hierarchies
from browl.precache.core import Hook
from browl.precache.entities import Reserved, fill_hash_prefix, insert_entities
from browl.precache.keys import build_keys
from browl.precache.stage import HOOKS, pre_cache
from browl.rdf import RDF, RDFS
from browl.sandbox import Runner
from browl.settings import Settings
from browl.urls import URLMapper
from browl.usages import UsageStore


def make_settings(**overrides: object) -> Settings:
    """Return settings for tests: authentication off, DATA_DIR from the environment, and any overrides."""
    defaults = {"BASE_URL": "http://example.org/", "AUTH_DISABLED": True, "DATA_DIR": os.environ["DATA_DIR"]}
    return Settings.model_validate(defaults | overrides)


def make_app(
    fuseki_transport: httpx2.AsyncBaseTransport | None = None,
    real_stages: bool = False,
    usage_tables: bool = False,
    **overrides: object,
) -> FastAPI:
    """Return an app that doesn't start the JVM, with `make_settings(**overrides)` and the Fuseki transport given.

    Its load jobs run no background stages, unless `real_stages`, when they run the app's own; it builds no usage
    tables unless `usage_tables`.
    """
    return create_app(
        make_settings(**overrides),
        fuseki_transport=fuseki_transport,
        start_jvm=False,
        stages=None if real_stages else {},
        usage_tables=usage_tables,
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

AXIOMS_ONTOLOGY = Path(__file__).parent / "data" / "axioms.ofn"
"""A small ontology with every kind of OWL 2 axiom, in functional syntax."""


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


EDGE_HEADER = f"?child\t?relation\t?parent\t?{END_VARIABLE}\n"
EDGE_END = "\t\t\ttrue\n"
EDGE_RESULT = EDGE_HEADER + EDGE_END

OWNER_MAP_HEADER = f"?node\t?predicate\t?object\t?blank\t?parent\t?{END_VARIABLE}\n"
OWNER_MAP_END = "\t\t\t\t\ttrue\n"
OWNER_MAP_RESULT = (
    OWNER_MAP_HEADER
    # x SubClassOf p some A; an axiom annotated with A; A in an n-ary DisjointClasses, which is its own.
    + f'"r"\t<{RDFS}subClassOf>\t\t\t<{EX}x>\t\n"r"\t<{OWL}someValuesFrom>\t<{EX}onto#A>\t\t\t\n'
    + f'"r"\t<{OWL}onProperty>\t<{EX}p>\t\t\t\n'
    + f'"a"\t<{RDF}type>\t<{OWL}Axiom>\t\t\t\n"a"\t<{OWL}annotatedSource>\t<{EX}x>\t\t\t\n'
    + f'"a"\t{LABEL}\t<{EX}onto#A>\t\t\t\n"a"\t{LABEL}\t"note"\t\t\t\n'
    + f'"d"\t<{RDF}type>\t<{OWL}AllDisjointClasses>\t\t\t\n"d"\t<{OWL}members>\t\t"l"\t\t\n'
    + f'"l"\t<{RDF}first>\t<{EX}onto#A>\t\t\t\n"l"\t<{RDF}rest>\t<{RDF}nil>\t\t\t\n'
    + OWNER_MAP_END
)


SCAN_RESULT = (
    f"?subject\t?predicate\t?object\t?{END_VARIABLE}\n<{EX}x>\t<{RDFS}subClassOf>\t<{EX}onto#A>\t\n"
    f"<{EX}x>\t{LABEL}\t\t\n_:b\t<{EX}p>\t<{EX}onto#A>\t\n\t\t\ttrue\n"
)
"""The usage table's scan (`usage-scan.rq`): x SubClassOf A, x's label, and a blank subject's triple naming A."""


def answer(query: str) -> str:
    """Return a stubbed Fuseki's answer: the entities', names', edges', owner map's, usage scan's, else the header's."""
    if "?subject ?predicate ?object" in query:
        return SCAN_RESULT
    if "?triples" in query:
        return "?triples\n3\n"
    if "?entity ?type" in query:
        return ENTITY_RESULT
    if "?node ?predicate" in query:
        return OWNER_MAP_RESULT
    if "?entity ?name" in query:
        return NAME_RESULT
    if "?child ?relation" in query:
        return EDGE_RESULT
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


def iso(text: str) -> str:
    """Return text as titles isolate it, between U+2068 and U+2069."""
    return f"\u2068{text}\u2069"


def set_names(connection: sqlite3.Connection, names: Mapping[str, tuple[str | None, str | None]]) -> None:
    """Set IRIs' display names and CURIEs in a derived database's `entity` table."""
    for iri, (display_name, curie) in names.items():
        connection.execute("UPDATE entity SET display_name = ?, curie = ? WHERE iri = ?", (display_name, curie, iri))


def derived_stage(
    entities: Sequence[tuple[str, derived.EntityType]] = (),
    ontology_iri: str | None = None,
    title: str | None = None,
    names: Mapping[str, tuple[str | None, str | None]] | None = None,
    equivalence_groups: Sequence[Sequence[str]] = (),
    edges: Sequence[tuple[str, str, str]] | None = None,
    name_values: Sequence[tuple[str, str, str, str | None, bool]] = (),
) -> StageFunction:
    """Return a load job stage that builds the job's derived database with these entities, and the ontology if given.

    Their URLs come from the job's settings (`URLMapper`), and the rows and `hash_prefix` are written as the entities
    hook writes them (`browl.precache.entities`). `names` gives IRIs' display names and CURIEs; `equivalence_groups`
    classes' equivalence groups (the `equiv_group` table), each with its representative first. `edges`, if given,
    are the edges query's rows, (child, relation IRI, parent), from which the hierarchies hook fills `equiv_group`,
    `edge`, `counts` and `root`. `name_values` gives name values, (IRI, name property IRI, value, language, direct),
    which fill `name_value` and the search index (`browl.search`) for each type the IRI has.
    """

    def build(context: JobContext) -> None:
        settings = context.settings
        mapper = URLMapper(settings.base_url, settings.reserved_prefix)
        meta = derived.Meta(context.generation, context.reasoner, LOADED_AT, ontology_iri=ontology_iri, title=title)
        rows = [*([(ontology_iri, derived.EntityType.ONTOLOGY)] if ontology_iri else []), *entities]
        with derived.build(settings.data_dir, meta, build_keys(settings, HOOKS)) as connection:
            insert_entities(rows, mapper, Reserved(), connection)
            fill_hash_prefix(connection)
            set_names(connection, names or {})
            for group in equivalence_groups:
                connection.executemany(
                    "INSERT INTO equiv_group (entity, representative) SELECT e.id, r.id FROM entity AS e, entity AS r"
                    " WHERE e.iri = ? AND r.iri = ? AND e.type = 'class' AND r.type = 'class'",
                    [(member, group[0]) for member in group],
                )
            if edges is not None:
                hierarchies.create_staging(connection)
                connection.executemany(
                    "INSERT INTO edge_staging VALUES (?, ?, ?)",
                    [(child, hierarchies.RELATION_CODES[relation], parent) for child, relation, parent in edges],
                )
                hierarchies.fill_staged(connection)
            if name_values:
                connection.executemany(
                    "INSERT INTO name_value (entity, property, value, normalized, language, direct)"
                    " SELECT id, ?, ?, ?, ?, ? FROM entity WHERE iri = ?",
                    [(p, value, normalize(value), lang, direct, iri) for iri, p, value, lang, direct in name_values],
                )
                search._fill(connection, name_property_iris(settings), settings.language_ranking)

    async def stage(context: JobContext) -> None:
        await asyncio.to_thread(build, context)

    return stage


@contextlib.contextmanager
def fuseki_dataset(fuseki_url: str, graphs: dict[str | None, bytes]) -> Iterator[str]:
    """Yield the name of an in-memory Fuseki dataset holding N-Triples by graph (None: the default), then remove it."""
    name = f"test-{uuid.uuid4().hex[:8]}"
    with httpx2.Client(base_url=fuseki_url, timeout=30) as client:
        client.post("/$/datasets", data={"dbName": name, "dbType": "mem"}).raise_for_status()
        try:
            headers = {"Content-Type": "application/n-triples"}
            for graph, triples in graphs.items():
                params = {"default": ""} if graph is None else {"graph": graph}
                client.post(f"/{name}/data", params=params, content=triples, headers=headers).raise_for_status()
            yield name
        finally:
            client.delete(f"/$/datasets/{name}")


async def no_axioms(*args: object) -> tuple[list[object], bool]:
    """Stand in for `browl.overview._read_axioms`, finding no axioms without a query."""
    return [], False


def ontology_ntriples() -> bytes:
    """Return the test ontology as N-Triples, as the OWLAPI writes it: the RDF that browl's store would hold."""
    manager = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").createOWLOntologyManager()
    ontology = manager.loadOntologyFromOntologyDocument(jpype.JClass("java.io.File")(str(AXIOMS_ONTOLOGY)))
    out = jpype.JClass("java.io.ByteArrayOutputStream")()
    manager.saveOntology(ontology, jpype.JClass("org.semanticweb.owlapi.formats.NTriplesDocumentFormat")(), out)
    return bytes(out.toByteArray())


@asynccontextmanager
async def loaded(
    fuseki_url: str, settings: Settings, graphs: dict[str | None, bytes], dataset_clause: str
) -> AsyncIterator[UsageStore]:
    """Yield a store of N-Triples by graph (None: the default): an in-memory Fuseki dataset, and its derived database.

    The named graphs are imports, which the pre-caching stage sees.
    """
    generation = 900_000 + uuid.uuid4().int % 100_000
    name = dataset_name(generation)
    async with httpx2.AsyncClient(base_url=fuseki_url, timeout=60) as http:
        (await http.post("/$/datasets", data={"dbName": name, "dbType": "mem"})).raise_for_status()
        try:
            for graph, triples in graphs.items():
                params = {"default": ""} if graph is None else {"graph": graph}
                headers = {"Content-Type": "application/n-triples"}
                (await http.post(f"/{name}/data", params=params, content=triples, headers=headers)).raise_for_status()
            imports = [ImportResult(graph, triples=1) for graph in graphs if graph is not None]
            await pre_cache(JobContext(generation, settings, Reasoner.NONE, imports=imports), http)
            reader = DerivedReader(settings.data_dir, build_keys(settings, HOOKS))
            try:
                yield UsageStore(FusekiClient(http), reader, generation, dataset_clause)
            finally:
                reader.close()
        finally:
            await http.delete(f"/$/datasets/{name}")
