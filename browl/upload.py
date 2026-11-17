"""The admin upload (A-2, A-4): a multipart form streamed to the data volume, never held in memory whole.

Starlette's own form parser spools a file to a temporary file before the handler sees it, which for a multi-gigabyte
ontology would mean writing it twice; this parser writes each chunk to the data volume as it arrives. The job is
claimed only once the file's name has been checked and its first bytes found to look like the syntax the name says
(`check_syntax`), so a malformed request, or a mislabelled file, leaves the served ontology alone.
"""

import asyncio
import errno
import logging
import re
import shutil
from collections.abc import Callable
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import IO

from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect, Request

from browl.jobs import JobContext, JobRunner, Reasoner, StageError
from browl.pages import clear_page_cache

logger = logging.getLogger(__name__)

UPLOADS_DIR = "uploads"
"""The uploads' directory in `DATA_DIR`; each job's file goes in a subdirectory named for its generation."""

REASONER_FIELD = "reasoner"
FILE_FIELD = "ontology"

FORMATS = {".ttl": ".ttl", ".rdf": ".rdf", ".owl": ".rdf", ".ofn": ".ofn"}
"""The file name suffixes accepted (A-2), and the suffix each is stored with, which says its format: `.ttl` Turtle,
`.rdf` RDF/XML, `.ofn` OWL Functional Syntax. An `.owl` file is taken to be RDF/XML, as OBO ontologies' are."""

SYNTAXES = {".ttl": "Turtle", ".rdf": "RDF/XML", ".ofn": "OWL Functional Syntax"}
"""The syntax each stored suffix stands for."""

SNIFF_BYTES = 8192
"""How much of the start of the file `check_syntax` looks at, before the job is claimed."""

MAX_FIELD_BYTES = 64
"""The longest form field other than the file; a longer one is refused."""

WRITE_BYTES = 1 << 20
"""How much of the file to buffer before each write, to keep thread hand-offs few."""


class UploadError(StageError):
    """The request isn't a valid upload.

    Raised before the job is claimed, it leaves the job state alone; after, the job has failed in the uploading stage
    with it as its reason.
    """


class StorageError(StageError):
    """The file couldn't be written to the data volume, e.g. it's full; the job has failed in the uploading stage.

    Attributes:
        no_space: Whether the volume was full.
    """

    def __init__(self, error: OSError) -> None:
        """Describe the error for admins (A-6), without the path."""
        super().__init__(f"couldn't store the uploaded file: {error.strerror or error}")
        self.no_space = error.errno in {errno.ENOSPC, errno.EDQUOT}


def upload_path(data_dir: Path, generation: int, suffix: str) -> Path:
    """Return where a job's uploaded file is stored."""
    return data_dir / UPLOADS_DIR / str(generation) / f"ontology{suffix}"


def stored_suffix(filename: str) -> str:
    """Return the suffix a file is stored with, from its name.

    Raises:
        UploadError: The name's suffix isn't one of `FORMATS`.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in FORMATS:
        accepted = ", ".join(sorted(FORMATS))
        raise UploadError(f"unsupported file type {suffix or '(none)'!r}; accepted: {accepted}")
    return FORMATS[suffix]


_XML_ROOT = re.compile(r"<([A-Za-z_][\w.-]*:)?([A-Za-z_][\w.-]*)([^>]*)")
"""An XML start tag: its prefix, local name, and attributes so far (the tag may run past the sample)."""

_TURTLE_START = re.compile(r"(@prefix|@base|prefix\s|base\s|<[^\s<>\"{}|^`\\]*>)", re.IGNORECASE)
"""How a Turtle document can start, distinctively: a prefix or base directive, or an IRI."""

_OFN_START = re.compile(r"(Prefix|Ontology)\s*\(")
"""How an OWL Functional Syntax document starts: its grammar allows only prefix declarations before the ontology."""


def sniff_syntax(sample: bytes) -> str | None:
    """Return the syntax the start of a file looks like, or None if it isn't distinctive.

    The result is one of `SYNTAXES`' values, "OWL/XML", or "compressed". It's a heuristic, from the first token or XML
    element after any comments: a file that looks right can still fail to parse later in the job.
    """
    if sample.startswith((b"\x1f\x8b", b"PK\x03\x04", b"BZh", b"\xfd7zXZ")):  # gzip, zip, bzip2, xz
        return "compressed"
    text = sample.decode("utf-8", "replace").removeprefix("\ufeff")
    while (text := text.lstrip()).startswith("#"):  # Turtle comments; the OWLAPI allows them in OWL Functional Syntax
        text = text.partition("\n")[2]
    if _OFN_START.match(text):
        return SYNTAXES[".ofn"]
    if _TURTLE_START.match(text):
        return SYNTAXES[".ttl"]
    if not text.startswith("<"):
        return None
    # XML: skip the declaration, comments, processing instructions and the doctype (its internal subset can be long)
    body = re.sub(r"<\?.*?\?>|<!--.*?-->|<!DOCTYPE(\[.*?\]|[^>\[])*>", "", text, flags=re.DOTALL)
    root = _XML_ROOT.search(body)
    if root is None:
        return SYNTAXES[".rdf"]  # still in the prologue: XML, so RDF/XML is the only accepted syntax it can be
    prefix, name, attributes = root.groups()
    if name == "Ontology" and (prefix is None or "ontologyIRI" in attributes):
        return "OWL/XML"
    return SYNTAXES[".rdf"]


def check_syntax(sample: bytes, suffix: str) -> None:
    """Check that the start of a file looks like the syntax its stored suffix says.

    A file whose start isn't distinctive passes, except as RDF/XML or OWL Functional Syntax, which must start with an
    XML tag or a prefix or ontology declaration.

    Raises:
        UploadError: The file looks like another syntax, or is compressed.
    """
    expected = SYNTAXES[suffix]
    found = sniff_syntax(sample)
    if found == expected or (found is None and suffix == ".ttl"):
        return
    if found == "compressed":
        raise UploadError("the file looks compressed; upload it uncompressed")
    if found == "OWL/XML":
        raise UploadError(
            "the file looks like OWL/XML, which isn't supported; convert it to RDF/XML, Turtle or OWL Functional"
            " Syntax, e.g. with ROBOT convert"
        )
    looks = "isn't recognizable" if found is None else f"looks like {found}"
    raise UploadError(f"the file's name says {expected}, but its content {looks}")


class _Events:
    """The parser's callbacks, recorded as events for the async handler to act on after each chunk is fed.

    An event is its kind and its bytes: a part's data, or for "headers", the part's `Content-Disposition`.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, bytes]] = []
        self._field = b""
        self._value = b""
        self.headers: dict[bytes, bytes] = {}

    def callbacks(self) -> dict[str, Callable[..., None]]:
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value,
            "on_header_end": self._header_end,
            "on_headers_finished": lambda: self.events.append(
                ("headers", self.headers.get(b"content-disposition", b""))
            ),
            "on_part_data": lambda data, start, end: self.events.append(("data", bytes(data[start:end]))),
            "on_part_end": lambda: self.events.append(("part_end", b"")),
            "on_end": lambda: self.events.append(("end", b"")),
        }

    def _part_begin(self) -> None:
        self.headers = {}

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._field += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def _header_end(self) -> None:
        self.headers[self._field.lower()] = self._value
        self._field, self._value = b"", b""


class _Upload:
    """One upload request's handling: the fields seen so far, and the file being written."""

    def __init__(self, request: Request, runner: JobRunner, stack: AsyncExitStack) -> None:
        self.request = request
        self.runner = runner
        self.stack = stack
        self.data_dir: Path = request.app.state.settings.data_dir
        self.reasoner: Reasoner | None = None
        self.suffix: str | None = None  # the file's stored suffix, once its part has started
        self.context: JobContext | None = None
        self.file: IO[bytes] | None = None
        self.field: str | None = None  # the current part's field name
        self.value = b""  # the current non-file part's value
        self.buffer = bytearray()
        self.size = 0
        self.ended = False

    async def headers(self, disposition: bytes) -> None:
        _, options = parse_options_header(disposition)
        self.field = options.get(b"name", b"").decode("utf-8", "replace")
        filename = options.get(b"filename")
        if self.field != FILE_FIELD:
            return
        if self.suffix is not None:
            raise UploadError("the form has more than one file")
        if filename is None or not filename:
            raise UploadError("no file was chosen")
        suffix = stored_suffix(filename.decode("utf-8", "replace"))
        if self.reasoner is None:
            raise UploadError(f"the form's {REASONER_FIELD!r} field must come before its file")
        self.suffix = suffix

    async def start(self) -> None:
        """Check the file's start, claim the job, clear the page cache, and open the file, removing earlier uploads."""
        assert self.reasoner is not None and self.suffix is not None
        if not self.buffer:
            raise UploadError("the file is empty")
        check_syntax(bytes(self.buffer[:SNIFF_BYTES]), self.suffix)
        suffix = self.suffix
        self.context = await self.stack.enter_async_context(self.runner.upload(self.reasoner))
        clear_page_cache()
        path = upload_path(self.data_dir, self.context.generation, suffix)
        self.file = await asyncio.to_thread(_open, path)
        self.stack.callback(self.file.close)
        self.context.upload = path
        logger.info("Load job %d: storing the upload at %s", self.context.generation, path)

    async def data(self, data: bytes) -> None:
        if self.field == FILE_FIELD:
            self.buffer += data
            self.size += len(data)
            if self.context is None and len(self.buffer) >= SNIFF_BYTES:
                await self.start()
            if len(self.buffer) >= WRITE_BYTES:
                await self.flush()
        elif self.field == REASONER_FIELD:
            self.value += data
            if len(self.value) > MAX_FIELD_BYTES:
                raise UploadError(f"the {REASONER_FIELD!r} field is too long")

    async def part_end(self) -> None:
        if self.field == FILE_FIELD:
            if self.context is None:
                await self.start()
            await self.flush()
        elif self.field == REASONER_FIELD:
            value = self.value.decode("utf-8", "replace")
            try:
                self.reasoner = Reasoner(value)
            except ValueError:
                raise UploadError(f"unknown reasoner {value!r}") from None
        self.field, self.value = None, b""

    async def flush(self) -> None:
        if self.buffer and self.file is not None:
            await asyncio.to_thread(self.file.write, bytes(self.buffer))
            self.buffer.clear()

    async def discard(self) -> None:
        """Close and remove the file, if it was opened, so a failed upload doesn't keep its disk space."""
        if self.context is not None and self.context.upload is not None:
            await asyncio.to_thread(_remove, self.context.upload.parent, self.file)

    async def finish(self) -> JobContext:
        if not self.ended:
            raise UploadError("the upload was cut short")
        if self.context is None or self.file is None:
            raise UploadError("no file was chosen")
        await asyncio.to_thread(self.file.close)
        logger.info("Load job %d: stored %d bytes", self.context.generation, self.size)
        return self.context


def _remove(path: Path, file: IO[bytes] | None) -> None:
    """Close a job's upload file, if open, ignoring a failed flush (e.g. the disk is full), and remove its directory."""
    if file is not None:
        with suppress(OSError):
            file.close()
    shutil.rmtree(path, ignore_errors=True)


def _open(path: Path) -> IO[bytes]:
    """Remove every earlier upload, which nothing needs once its job has ended, and open a new file for this one."""
    uploads = path.parent.parent
    if uploads.is_dir():
        for entry in uploads.iterdir():
            if entry != path.parent:
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("wb")


async def receive_upload(request: Request, runner: JobRunner) -> JobContext:
    """Stream the form's file to the data volume within a new load job's uploading stage, and return the job's context.

    The form has a `reasoner` field (a `Reasoner` value) and then an `ontology` file. On leaving, the job's other stages
    run in the background.

    Raises:
        UploadError: The request isn't a valid upload. If it was found invalid after the job was claimed (e.g. it was
            cut short), the job has failed in the uploading stage with this as its reason.
        StorageError: The file couldn't be written; the job has failed in the uploading stage with this as its reason.
        JobRunningError: A job is already running.

    A job that fails here leaves no partial file behind.
    """
    content_type, options = parse_options_header(request.headers.get("content-type"))
    boundary = options.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise UploadError("the request isn't a multipart form")
    events = _Events()
    parser = MultipartParser(boundary, events.callbacks())  # type: ignore[arg-type]
    async with AsyncExitStack() as stack:
        upload = _Upload(request, runner, stack)
        try:
            async for chunk in request.stream():
                parser.write(chunk)
                await _handle(upload, events)
            parser.finalize()
            await _handle(upload, events)
            return await upload.finish()
        except BaseException as error:
            await upload.discard()
            if isinstance(error, ClientDisconnect):
                raise UploadError("the upload was cut short: the client disconnected") from error
            if isinstance(error, OSError):
                raise StorageError(error) from error
            if isinstance(error, ValueError):
                # python-multipart's errors (e.g. MultipartParseError) are ValueErrors
                raise UploadError(f"the form is malformed: {error}") from error
            raise
    raise AssertionError("unreachable")  # pragma: no cover  # an exit stack doesn't swallow exceptions


async def _handle(upload: _Upload, events: _Events) -> None:
    pending, events.events = events.events, []
    for kind, data in pending:
        if kind == "headers":
            await upload.headers(data)
        elif kind == "data":
            await upload.data(data)
        elif kind == "part_end":
            await upload.part_end()
        else:
            upload.ended = True
