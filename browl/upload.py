"""The admin upload (A-2, A-4): a multipart form streamed to the data volume, never held in memory whole.

Starlette's own form parser spools a file to a temporary file before the handler sees it, which for a multi-gigabyte
ontology would mean writing it twice; this parser writes each chunk to the data volume as it arrives. The job is
claimed only once the file's part has started and its name has been checked, so a malformed request leaves the served
ontology alone.
"""

import asyncio
import logging
import shutil
from collections.abc import Callable
from contextlib import AsyncExitStack
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

MAX_FIELD_BYTES = 64
"""The longest form field other than the file; a longer one is refused."""

WRITE_BYTES = 1 << 20
"""How much of the file to buffer before each write, to keep thread hand-offs few."""


class UploadError(StageError):
    """The request isn't a valid upload. Raised before the job is claimed, it leaves the job state alone."""


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
        if self.context is not None:
            raise UploadError("the form has more than one file")
        if filename is None or not filename:
            raise UploadError("no file was chosen")
        suffix = stored_suffix(filename.decode("utf-8", "replace"))
        if self.reasoner is None:
            raise UploadError(f"the form's {REASONER_FIELD!r} field must come before its file")
        await self.start(suffix)

    async def start(self, suffix: str) -> None:
        """Claim the job, clear the page cache, and open the file, removing earlier jobs' uploads."""
        assert self.reasoner is not None
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
            if len(self.buffer) >= WRITE_BYTES:
                await self.flush()
        elif self.field == REASONER_FIELD:
            self.value += data
            if len(self.value) > MAX_FIELD_BYTES:
                raise UploadError(f"the {REASONER_FIELD!r} field is too long")

    async def part_end(self) -> None:
        if self.field == FILE_FIELD:
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

    async def finish(self) -> JobContext:
        if not self.ended:
            raise UploadError("the upload was cut short")
        if self.context is None or self.file is None:
            raise UploadError("no file was chosen")
        if self.size == 0:
            raise UploadError("the file is empty")
        await asyncio.to_thread(self.file.close)
        logger.info("Load job %d: stored %d bytes", self.context.generation, self.size)
        return self.context


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
        JobRunningError: A job is already running.
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
        except ClientDisconnect as error:
            raise UploadError("the upload was cut short: the client disconnected") from error
        except UploadError:
            raise
        except ValueError as error:  # python-multipart's errors (e.g. MultipartParseError) are ValueErrors
            raise UploadError(f"the form is malformed: {error}") from error
        return await upload.finish()
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
