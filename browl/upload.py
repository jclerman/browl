"""The admin upload (A-2, A-4): a multipart form streamed to the jobs volume, never held in memory whole.

Starlette's own form parser spools a file to a temporary file before the handler sees it, which for a multi-gigabyte
ontology would mean writing it twice; this parser writes each chunk to the jobs volume as it arrives. The job is
claimed only once the file's name has been checked and its first bytes found to look like the syntax the name says
(`check_syntax`), so a malformed request, or a mislabelled file, leaves the served ontology alone.
"""

import asyncio
import errno
import logging
import re
import shutil
from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack, aclosing, suppress
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import IO, Any

from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect, Request

from browl.jobs import JobContext, JobRunner, Reasoner, StageError
from browl.pages import clear_page_cache
from browl.settings import Settings

logger = logging.getLogger(__name__)

UPLOADS_DIR = "uploads"
"""The uploads' directory in `DATA_DIR`; each job's file goes in a subdirectory named for its generation."""

REASONER_FIELD = "reasoner"
FILE_FIELD = "ontology"


class Syntax(StrEnum):
    """What `sniff_syntax` can find a file to be; the first three are the syntaxes accepted (A-2)."""

    TURTLE = "Turtle"
    RDF_XML = "RDF/XML"
    OFN = "OWL Functional Syntax"
    OWL_XML = "OWL/XML"
    COMPRESSED = "compressed"


FORMATS = {".ttl": Syntax.TURTLE, ".rdf": Syntax.RDF_XML, ".owl": Syntax.RDF_XML, ".ofn": Syntax.OFN}
"""The file name suffixes accepted, and the syntax each says; an `.owl` file is taken to be RDF/XML, as OBO's are."""

STORED_SUFFIXES = {Syntax.TURTLE: ".ttl", Syntax.RDF_XML: ".rdf", Syntax.OFN: ".ofn"}
"""The suffix a file in each accepted syntax is stored with, which tells the later stages its syntax."""

SNIFF_BYTES = 8192
"""How much of the start of the file `check_syntax` looks at, before the job is claimed."""

MAX_NAME_CHARS = 255
"""The longest file name recorded with the job; a longer one is cut short."""

MAX_FIELD_BYTES = 64
"""The longest form field other than the file; a longer one is refused."""

WRITE_BYTES = 1 << 20
"""How much of the file to buffer before each write, to keep thread hand-offs few."""

FREE_SPACE_MARGIN_BYTES = 10**9
"""Space an upload must leave free on the jobs volume, for the job's other files (`check_space`)."""

IDLE_S = 60.0
"""How long the upload may go without receiving data before it fails, e.g. when the admin's laptop sleeps."""


class UploadError(StageError):
    """The request isn't a valid upload.

    Raised before the job is claimed, it leaves the job state alone; after, the job has failed in the uploading stage
    with it as its reason.

    Attributes:
        status_code: The HTTP status to refuse the upload with.
    """

    def __init__(self, message: str, status_code: int = 400) -> None:
        """Make the error, with its status."""
        super().__init__(message)
        self.status_code = status_code


class StorageError(StageError):
    """The file couldn't be written to the jobs volume, e.g. it's full; the job has failed in the uploading stage.

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


def file_syntax(filename: str) -> Syntax:
    """Return the syntax a file's name says it's in.

    Raises:
        UploadError: The name's suffix isn't one of `FORMATS`.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in FORMATS:
        accepted = ", ".join(sorted(FORMATS))
        raise UploadError(f"unsupported file type {suffix or '(none)'!r}; accepted: {accepted}")
    return FORMATS[suffix]


def _check_limit(size: int, max_bytes: int) -> None:
    if size > max_bytes:
        raise UploadError(f"the upload is larger than the limit of {max_bytes:,} bytes (MAX_UPLOAD_BYTES)", 413)


async def check_space(size: int, max_bytes: int, data_dir: Path) -> None:
    """Check that an upload of `size` bytes is within the limit, and leaves `FREE_SPACE_MARGIN_BYTES` free.

    The space is that of the uploads directory, the jobs volume in the Compose deployment; of `data_dir` if the
    directory doesn't exist yet.

    Raises:
        UploadError: It isn't, with status 413 or 507.
    """
    _check_limit(size, max_bytes)
    uploads = data_dir / UPLOADS_DIR
    path = uploads if await asyncio.to_thread(uploads.is_dir) else data_dir
    free = (await asyncio.to_thread(shutil.disk_usage, path)).free
    if size > free - FREE_SPACE_MARGIN_BYTES:
        raise UploadError(f"the jobs volume has too little free space: {free:,} bytes for a {size:,}-byte upload", 507)


_XML_ROOT = re.compile(r"<([A-Za-z_][\w.-]*:)?([A-Za-z_][\w.-]*)([^>]*)")
"""An XML start tag: its prefix, local name, and attributes so far (the tag may run past the sample)."""

_TURTLE_START = re.compile(r"(@prefix|@base|prefix\s|base\s|<[^\s<>\"{}|^`\\]*>)", re.IGNORECASE)
"""How a Turtle document can start, distinctively: a prefix or base directive, or an IRI."""

_OFN_START = re.compile(r"(Prefix|Ontology)\s*\(")
"""How an OWL Functional Syntax document starts: its grammar allows only prefix declarations before the ontology."""


def _skip_xml_prologue(text: str) -> str:
    """Return the text after an XML prologue: the declaration, comments, processing instructions and the doctype.

    It's scanned with `str.find`, not a regex, so its time grows linearly with the text, whatever an upload holds.
    Returns "" if the prologue runs past the text, as a long doctype's internal subset can.
    """
    while True:
        text = text.lstrip()
        if text.startswith("<?"):
            end = _end_of(text, "?>")
        elif text.startswith("<!--"):
            end = _end_of(text, "-->")
        elif text.startswith("<!DOCTYPE"):
            subset = text.find("[")
            if subset >= 0 and not 0 <= text.find(">") < subset:  # an internal subset, which can hold ">"
                subset_end = text.find("]", subset)
                end = -1 if subset_end < 0 else _end_of(text, ">", subset_end)
            else:
                end = _end_of(text, ">")
        else:
            return text
        if end < 0:
            return ""
        text = text[end:]


def _end_of(text: str, marker: str, start: int = 0) -> int:
    """Return the index just past the first `marker` at or after `start`, or -1 if there's none."""
    index = text.find(marker, start)
    return -1 if index < 0 else index + len(marker)


def sniff_syntax(sample: bytes) -> Syntax | None:
    """Return the syntax the start of a file looks like, or None if it isn't distinctive.

    It's a heuristic, from the first token or XML element after any comments: a file that looks right can still fail
    to parse later in the job.
    """
    if sample.startswith((b"\x1f\x8b", b"PK\x03\x04", b"BZh", b"\xfd7zXZ")):  # gzip, zip, bzip2, xz
        return Syntax.COMPRESSED
    text = sample.decode("utf-8", "replace").removeprefix("\ufeff")
    while (text := text.lstrip()).startswith("#"):  # Turtle comments; the OWLAPI allows them in OWL Functional Syntax
        text = text.partition("\n")[2]
    if _OFN_START.match(text):
        return Syntax.OFN
    if _TURTLE_START.match(text):
        return Syntax.TURTLE
    if not text.startswith("<"):
        return None
    root = _XML_ROOT.match(_skip_xml_prologue(text))
    if root is None:
        return Syntax.RDF_XML  # still in the prologue: XML, so RDF/XML is the only accepted syntax it can be
    prefix, name, attributes = root.groups()
    if name == "Ontology" and (prefix is None or "ontologyIRI" in attributes):
        return Syntax.OWL_XML
    return Syntax.RDF_XML


def check_syntax(sample: bytes, expected: Syntax) -> None:
    """Check that the start of a file looks like the syntax its name says.

    A file whose start isn't distinctive passes, except as RDF/XML or OWL Functional Syntax, which must start with an
    XML tag or a prefix or ontology declaration.

    Raises:
        UploadError: The file looks like another syntax, or is compressed.
    """
    found = sniff_syntax(sample)
    if found == expected or (found is None and expected == Syntax.TURTLE):
        return
    if found == Syntax.COMPRESSED:
        raise UploadError("the file looks compressed; upload it uncompressed")
    if found == Syntax.OWL_XML:
        raise UploadError(
            "the file looks like OWL/XML, which isn't supported; convert it to RDF/XML, Turtle or OWL Functional"
            " Syntax, e.g. with ROBOT convert"
        )
    looks = "isn't recognizable" if found is None else f"looks like {found}"
    raise UploadError(f"the file's name says {expected}, but its content {looks}")


class _Events:
    """The parser's callbacks, recorded as events for `_Upload.handle` to act on after each chunk is fed.

    An event is its kind and its bytes: a part's data, or for "headers", the part's `Content-Disposition`.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, bytes]] = []
        self._field = b""
        self._value = b""
        self._disposition = b""

    def callbacks(self) -> dict[str, Callable[..., None]]:
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value,
            "on_header_end": self._header_end,
            "on_headers_finished": lambda: self.events.append(("headers", self._disposition)),
            "on_part_data": lambda data, start, end: self.events.append(("data", bytes(data[start:end]))),
            "on_part_end": lambda: self.events.append(("part_end", b"")),
            "on_end": lambda: self.events.append(("end", b"")),
        }

    def _part_begin(self) -> None:
        self._disposition = b""

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._field += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def _header_end(self) -> None:
        if self._field.lower() == b"content-disposition":
            self._disposition = self._value
        self._field, self._value = b"", b""


class _Upload:
    """One upload request's handling: the fields seen so far, and the file being written."""

    def __init__(self, request: Request, runner: JobRunner, stack: AsyncExitStack) -> None:
        self.request = request
        self.runner = runner
        self.stack = stack
        self.settings: Settings = request.app.state.settings
        self.reasoner: Reasoner | None = None
        self.syntax: Syntax | None = None  # the syntax the file's name says, once its part has started
        self.name: str | None = None  # the file's name, without any directory a browser sent
        self.context: JobContext | None = None
        self.file: IO[bytes] | None = None
        self.field: str | None = None  # the current part's field name
        self.value = b""  # the current non-file part's value
        self.buffer = bytearray()
        self.ended = False
        self._work: asyncio.Future[Any] | None = None  # the latest file work, which can outlive its cancelled await

    async def handle(self, events: _Events) -> None:
        """Act on the events the parser recorded for the latest chunk."""
        pending, events.events = events.events, []
        for kind, data in pending:
            match kind:
                case "headers":
                    self.headers(data)
                case "data":
                    await self.data(data)
                case "part_end":
                    await self.part_end()
                case "end":
                    self.ended = True

    def headers(self, disposition: bytes) -> None:
        _, options = parse_options_header(disposition)
        self.field = options.get(b"name", b"").decode("utf-8", "replace")
        if self.field == REASONER_FIELD and self.reasoner is not None:
            raise UploadError(f"the form has more than one {REASONER_FIELD!r} field")
        if self.field != FILE_FIELD:
            return
        if self.syntax is not None:
            raise UploadError("the form has more than one file")
        filename = options.get(b"filename")
        if not filename:
            raise UploadError("no file was chosen")
        name = PureWindowsPath(filename.decode("utf-8", "replace")).name  # also splits on "/"
        syntax = file_syntax(name)
        if self.reasoner is None:
            raise UploadError(f"the form's {REASONER_FIELD!r} field must come before its file")
        self.syntax = syntax
        self.name = "".join(char for char in name if char.isprintable())[:MAX_NAME_CHARS]

    async def start(self) -> None:
        """Check the file's start, claim the job, clear the page cache, and open the file, removing earlier uploads."""
        assert self.reasoner is not None and self.syntax is not None
        if not self.buffer:
            raise UploadError("the file is empty")
        _check_limit(len(self.buffer), self.settings.max_upload_bytes)
        check_syntax(bytes(self.buffer[:SNIFF_BYTES]), self.syntax)
        self.context = await self.stack.enter_async_context(self.runner.upload(self.reasoner, self.name, self.syntax))
        clear_page_cache()
        path = upload_path(self.settings.data_dir, self.context.generation, STORED_SUFFIXES[self.syntax])
        self.context.upload = path  # before opening, so `discard` removes whatever the open leaves
        self.file = await self._in_thread(_open, path)
        self.stack.callback(self.file.close)
        self.context.upload_bytes = len(self.buffer)
        logger.info("Load job %d: storing the upload at %s", self.context.generation, path)

    async def data(self, data: bytes) -> None:
        if self.field == FILE_FIELD:
            self.buffer += data
            if self.context is not None:
                self.context.upload_bytes = (self.context.upload_bytes or 0) + len(data)
                _check_limit(self.context.upload_bytes, self.settings.max_upload_bytes)
            elif len(self.buffer) >= SNIFF_BYTES:
                await self.start()
            if self.context is not None:
                await self.runner.record_upload_progress(self.context)
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
            buffer, self.buffer = self.buffer, bytearray()
            await self._in_thread(_write, self.file, buffer)

    async def finish(self) -> JobContext:
        if not self.ended:
            raise UploadError("the upload was cut short")
        if self.context is None or self.file is None:
            raise UploadError("no file was chosen")
        await self._in_thread(self.file.close)
        logger.info("Load job %d: stored %d bytes", self.context.generation, self.context.upload_bytes)
        return self.context

    async def _in_thread[**P, T](self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        """Run file work in a thread, reporting an `OSError` as a `StorageError`; `discard` waits for it to end."""
        work = self._work = asyncio.ensure_future(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(work)
        except OSError as error:
            raise StorageError(error) from error

    async def discard(self) -> None:
        """Remove the job's file and directory, if it got that far, so a failed upload doesn't keep its disk space.

        The removal runs to its end even if this is cancelled.
        """
        if self.context is not None and self.context.upload is not None:
            await asyncio.shield(asyncio.ensure_future(self._remove(self.context.upload.parent)))

    async def _remove(self, directory: Path) -> None:
        if (work := self._work) is not None:
            await asyncio.wait({work})  # a cancelled open or write still runs in its thread
            if self.file is None and not work.cancelled() and work.exception() is None:
                self.file = work.result()  # opened after the open's await was cancelled
        await asyncio.to_thread(_remove, directory, self.file)


def _write(file: IO[bytes], data: bytearray) -> None:
    """Write to the file and flush it, so a write smaller than the file's buffer (128 KiB in Python 3.14) isn't held."""
    file.write(data)
    file.flush()


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
    """Stream the form's file to the jobs volume within a new load job's uploading stage, and return the job's context.

    The form has a `reasoner` field (a `Reasoner` value) and then an `ontology` file. On leaving, the job's other stages
    run in the background.

    Raises:
        UploadError: The request isn't a valid upload, or is too large for the limit or the free space. If it was
            found invalid after the job was claimed (e.g. it was cut short), the job has failed in the uploading stage
            with this as its reason.
        StorageError: The file couldn't be written; the job has failed in the uploading stage with this as its reason.
        JobRunningError: A job is already running.

    A job that fails here leaves no partial file behind.
    """
    content_type, options = parse_options_header(request.headers.get("content-type"))
    boundary = options.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise UploadError("the request isn't a multipart form")
    settings: Settings = request.app.state.settings
    length = request.headers.get("content-length", "")
    if length.isdigit():  # browsers send it; a body without it is limited as it streams in
        await check_space(int(length), settings.max_upload_bytes, settings.data_dir)
    events = _Events()
    parser = MultipartParser(boundary, events.callbacks())  # type: ignore[arg-type]
    async with AsyncExitStack() as stack:
        upload = _Upload(request, runner, stack)
        try:
            async with aclosing(_chunks(request)) as chunks:
                async for chunk in chunks:
                    _parse(parser.write, chunk)
                    await upload.handle(events)
            _parse(parser.finalize)
            await upload.handle(events)
            context = await upload.finish()
        except BaseException:
            await upload.discard()
            raise
    return context


async def _chunks(request: Request) -> AsyncGenerator[bytes]:
    """Yield the request body's chunks, failing the upload if the client disconnects or sends nothing for `IDLE_S`."""
    async with aclosing(request.stream()) as stream:
        while True:
            timeout = asyncio.timeout(IDLE_S)
            try:
                async with timeout:
                    chunk = await anext(stream)
            except StopAsyncIteration:
                return
            except ClientDisconnect as error:
                raise UploadError("the upload was cut short: the client disconnected") from error
            except TimeoutError:
                if not timeout.expired():
                    raise
                raise UploadError(f"the upload stalled: no data arrived for {IDLE_S:g} s") from None
            yield chunk


def _parse[**P](function: Callable[P, object], *args: P.args, **kwargs: P.kwargs) -> None:
    """Feed the multipart parser, reporting its errors (`ValueError`s, e.g. `MultipartParseError`) as `UploadError`s."""
    try:
        function(*args, **kwargs)
    except ValueError as error:
        raise UploadError(f"the form is malformed: {error}") from error
