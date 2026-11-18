"""Check, in the Compose deployment, that ROBOT's imports can't reach what the app's container can (P-30).

Run in the app's container: `docker compose exec -T app python - < docker/sandbox-check.py`. ROBOT reasons over an
ontology on the jobs volume, with an empty catalog, once for each of two imports:

- an `http:` URL that this script serves from the app's container, by IP address, so it isn't DNS that stops it;
- a `file:` IRI of the job database, which only the app's container has.

Each import is tried as the load job runs ROBOT (`run_robot`), and again without the JVM options that refuse ROBOT's
HTTP requests, so that only the sandbox stops it. Each run must fail naming its import, with no request reaching the
server and the file not found.

With `--unsandboxed`, ROBOT runs in the app's container instead, to show what the sandbox stops: the server gets
requests and the file is found. It reports what happened, and fails only if the sandbox is used.
"""

import asyncio
import shutil
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from browl.imports import write_catalog
from browl.jobs import STATE_DB, Reasoner, StageError
from browl.process import java, run_process
from browl.robot import reason_args, run_robot
from browl.settings import Settings

ONTOLOGY = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
<urn:browl:sandbox-check> a owl:Ontology ; owl:imports <{iri}> .
<urn:browl:sandbox-check#A> a owl:Class .
"""

IMPORTED = b"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
<urn:browl:sandbox-check-import> a owl:Ontology .
"""

NOT_FOUND = "No such file or directory"
"""How Java reports the missing file, which the sandbox can't see."""

requests: list[str] = []


class Handler(BaseHTTPRequestHandler):
    """Records each request, and answers it with an ontology."""

    def do_GET(self) -> None:
        """Record the request and answer it."""
        requests.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/turtle")
        self.end_headers()
        self.wfile.write(IMPORTED)

    def log_message(self, format: str, *args: object) -> None:
        """Log nothing."""


async def reason(settings: Settings, directory: Path, iri: str, *, proxy: bool) -> str | None:
    """Reason over an ontology importing the IRI; return why ROBOT failed, or None if it succeeded."""
    source = directory / "ontology.ttl"
    source.write_text(ONTOLOGY.format(iri=iri))
    catalog = directory / "catalog.xml"
    write_catalog(catalog, {})
    args = reason_args(Reasoner.ELK, source, directory / "reasoned.ttl", settings, catalog)
    if proxy:
        try:
            await run_robot(args, directory, settings)
        except StageError as error:
            return str(error)
        return None
    command = [java(), f"-Xmx{settings.reasoner_max_heap}", "-jar", str(settings.robot_jar), "-vv", *args]
    completed = await run_process(
        command, timeout_s=settings.reasoner_timeout_s, name="ROBOT", sandbox=settings.sandbox_socket
    )
    return None if completed.ok else completed.output


async def main() -> int:
    """Run the checks; return the exit status."""
    sandboxed = "--unsandboxed" not in sys.argv[1:]
    settings = Settings()  # type: ignore[call-arg]  # the app's settings, from its environment
    if not sandboxed:
        settings = settings.model_copy(update={"sandbox_socket": None})
    elif settings.sandbox_socket is None:
        print("FAIL: SANDBOX_SOCKET is unset")
        return 1
    database = settings.data_dir / STATE_DB
    if not database.is_file():
        print(f"FAIL: {database} doesn't exist in the app's container")
        return 1
    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)  # reached by the container's address
    threading.Thread(target=server.serve_forever, daemon=True).start()
    address = socket.gethostbyname(socket.gethostname())
    url = f"http://{address}:{server.server_address[1]}/ontology.ttl"
    directory = Path(tempfile.mkdtemp(prefix="sandbox-check-", dir=settings.data_dir / "uploads"))
    failures = 0
    try:
        for iri in (url, database.as_uri()):
            for proxy in (True, False):
                requests.clear()
                reason_ = await reason(settings, directory, iri, proxy=proxy)
                found = reason_ is None or NOT_FOUND not in reason_
                leaked = bool(requests) if iri.startswith("http:") else found
                ok = not sandboxed or (reason_ is not None and iri in reason_ and not leaked)
                failures += not ok
                with_proxy = "with" if proxy else "without"
                what = f"{len(requests)} requests received" if iri.startswith("http:") else f"file found: {found}"
                label = ("ok" if ok else "FAIL") if sandboxed else "unsandboxed"
                print(f"{label}: {iri}, {with_proxy} the no-network JVM options: {what}")
                lines = [line for line in (reason_ or "").splitlines() if iri in line]
                print(f"  ROBOT {'succeeded' if reason_ is None else 'failed'}", *lines[:1], sep="\n  ")
    finally:
        server.shutdown()
        shutil.rmtree(directory)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
