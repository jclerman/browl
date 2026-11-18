"""The imports stage (DESIGN.md §2.4): fetch the ontology's imports, recursively, and write ROBOT's catalog for them.

Neither the TDB2 loader nor browl's queries follow `owl:imports`, and ROBOT would follow them itself, reading local
files (`file:`) or sending requests from the app container. So browl lists every import, fetches only `http` and
`https` IRIs from public addresses (checked on each connection, so on each redirect too), within a size limit, and
writes a catalog mapping every import to its fetched file, or to an empty ontology if it was dropped. ROBOT then never
resolves an import itself. A dropped import is a warning, not a failure (A-4).

Imports are listed from the whole file, not just its header, since a crafted file can declare them anywhere: RDF
documents through Jena's `riot`, which streams them as N-Triples; OWL Functional Syntax by scanning for `Import(…)`.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import httpcore2
import httpx2

import browl
from browl.jobs import ImportResult, JobContext, StageError
from browl.process import free_bytes, run_process
from browl.robot import convert_args, robot_to_file
from browl.settings import Settings
from browl.upload import FREE_SPACE_MARGIN_BYTES, IDLE_S, SNIFF_BYTES, STORED_SUFFIXES, Syntax, sniff_syntax

logger = logging.getLogger(__name__)

OWL_IMPORTS = "http://www.w3.org/2002/07/owl#imports"

IMPORTS_DIR = "imports"
"""The fetched imports' directory, beside the upload."""

CATALOG = "catalog.xml"
"""The catalog's file name, beside the upload."""

MAX_IMPORTS = 100
"""The most imports fetched for one ontology, directly or not; any more are dropped."""

MAX_REDIRECTS = 10
CONNECT_TIMEOUT_S = 30.0
FETCH_TIMEOUT_S = 3600.0
"""The longest one import may take to fetch."""

SCAN_TIMEOUT_S = 4 * 3600.0
"""The longest listing one document's imports may take: `riot` parses NCBITaxon (2 GB of RDF/XML) in about 25 s."""

TAIL_LINES = 10
"""How many of `riot`'s last lines a failure's reason quotes."""

ACCEPT = "text/turtle, application/rdf+xml;q=0.9, text/owl-functional;q=0.8, */*;q=0.1"

SYNTAX_NAMES = {Syntax.TURTLE: "Turtle", Syntax.RDF_XML: "RDF/XML"}
"""Jena's names for the RDF syntaxes `riot` reads."""

_IRI = r"[^\s<>\"{}|^`\\]*"
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_NT_IMPORT = re.compile(rf"^(?:<{_IRI}>|_:\S+) <{re.escape(OWL_IMPORTS)}> <({_IRI})> \.$")
_OFN_PREFIX = re.compile(rf"Prefix\s*\(\s*([A-Za-z_][\w.-]*)?:\s*=\s*<({_IRI})>\s*\)")
_OFN_IMPORT = re.compile(rf"Import\s*\(\s*(?:<({_IRI})>|([A-Za-z_][\w.-]*)?:([^\s()]*))\s*\)")
_OFN_HEADER_TOKEN = re.compile(rf"\s+|#[^\n]*|Prefix\s*\([^)]*\)|Ontology\s*\(|<{_IRI}>|Import\s*\([^)]*\)")
OFN_READ_CHARS = 1 << 20
"""How much of an OWL Functional Syntax document is scanned for imports at a time."""

OFN_OVERLAP_CHARS = 65536
"""How much of each chunk is kept for the next, so an `Import(…)` split between them is still found."""
_OFN_HEADER_CHARS = 16 * 2**20  # the longest header read for its prefixes and imports


@dataclass
class Imports:
    """What a document declares.

    Attributes:
        imports: The IRIs it imports, in the order found, each once.
        mentions: Every IRI that could be read as an import, including the imports: all must be in the catalog.
    """

    imports: list[str] = field(default_factory=list)
    mentions: set[str] = field(default_factory=set)

    def add(self, iri: str, imported: bool = True) -> None:
        """Add an IRI found, as an import, or only as a mention."""
        if imported and iri not in self.imports:
            self.imports.append(iri)
        self.mentions.add(iri)


class _ImportLines:
    """Collect the `owl:imports` triples from N-Triples arriving in chunks, splitting only their lines out."""

    _NEEDLE = f"<{OWL_IMPORTS}>".encode()

    def __init__(self, found: Imports) -> None:
        self._found = found
        self._rest = b""

    def __call__(self, chunk: bytes) -> None:
        data = self._rest + chunk
        end = data.rfind(b"\n") + 1
        self._rest = data[end:]
        start = data.find(self._NEEDLE, 0, end)
        while start >= 0:
            line_start = data.rfind(b"\n", 0, start) + 1
            line_end = data.find(b"\n", start)
            if match := _NT_IMPORT.match(data[line_start:line_end].decode("utf-8", "replace")):
                self._found.add(match[1])
            start = data.find(self._NEEDLE, line_end, end)


async def list_rdf_imports(path: Path, syntax: Syntax, settings: Settings) -> Imports:
    """Return the IRIs an RDF document imports, parsing all of it with Jena's `riot`.

    Raises:
        StageError: `riot` couldn't parse it.
    """
    found = Imports()
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
    return found


def list_ofn_imports(path: Path) -> Imports:
    """Return the IRIs an OWL Functional Syntax document imports.

    Its imports are the `Import(…)`s in its header, which its grammar puts before any annotation or axiom. Any
    `Import(…)` later in the file, even inside a literal, is also listed as a mention, for the catalog, in case a
    parser accepted it there.
    """
    found = Imports()
    with path.open(encoding="utf-8", errors="replace") as file:
        prefixes = _ofn_header(file, found)
        file.seek(0)
        rest = ""
        while chunk := file.read(OFN_READ_CHARS):
            text = rest + chunk
            cut = max(0, len(text) - OFN_OVERLAP_CHARS)
            last = 0
            for match in _OFN_IMPORT.finditer(text):
                if match.start() >= cut:
                    break
                _ofn_mention(match, prefixes, found)
                last = match.end()
            rest = text[max(cut, last) :]
        for match in _OFN_IMPORT.finditer(rest):
            _ofn_mention(match, prefixes, found)
    return found


def _ofn_header(file: IO[str], found: Imports) -> dict[str, str]:
    """Add the header's imports, and return its prefixes."""
    text = file.read(_OFN_HEADER_CHARS).removeprefix("﻿")
    prefixes = {match[1] or "": match[2] for match in _OFN_PREFIX.finditer(text)}
    position = 0
    while token := _OFN_HEADER_TOKEN.match(text, position):
        match = _OFN_IMPORT.fullmatch(token[0])
        if match and (iri := _ofn_iri(match, prefixes)) is not None:
            found.add(iri)
        position = token.end()
    return prefixes


def _ofn_iri(match: re.Match[str], prefixes: dict[str, str]) -> str | None:
    if match[1] is not None:
        return match[1]
    namespace = prefixes.get(match[2] or "")
    return None if namespace is None else namespace + match[3]


def _ofn_mention(match: re.Match[str], prefixes: dict[str, str], found: Imports) -> None:
    if (iri := _ofn_iri(match, prefixes)) is not None:
        found.add(iri, imported=False)


async def list_imports(path: Path, syntax: Syntax, settings: Settings) -> Imports:
    """Return the IRIs a document imports.

    Raises:
        StageError: It couldn't be parsed.
    """
    if syntax == Syntax.OFN:
        return await asyncio.to_thread(list_ofn_imports, path)
    return await list_rdf_imports(path, syntax, settings)


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


async def fetch(client: httpx2.AsyncClient, iri: str, path: Path, settings: Settings) -> Syntax:
    """Fetch an import into a file, returning its syntax; the file is removed if the fetch fails.

    Raises:
        FetchError: It isn't an `http` or `https` IRI, its host isn't public, the request failed, it's larger than
            `MAX_UPLOAD_BYTES`, the data volume is low on space, or it isn't in a syntax browl reads.
    """
    if not iri.lower().startswith(("http://", "https://")):
        raise FetchError("only http and https imports are fetched")
    try:
        async with asyncio.timeout(FETCH_TIMEOUT_S):
            return await _fetch(client, iri, path, settings)
    except TimeoutError as error:
        path.unlink(missing_ok=True)
        raise FetchError(f"fetching took longer than {FETCH_TIMEOUT_S:g} s") from error
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def _fetch(client: httpx2.AsyncClient, iri: str, path: Path, settings: Settings) -> Syntax:
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
                    if size > settings.max_upload_bytes:
                        raise FetchError(f"larger than {settings.max_upload_bytes:,} bytes (MAX_UPLOAD_BYTES)")
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
    syntax = sniff_syntax(sample)
    if syntax is None and content_type in {"text/turtle", "application/n-triples"}:
        syntax = Syntax.TURTLE
    if syntax not in STORED_SUFFIXES:
        raise FetchError(f"not in a syntax browl reads (RDF/XML, Turtle, OWL Functional Syntax): {syntax or 'unknown'}")
    return syntax


def catalog_names(iri: str) -> list[str]:
    """Return the names an import may be requested by, for the catalog: the IRI, and other spellings of it.

    The OWLAPI writes a file IRI as `file:/path`, Jena as `file:///path`; a relative import in RDF, resolved against
    the document's own file IRI, can come out either way. The OWLAPI reads a relative import in OWL Functional Syntax
    as `urn:absolute:` followed by it.
    """
    if not _SCHEME.match(iri):
        return [iri, f"urn:absolute:{iri}"]
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


async def imports_stage(context: JobContext, allowed: AddressCheck | None = None) -> None:
    """Fetch the upload's imports, recursively, and write the catalog that maps every one of them to a local file.

    Sets the context's `catalog`, `imports` and `import_files`. An import that can't be fetched or read is dropped,
    and listed as such; one in OWL Functional Syntax is converted to Turtle, for the TDB2 loader.

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
    queue = deque([_Document(context.upload, syntax, None)])
    results: dict[str, ImportResult] = {}
    files: dict[str, _Document] = {}
    mentions: set[str] = set()
    async with fetch_client(allowed) as client:
        while queue:
            document = queue.popleft()
            try:
                found = await list_imports(document.path, document.syntax, settings)
            except StageError as error:
                if document.iri is None:
                    raise
                results[document.iri] = ImportResult(document.iri, results[document.iri].imported_by, str(error))
                del files[document.iri]
                continue
            mentions |= found.mentions
            for iri in found.imports:
                if iri in results:
                    continue
                if len(files) >= MAX_IMPORTS:
                    results[iri] = ImportResult(iri, document.iri, f"more than {MAX_IMPORTS} imports")
                    continue
                path = directory / str(len(results) + 1)
                logger.info("Fetching import %s", iri)
                try:
                    fetched = await fetch(client, iri, path, settings)
                except FetchError as error:
                    logger.warning("Import %s dropped: %s", iri, error)
                    results[iri] = ImportResult(iri, document.iri, str(error))
                    continue
                stored = await asyncio.to_thread(path.rename, path.with_suffix(STORED_SUFFIXES[fetched]))
                results[iri] = ImportResult(iri, document.iri)
                files[iri] = _Document(stored, fetched, iri)
                queue.append(files[iri])
    catalog = context.upload.with_name(CATALOG)
    entries = await asyncio.to_thread(_catalog_entries, directory, mentions, files)
    await asyncio.to_thread(write_catalog, catalog, entries)
    context.catalog = catalog
    for iri, document in list(files.items()):
        if document.syntax == Syntax.OFN:
            converted = document.path.with_suffix(".ttl")
            try:
                await robot_to_file(
                    convert_args(document.path, converted.with_suffix(".partial.ttl"), catalog),
                    converted.with_suffix(".partial.ttl"),
                    converted,
                    settings,
                )
            except StageError as error:
                results[iri] = ImportResult(iri, results[iri].imported_by, f"couldn't convert it: {error}")
                del files[iri]
                entries[iri] = await asyncio.to_thread(_write_empty, directory, iri, len(entries))
                await asyncio.to_thread(write_catalog, catalog, entries)
                continue
            files[iri] = _Document(converted, Syntax.TURTLE, iri)
    context.imports = list(results.values())
    context.import_files = {iri: document.path for iri, document in files.items()}


def _catalog_entries(directory: Path, mentions: set[str], files: dict[str, _Document]) -> dict[str, Path]:
    """Map each fetched import to its file, and every other IRI that could be read as an import to an empty ontology."""
    entries = {iri: document.path for iri, document in files.items()}
    for number, iri in enumerate(sorted(mentions - entries.keys())):
        entries[iri] = _write_empty(directory, iri, number)
    return entries


def _write_empty(directory: Path, iri: str, number: int) -> Path:
    path = directory / f"empty-{number}.ofn"
    path.write_text(empty_ontology(iri), encoding="utf-8")
    return path
