"""Check, in the Compose deployment, that the tools work in the sandbox and that ROBOT's imports reach nothing (P-30).

Run in the app's container: `docker compose exec -T app python - < docker/sandbox-check.py`.

- ROBOT reasons over an ontology with no imports, and the OFN lister converts a small document; the app reads both
  outputs from the jobs volume.
- ROBOT reasons over an ontology with an empty catalog, once for each of two imports: an `http:` URL that this script
  serves from the app's container, by IP address, so it isn't DNS that stops it; and a `file:` IRI of the job database,
  which only the app's container has. Each is tried as the load job runs ROBOT (`run_robot`), and again without the JVM
  options that refuse ROBOT's HTTP requests, so that only the sandbox stops it. Each run must fail naming its import,
  with no request reaching the server and the file not found.

With `--unsandboxed`, the control: ROBOT runs in the app's container, without those JVM options, and each import must
reach its target (the server gets requests, the file is found), so the check above can see a leak.
"""

import asyncio
import shutil
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import browl.robot
from browl.imports import write_catalog
from browl.jobs import STATE_DB
from browl.jobtypes import Reasoner, StageError
from browl.ofn import convert_ofn
from browl.robot import reason_args, run_robot
from browl.settings import Settings

ONTOLOGY = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<urn:browl:sandbox-check> a owl:Ontology {imports}.
<urn:browl:sandbox-check#A> a owl:Class ; rdfs:subClassOf <urn:browl:sandbox-check#B> .
<urn:browl:sandbox-check#B> a owl:Class .
"""

OFN = """Ontology(<urn:browl:sandbox-check>
Import(<urn:browl:sandbox-check-import>)
Declaration(Class(<urn:browl:sandbox-check#A>))
)
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


async def reason(settings: Settings, directory: Path, iri: str | None, *, proxy: bool = True) -> str | None:
    """Reason over an ontology importing the IRI, if any; return why ROBOT failed, or None if it succeeded.

    Args:
        settings: The app's settings.
        directory: Where to write the ontology, catalog and output.
        iri: What the ontology imports.
        proxy: Whether ROBOT gets the JVM options that refuse its HTTP requests.
    """
    source = directory / "ontology.ttl"
    source.write_text(ONTOLOGY.format(imports=f"; owl:imports <{iri}> " if iri else ""))
    catalog = directory / "catalog.xml"
    write_catalog(catalog, {})
    args = reason_args(Reasoner.ELK, source, directory / "reasoned.ttl", settings, catalog)
    try:
        with mock.patch.object(
            browl.robot, "NO_NETWORK_JAVA_OPTIONS", browl.robot.NO_NETWORK_JAVA_OPTIONS if proxy else ()
        ):
            await run_robot(args, directory, settings)
    except StageError as error:
        return str(error)
    return None


async def tools_work(settings: Settings, directory: Path) -> int:
    """Check that ROBOT and the OFN lister succeed, writing what the app then reads; return how many failed."""
    failures = 0
    reason_ = await reason(settings, directory, None)
    output = directory / "reasoned.ttl"
    ok = reason_ is None and "urn:browl:sandbox-check#A" in output.read_text()
    failures += not ok
    print(f"{'ok' if ok else 'FAIL'}: reasoning over an ontology with no imports", *([reason_] if reason_ else []))
    source = directory / "ontology.ofn"
    source.write_text(OFN)
    try:
        listing = await convert_ofn(source, directory / "converted.ttl", settings)
        ok = (
            listing.imports == ["urn:browl:sandbox-check-import"]
            and "urn:browl:sandbox-check#A" in (directory / "converted.ttl").read_text()
        )
        detail = f"imports {listing.imports}"
    except StageError as error:
        ok, detail = False, str(error)
    failures += not ok
    print(f"{'ok' if ok else 'FAIL'}: converting an OFN document: {detail}")
    return failures


async def imports_isolated(settings: Settings, directory: Path, iris: tuple[str, str], *, sandboxed: bool) -> int:
    """Check each import, as described above; return how many checks failed."""
    failures = 0
    for iri in iris:
        for proxy in (True, False) if sandboxed else (False,):
            requests.clear()
            reason_ = await reason(settings, directory, iri, proxy=proxy)
            found = reason_ is None or NOT_FOUND not in reason_
            leaked = bool(requests) if iri.startswith("http:") else found
            ok = (reason_ is not None and iri in reason_ and not leaked) if sandboxed else leaked
            failures += not ok
            with_proxy = "with" if proxy else "without"
            what = f"{len(requests)} requests received" if iri.startswith("http:") else f"file found: {found}"
            print(f"{'ok' if ok else 'FAIL'}: {iri}, {with_proxy} the no-network JVM options: {what}")
            lines = [line for line in (reason_ or "").splitlines() if iri in line]
            print(f"  ROBOT {'succeeded' if reason_ is None else 'failed'}", *lines[:1], sep="\n  ")
    return failures


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
    try:
        failures = await tools_work(settings, directory) if sandboxed else 0
        failures += await imports_isolated(settings, directory, (url, database.as_uri()), sandboxed=sandboxed)
    finally:
        server.shutdown()
        shutil.rmtree(directory)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
