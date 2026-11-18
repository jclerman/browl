"""The imports stage (DESIGN.md §2.4): fetch the ontology's imports, recursively, and write ROBOT's catalog for them.

Neither the TDB2 loader nor browl's queries follow `owl:imports`, and ROBOT would follow them itself, reading local
files (`file:`) or sending requests from the app container. So browl lists every import, fetches only `http` and
`https` IRIs from public addresses (checked on each connection, so on each redirect too), within a size limit, and
writes a catalog mapping every import to its fetched file, or to an empty ontology if it was dropped. A dropped import
is a warning, not a failure (A-4). The flow is charted in docs/LOAD-FLOW.md.

Imports are listed by the parser that reads the document next. An RDF document's are listed by Jena's `riot`, which
streams it as N-Triples, from the whole file. An OWL Functional Syntax (OFN) document is read only by the OWLAPI, which
lists its imports and converts it to Turtle in one parse, with every other load refused (`browl.ofn`); from then on,
the Turtle stands in for it.
"""

import asyncio
import ipaddress
import logging
import re
import shutil
import socket
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import httpcore2
import httpx2

import browl
from browl.jobs import ImportResult, JobContext, StageError
from browl.ofn import convert_ofn
from browl.process import free_bytes, run_process
from browl.settings import Settings
from browl.upload import FREE_SPACE_MARGIN_BYTES, IDLE_S, SNIFF_BYTES, STORED_SUFFIXES, Syntax, sniff_syntax

logger = logging.getLogger(__name__)

OWL_IMPORTS = "http://www.w3.org/2002/07/owl#imports"

IMPORTS_DIR = "imports"
"""The fetched imports' directory, beside the upload."""

CATALOG = "catalog.xml"
"""The catalog's file name, beside the upload."""

CONVERTED = "converted.ttl"
"""An OFN upload converted to Turtle, beside the upload."""

MAX_IMPORTS = 100
"""The most imports fetched for one ontology, directly or not, counting failed attempts; any more are dropped."""

MAX_LISTED = 1000
"""The most distinct imports one load's documents may list. More fails the load: each needs a catalog entry, and an
ontology with that many is crafted."""

FETCH_ALL_TIMEOUT_S = 2 * 3600.0
"""The longest fetching all of one load's imports may take; imports not fetched by then are dropped."""

MAX_REDIRECTS = 10
CONNECT_TIMEOUT_S = 30.0
FETCH_TIMEOUT_S = 3600.0
"""The longest one import may take to fetch."""

SCAN_TIMEOUT_S = 4 * 3600.0
"""The longest listing one document's imports may take: `riot` parses NCBITaxon (2 GB of RDF/XML) in about 25 s."""

TAIL_LINES = 10
"""How many of `riot`'s last lines a failure's reason quotes."""

MAX_IMPORT_LINE_BYTES = 2**20
"""The longest N-Triples line read for an import; a longer one, e.g. a large literal, is skipped without being held."""

ACCEPT = "text/turtle, application/rdf+xml;q=0.9, text/owl-functional;q=0.8, */*;q=0.1"

SYNTAX_NAMES = {Syntax.TURTLE: "Turtle", Syntax.RDF_XML: "RDF/XML"}
"""Jena's names for the RDF syntaxes `riot` reads."""

_IRI = r"[^\s<>\"{}|^`\\]*"
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_NT_IMPORT = re.compile(rf"^(?:<{_IRI}>|_:\S+) <{re.escape(OWL_IMPORTS)}> <({_IRI})> \.$")


class _ImportLines:
    """Collect the `owl:imports` triples from N-Triples arriving in chunks, splitting only their lines out."""

    _NEEDLE = f"<{OWL_IMPORTS}>".encode()

    def __init__(self, found: dict[str, None]) -> None:
        self._found = found
        self._pending: list[bytes] = []  # the current line's start, until its end arrives
        self._pending_bytes = 0
        self._skipping = False  # whether the current line is too long to read

    def __call__(self, chunk: bytes) -> None:
        end = chunk.rfind(b"\n") + 1
        if not end:
            self._hold(chunk)
            return
        first = chunk.find(b"\n") + 1
        if self._skipping:
            data = chunk[first:end]  # the long line ends in this chunk
        else:
            data = b"".join([*self._pending, chunk[:end]])  # each byte is joined once, so this stays linear
        self._pending, self._pending_bytes, self._skipping = [], 0, False
        self._hold(chunk[end:])
        self._scan(data)

    def _hold(self, piece: bytes) -> None:
        if self._skipping or not piece:
            return
        self._pending.append(piece)
        self._pending_bytes += len(piece)
        if self._pending_bytes > MAX_IMPORT_LINE_BYTES:
            self._pending, self._pending_bytes, self._skipping = [], 0, True

    def _scan(self, data: bytes) -> None:
        end = len(data)
        start = data.find(self._NEEDLE)
        while start >= 0:
            line_start = data.rfind(b"\n", 0, start) + 1
            line_end = data.find(b"\n", start)
            if match := _NT_IMPORT.match(data[line_start:line_end].decode("utf-8", "replace")):
                self._found[match[1]] = None
                if len(self._found) > MAX_LISTED:  # enough to fail the load; keep no more
                    return
            start = data.find(self._NEEDLE, line_end, end)


async def list_rdf_imports(path: Path, syntax: Syntax, settings: Settings) -> list[str]:
    """Return the IRIs an RDF document imports, each once, parsing all of it with Jena's `riot`.

    Raises:
        StageError: `riot` couldn't parse it.
    """
    found: dict[str, None] = {}
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    command = [str(settings.jena_home / "bin" / "riot"), f"--syntax={SYNTAX_NAMES[syntax]}", "--output=nt", str(path)]
    completed = await run_process(
        command, tail.append, timeout_s=SCAN_TIMEOUT_S, on_stdout=_ImportLines(found), name="riot"
    )
    if completed.timed_out:
        raise StageError(f"listing imports timed out after {SCAN_TIMEOUT_S:g} s")
    if not completed.ok:
        lines = "\n".join(tail)
        raise StageError(f"parse error: Jena couldn't read {path.name} as {SYNTAX_NAMES[syntax]}:\n{lines}")
    return list(found)


def is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether an address is on the public internet: not loopback, private, link-local, reserved or multicast.

    An IPv6 address that embeds an IPv4 one (mapped, 6to4, Teredo) is judged by both.
    """
    if not address.is_global or address.is_multicast:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        embedded = [address.ipv4_mapped, address.sixtofour, *(address.teredo or ())]
        return all(is_public(inner) for inner in embedded if inner is not None)
    return True


AddressCheck = Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address], bool]


class BlockedAddressError(httpcore2.ConnectError):
    """A host resolved to no address that imports may be fetched from."""


class GuardedBackend(httpcore2.AsyncNetworkBackend):
    """A network backend that connects only to addresses the check allows, resolving each host itself.

    It connects to the very address it checked, so a host can't pass the check with one address and then be reached
    at another (DNS rebinding). TLS still checks the certificate against the host's name.

    Args:
        allowed: Whether an address may be connected to; `is_public` if not given.
    """

    def __init__(self, allowed: AddressCheck | None = None) -> None:
        """Wrap httpcore's own backend."""
        self._allowed = allowed or is_public
        self._backend = httpcore2.AnyIOBackend()

    async def connect_tcp(  # noqa: D102
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(timeout):
                infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, TimeoutError) as failed:
            raise httpcore2.ConnectError(f"couldn't resolve {host}: {failed}") from failed
        addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
        allowed = [address for address in addresses if self._allowed(ipaddress.ip_address(address.partition("%")[0]))]
        if not allowed:
            raise BlockedAddressError(f"{host} has no public address ({', '.join(addresses)})")
        errors: list[httpcore2.ConnectError] = []
        for address in allowed:
            try:
                return await self._backend.connect_tcp(address, port, timeout, local_address, socket_options)
            except httpcore2.ConnectError as failed:
                errors.append(failed)
        raise errors[-1]

    async def connect_unix_socket(  # noqa: D102
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        raise httpcore2.ConnectError("imports aren't fetched over Unix sockets")

    async def sleep(self, seconds: float) -> None:  # noqa: D102
        await self._backend.sleep(seconds)


def fetch_client(allowed: AddressCheck | None = None) -> httpx2.AsyncClient:
    """Return an HTTP client for fetching imports, which connects only to addresses the check (`is_public`) allows.

    It ignores proxy settings in the environment, which would make the proxy, not the import's host, the address
    checked.
    """
    transport = httpx2.AsyncHTTPTransport(trust_env=False)
    # httpx2's transport takes no network backend, so its pool is replaced with one that uses the guarded backend.
    transport._pool = httpcore2.AsyncConnectionPool(
        ssl_context=httpx2.create_ssl_context(trust_env=False), network_backend=GuardedBackend(allowed)
    )
    return httpx2.AsyncClient(
        transport=transport,
        trust_env=False,
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        timeout=httpx2.Timeout(CONNECT_TIMEOUT_S, read=IDLE_S),
        headers={"Accept": ACCEPT, "User-Agent": f"browl/{browl.__version__}"},
    )


class FetchError(Exception):
    """An import couldn't be fetched; the message says why, for the upload page."""


async def fetch(
    client: httpx2.AsyncClient,
    iri: str,
    path: Path,
    settings: Settings,
    max_bytes: int | None = None,
    timeout_s: float = FETCH_TIMEOUT_S,
) -> Syntax:
    """Fetch an import into a file, returning its syntax; the file is removed if the fetch fails.

    Args:
        client: The client to fetch with (`fetch_client`).
        iri: The import's IRI.
        path: Where to write it.
        settings: The settings.
        max_bytes: The most it may be: what's left of `MAX_UPLOAD_BYTES` for all imports; all of it if not given.
        timeout_s: The longest it may take.

    Raises:
        FetchError: It isn't an `http` or `https` IRI, its host isn't public, the request failed, it's too large, the
            data volume is low on space, or it isn't in a syntax browl reads.
    """
    if not iri.lower().startswith(("http://", "https://")):
        raise FetchError("only http and https imports are fetched")
    try:
        async with asyncio.timeout(timeout_s):
            return await _fetch(client, iri, path, settings, max_bytes)
    except TimeoutError as error:
        path.unlink(missing_ok=True)
        raise FetchError(f"fetching took longer than {timeout_s:g} s") from error
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def _fetch(client: httpx2.AsyncClient, iri: str, path: Path, settings: Settings, max_bytes: int | None) -> Syntax:
    limit = settings.max_upload_bytes if max_bytes is None else max_bytes
    sample = b""
    size = 0
    checked_at = 0
    try:
        async with client.stream("GET", iri) as response:
            if not response.is_success:
                raise FetchError(f"HTTP {response.status_code} from {response.url}")
            content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
            with path.open("wb") as file:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise FetchError(
                            f"larger than {limit:,} bytes (MAX_UPLOAD_BYTES)"
                            if limit == settings.max_upload_bytes
                            else f"larger than the {limit:,} bytes left of MAX_UPLOAD_BYTES for all imports together"
                        )
                    if size - checked_at > 64 * 2**20 or not checked_at:
                        checked_at = size
                        if await asyncio.to_thread(free_bytes, path.parent) < FREE_SPACE_MARGIN_BYTES:
                            raise FetchError("the data volume is low on space")
                    if len(sample) < SNIFF_BYTES:
                        sample += chunk[: SNIFF_BYTES - len(sample)]
                    await asyncio.to_thread(file.write, chunk)
    except httpx2.TooManyRedirects as error:
        raise FetchError(f"more than {MAX_REDIRECTS} redirects") from error
    except httpx2.UnsupportedProtocol as error:
        raise FetchError(f"redirected to an unsupported scheme: {error}") from error
    except httpx2.TimeoutException as error:
        raise FetchError(f"timed out ({type(error).__name__})") from error
    except httpx2.HTTPError as error:
        raise FetchError(str(error) or type(error).__name__) from error
    except httpx2.InvalidURL as error:  # not an HTTPError: e.g. a host name that isn't valid IDNA
        raise FetchError(f"not a valid URL: {error}") from error
    syntax = sniff_syntax(sample)
    if syntax is None and content_type in {"text/turtle", "application/n-triples"}:
        syntax = Syntax.TURTLE
    if syntax not in STORED_SUFFIXES:
        raise FetchError(f"not in a syntax browl reads (RDF/XML, Turtle, OWL Functional Syntax): {syntax or 'unknown'}")
    return syntax


def catalog_names(iri: str) -> list[str]:
    """Return the names an import may be requested by, for the catalog: the IRI, and other spellings of it.

    The OWLAPI writes a file IRI as `file:/path`, Jena as `file:///path`; a relative import in RDF, resolved against
    the document's own file IRI, can come out either way.
    """
    if not iri.startswith("file:"):
        return [iri]
    path = re.sub(r"^file:(//(localhost)?)?", "", iri)
    return list(dict.fromkeys([iri, f"file:{path}", f"file://{path}", f"file://localhost{path}"]))


def write_catalog(path: Path, entries: dict[str, Path]) -> None:
    """Write an XML catalog that maps each import IRI to a file, as ROBOT's `--catalog` reads it."""
    catalog = ET.Element("catalog", xmlns="urn:oasis:names:tc:entity:xmlns:xml:catalog", prefer="public")
    for iri, file in entries.items():
        for name in catalog_names(iri):
            ET.SubElement(catalog, "uri", name=name, uri=file.resolve().as_uri())
    ET.ElementTree(catalog).write(path, encoding="utf-8", xml_declaration=True)


def empty_ontology(iri: str) -> str:
    """Return an empty ontology with the IRI, in OWL Functional Syntax, which a dropped import is mapped to."""
    return f"Ontology(<{iri}>)\n" if re.fullmatch(_IRI, iri) else "Ontology()\n"


@dataclass
class _Document:
    path: Path
    syntax: Syntax
    iri: str | None  # None for the upload


async def list_imports(document: _Document, settings: Settings) -> list[str]:
    """Return the IRIs a document imports; an OFN document is converted to Turtle, which then stands in for it.

    Raises:
        StageError: It couldn't be parsed.
    """
    if document.syntax is not Syntax.OFN:
        return await list_rdf_imports(document.path, document.syntax, settings)
    converted = document.path.with_name(CONVERTED) if document.iri is None else document.path.with_suffix(".ttl")
    found = await convert_ofn(document.path, converted, settings)
    document.path, document.syntax = converted, Syntax.TURTLE
    return found


async def imports_stage(context: JobContext, allowed: AddressCheck | None = None) -> None:
    """Fetch the upload's imports, recursively, and write the catalog that maps every one of them to a local file.

    Sets the context's `catalog`, `imports` and `import_files`, and `converted` for an OFN upload. An import that can't
    be fetched or read is dropped, and listed as such.

    Args:
        context: The job.
        allowed: Whether an address may be fetched from; `is_public` if not given.

    Raises:
        StageError: The upload couldn't be parsed.
    """
    if context.upload is None:
        return
    settings = context.settings
    directory = context.upload.parent / IMPORTS_DIR
    await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)
    await asyncio.to_thread(directory.mkdir)
    syntax = next(syntax for syntax, suffix in STORED_SUFFIXES.items() if suffix == context.upload.suffix)
    upload = _Document(context.upload, syntax, None)
    queue = deque([upload])
    results: dict[str, ImportResult] = {}
    files: dict[str, _Document] = {}
    attempts = 0
    budget = settings.max_upload_bytes  # left for all imports together
    deadline = asyncio.get_running_loop().time() + FETCH_ALL_TIMEOUT_S
    async with fetch_client(allowed) as client:
        while queue:
            document = queue.popleft()
            try:
                found = await list_imports(document, settings)
            except StageError as error:
                if document.iri is None:
                    raise
                results[document.iri] = ImportResult(document.iri, results[document.iri].imported_by, str(error))
                del files[document.iri]
                await asyncio.to_thread(document.path.unlink, missing_ok=True)
                continue
            for iri in found:
                if iri in results:
                    continue
                if len(results) >= MAX_LISTED:
                    raise StageError(f"the ontology and its imports list more than {MAX_LISTED} imports")
                remaining_s = deadline - asyncio.get_running_loop().time()
                if attempts >= MAX_IMPORTS or remaining_s <= 0 or budget <= 0:
                    reason = (
                        f"more than {MAX_IMPORTS} imports"
                        if attempts >= MAX_IMPORTS
                        else f"fetching all imports took longer than {FETCH_ALL_TIMEOUT_S:g} s"
                        if remaining_s <= 0
                        else "the imports fetched already add up to MAX_UPLOAD_BYTES"
                    )
                    results[iri] = ImportResult(iri, document.iri, reason)
                    continue
                attempts += 1
                path = directory / str(len(results) + 1)
                logger.info("Fetching import %s", iri)
                try:
                    fetched = await fetch(client, iri, path, settings, budget, min(FETCH_TIMEOUT_S, remaining_s))
                except FetchError as error:
                    logger.warning("Import %s dropped: %s", iri, error)
                    results[iri] = ImportResult(iri, document.iri, str(error))
                    continue
                stored = await asyncio.to_thread(path.rename, path.with_suffix(STORED_SUFFIXES[fetched]))
                budget -= (await asyncio.to_thread(stored.stat)).st_size
                results[iri] = ImportResult(iri, document.iri)
                files[iri] = _Document(stored, fetched, iri)
                queue.append(files[iri])
    catalog = context.upload.with_name(CATALOG)
    entries = await asyncio.to_thread(_catalog_entries, directory, results, files)
    await asyncio.to_thread(write_catalog, catalog, entries)
    context.catalog = catalog
    context.converted = upload.path if upload.path != context.upload else None
    context.imports = list(results.values())
    context.import_files = {iri: document.path for iri, document in files.items()}


def _catalog_entries(directory: Path, results: dict[str, ImportResult], files: dict[str, _Document]) -> dict[str, Path]:
    """Map each fetched import to its file, and each dropped one to an empty ontology of its own."""
    entries = {iri: document.path for iri, document in files.items()}
    for number, iri in enumerate(iri for iri in results if iri not in files):
        entries[iri] = _write_empty(directory, iri, number)
    return entries


def _write_empty(directory: Path, iri: str, number: int) -> Path:
    path = directory / f"empty-{number}.ofn"
    path.write_text(empty_ontology(iri), encoding="utf-8")
    return path
