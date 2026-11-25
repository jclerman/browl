"""Tests for the upload page and the streaming upload (A-2, A-3, A-4)."""

import asyncio
import errno
import os
import shutil
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2.filters import do_filesizeformat
from markupsafe import escape
from starlette.requests import Request
from starlette.types import Message, Receive

import browl.jobs
import browl.upload
from browl.jobs import JobContext, JobRunner, Stage
from browl.jobtypes import Reasoner, StageError
from browl.upload import (
    SNIFF_BYTES,
    UPLOADS_DIR,
    Syntax,
    UploadError,
    check_job_file,
    check_space,
    check_syntax,
    receive_upload,
    sniff_syntax,
    upload_path,
)
from tests.helpers import make_app, make_settings

UPLOAD = "/_browl/admin/upload"
UPLOAD_WAIT = "A load is running; you can upload once it ends."
pytestmark = pytest.mark.anyio

TURTLE = b"@prefix ex: <http://example.org/> .\nex:a a ex:B .\n"
RDFXML = b'<?xml version="1.0"?>\n<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>\n'
OFN = b"Prefix(:=<http://example.org/>)\nOntology(<http://example.org/onto>)\n"
LONG = TURTLE * (SNIFF_BYTES // len(TURTLE) + 1)
"""A Turtle file longer than the part checked before the job is claimed, so the job is claimed while it streams."""
FORM = "multipart/form-data; boundary=b"
STREAM_WRITE_BYTES = 1 << 16
"""The write size `test_streamed` sets, small so a test file spans many writes."""
FILE_START = (
    b'--b\r\nContent-Disposition: form-data; name="reasoner"\r\n\r\nelk\r\n'
    b'--b\r\nContent-Disposition: form-data; name="ontology"; filename="o.ttl"\r\n\r\n'
)


@pytest.fixture
def app(data_dir: Path) -> FastAPI:
    """Return an app with default settings."""
    return make_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Yield a test client for the app, its lifespan running, with no job run yet."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def loading(app: FastAPI) -> threading.Event:
    """Register a loading stage that never ends; return an event set once it has started."""
    started = threading.Event()

    async def forever(context: JobContext) -> None:
        started.set()
        await asyncio.Event().wait()

    app.state.jobs.register(Stage.LOADING, forever)
    return started


def post(
    client: TestClient, filename: str = "onto.ttl", content: bytes = TURTLE, reasoner: str | None = "none"
) -> httpx2.Response:
    """Post the upload form, its reasoner field first as a browser sends it, without following the redirect."""
    data = {} if reasoner is None else {"reasoner": reasoner}
    return client.post(UPLOAD, data=data, files={"ontology": (filename, content)}, follow_redirects=False)


def finish(client: TestClient, app: FastAPI) -> None:
    """Wait for the running job, if any, to end."""
    assert client.portal is not None
    client.portal.call(app.state.jobs.wait)


def test_upload_page(client: TestClient) -> None:
    """Check that the page has the form, its reasoner choice first, defaulting to none, and the status, with htmx."""
    response = client.get(UPLOAD)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    text = response.text
    assert 'enctype="multipart/form-data"' in text
    assert f'action="{UPLOAD}"' in text
    assert text.index('name="reasoner"') < text.index('name="ontology"')
    assert '<input type="radio" name="reasoner" value="none" checked>' in text
    assert 'value="elk"' in text and 'value="hermit"' in text
    assert 'accept=".ofn,.owl,.rdf,.ttl"' in text
    assert 'id="job-status"' in text
    assert "/_browl/static/htmx.min.js" in text


@pytest.mark.parametrize(
    ("filename", "content", "suffix"),
    [
        ("onto.ttl", TURTLE, ".ttl"),
        ("onto.rdf", RDFXML, ".rdf"),
        ("uberon.OWL", RDFXML, ".rdf"),
        ("onto.ofn", OFN, ".ofn"),
        ("big.ttl", LONG, ".ttl"),
        ("commented.ofn", b"# a comment line\n" * 120 + OFN * 200, ".ofn"),  # the first 2 KB are comments
    ],
)
def test_upload(client: TestClient, app: FastAPI, data_dir: Path, filename: str, content: bytes, suffix: str) -> None:
    """Check that an upload is stored with the suffix for its format, and starts a job that reaches ready."""
    response = post(client, filename, content)
    assert response.status_code == 303
    assert response.headers["location"] == UPLOAD
    finish(client, app)
    state = app.state.jobs.state
    assert (state.generation, state.stage, state.reasoner) == (1, Stage.READY, Reasoner.NONE)
    assert upload_path(data_dir, 1, suffix).read_bytes() == content
    assert (state.upload_name, state.upload_bytes) == (filename, len(content))
    assert state.upload_syntax == {".ttl": "Turtle", ".rdf": "RDF/XML", ".ofn": "OWL Functional Syntax"}[suffix]


def test_upload_shown(client: TestClient, app: FastAPI) -> None:
    """Check that the status shows the file's name, without the directory a browser may send, syntax and size."""
    post(client, "C:\\fakepath\\uberon.owl", RDFXML * 20)
    finish(client, app)
    text = " ".join(client.get(UPLOAD).text.split())
    size = len(RDFXML) * 20
    assert (
        f'File <code>uberon.owl</code>, RDF/XML, <span title="{size:,} bytes">{do_filesizeformat(size)}</span>.' in text
    )


def test_upload_name_cleaned(client: TestClient, app: FastAPI) -> None:
    """Check that the name recorded loses control characters and is cut short."""
    filename = b"a\x07b" + b"c" * 300 + b".ttl"
    body = FILE_START.replace(b'filename="o.ttl"', b'filename="' + filename + b'"') + TURTLE + b"\r\n--b--\r\n"
    assert client.post(UPLOAD, content=body, headers={"Content-Type": FORM}, follow_redirects=False).status_code == 303
    finish(client, app)
    name = app.state.jobs.state.upload_name
    assert name == ("ab" + "c" * 300)[: browl.upload.MAX_NAME_CHARS]


@pytest.mark.parametrize("filename", [b"dir/onto.ttl", b"dir\\onto.ttl", b"C:\\fakepath\\onto.ttl"])
def test_upload_name_without_directory(client: TestClient, app: FastAPI, filename: bytes) -> None:
    """Check that the name recorded loses any directory the browser sent, with either kind of slash."""
    body = FILE_START.replace(b'filename="o.ttl"', b'filename="' + filename + b'"') + TURTLE + b"\r\n--b--\r\n"
    assert client.post(UPLOAD, content=body, headers={"Content-Type": FORM}, follow_redirects=False).status_code == 303
    finish(client, app)
    assert app.state.jobs.state.upload_name == "onto.ttl"


@pytest.mark.parametrize("reasoner", list(Reasoner))
def test_upload_reasoner(client: TestClient, app: FastAPI, reasoner: Reasoner) -> None:
    """Check that the reasoner chosen reaches the job's stages and its stored state (A-3)."""
    seen: list[tuple[Reasoner, Path | None]] = []

    async def reasoning(context: JobContext) -> None:
        seen.append((context.reasoner, context.upload))

    app.state.jobs.register(Stage.REASONING, reasoning)
    assert post(client, reasoner=reasoner).status_code == 303
    finish(client, app)
    assert seen == [(reasoner, upload_path(app.state.settings.data_dir, 1, ".ttl"))]
    assert app.state.jobs.state.reasoner == reasoner


def test_upload_replaces(client: TestClient, app: FastAPI, data_dir: Path) -> None:
    """Check that each upload starts a new generation and removes the previous upload (A-4)."""
    post(client)
    finish(client, app)
    post(client, "onto.ofn", b"Ontology()")
    finish(client, app)
    assert app.state.jobs.state.generation == 2
    assert [path.name for path in (data_dir / UPLOADS_DIR).iterdir()] == ["2"]
    assert upload_path(data_dir, 2, ".ofn").read_bytes() == b"Ontology()"


def test_upload_removes_symlinks(client: TestClient, app: FastAPI, data_dir: Path, tmp_path: Path) -> None:
    """Check that an upload removes a symlink left among the earlier jobs' files, not what it leads to."""
    (tmp_path / "kept").mkdir()
    (tmp_path / "kept" / "file").write_text("x")
    uploads = data_dir / UPLOADS_DIR
    uploads.mkdir()
    (uploads / "planted").symlink_to(tmp_path / "kept")
    post(client)
    finish(client, app)
    assert [path.name for path in uploads.iterdir()] == ["1"]
    assert (tmp_path / "kept" / "file").read_text() == "x"


def test_check_job_file(data_dir: Path, tmp_path: Path) -> None:
    """Check that only a regular file on the jobs volume passes, even through a symlink that stays on it."""
    job = data_dir / UPLOADS_DIR / "1"
    job.mkdir(parents=True)
    (job / "ontology.ttl").write_text("x")
    (tmp_path / "secret").write_text("x")
    (job / "inside.ttl").symlink_to(job / "ontology.ttl")
    (job / "outside.ttl").symlink_to(tmp_path / "secret")
    (job / "dir").symlink_to(tmp_path)
    for name in ("ontology.ttl", "inside.ttl"):
        check_job_file(data_dir, job / name)
    for path in (job / "outside.ttl", job / "dir" / "secret", job, job / "missing.ttl"):
        with pytest.raises(StageError, match=f"^{path.name} isn't a regular file on the jobs volume$"):
            check_job_file(data_dir, path)


def test_upload_large(client: TestClient, app: FastAPI, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a file larger than the write buffer arrives whole, in several writes."""
    monkeypatch.setattr(browl.upload, "WRITE_BYTES", 1 << 16)
    content = TURTLE + os.urandom(3 << 20)
    assert post(client, content=content).status_code == 303
    finish(client, app)
    assert upload_path(data_dir, 1, ".ttl").read_bytes() == content


def record_clears(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, bool]]:
    """Record each page cache clear: the running job's generation, and whether its file had been opened yet."""
    cleared: list[tuple[int, bool]] = []

    def clear() -> None:
        generation = app.state.jobs.state.generation
        cleared.append((generation, upload_path(app.state.settings.data_dir, generation, ".ttl").exists()))

    monkeypatch.setattr(browl.upload, "clear_page_cache", clear)
    return cleared


def test_upload_clears_page_cache(client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that an upload clears the page cache as soon as its job has started, before its file is stored."""
    cleared = record_clears(app, monkeypatch)
    post(client)
    finish(client, app)
    assert cleared == [(1, False)]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"filename": "onto.owx"}, "unsupported file type '.owx'"),
        ({"filename": "onto"}, "unsupported file type '(none)'"),
        ({"reasoner": "pellet"}, "unknown reasoner 'pellet'"),
        ({"reasoner": "x" * 100}, "field is too long"),
        ({"reasoner": None}, "field must come before its file"),
        ({"content": b""}, "the file is empty"),
        ({"filename": "onto.rdf"}, "the file's name says RDF/XML, but its content looks like Turtle"),
        ({"filename": "onto.ttl", "content": RDFXML}, "says Turtle, but its content looks like RDF/XML"),
        (
            {"filename": "onto.ofn", "content": b"Ontology"},
            "says OWL Functional Syntax, but its content isn't recognizable",
        ),
        ({"filename": "onto.owl", "content": b'<Ontology xmlns="http://www.w3.org/2002/07/owl#"/>'}, "OWL/XML"),
        ({"content": b"\x1f\x8b\x08\x00"}, "looks compressed"),
        ({"filename": "", "content": b""}, "no file was chosen"),  # as a browser sends a form with no file chosen
        ({"filename": "onto.ttl", "content": RDFXML * 200}, "says Turtle, but its content looks like RDF/XML"),
    ],
)
def test_refused_before_claim(client: TestClient, app: FastAPI, kwargs: dict[str, object], reason: str) -> None:
    """Check that an invalid form is refused with its reason, and starts no job."""
    response = post(client, **kwargs)  # type: ignore[arg-type]
    assert response.status_code == 400
    assert escape(reason) in response.text
    assert '<form class="upload"' in response.text
    assert app.state.jobs.state.generation is None


@pytest.mark.parametrize(
    ("headers", "body", "reason"),
    [
        ({"Content-Type": "application/x-www-form-urlencoded"}, b"reasoner=none", "isn't a multipart form"),
        ({"Content-Type": "multipart/form-data"}, b"", "isn't a multipart form"),
        (
            {"Content-Type": FORM},
            b'--b\r\nContent-Disposition: form-data; name="reasoner"\r\n\r\nnone\r\n--b--\r\n',
            "no file was chosen",
        ),
        (
            {"Content-Type": FORM},
            b'--b\r\nContent-Disposition: form-data; name="ontology"; filename="o.ttl"\r\n\r\n' + TURTLE + b"\r\n"
            b'--b\r\nContent-Disposition: form-data; name="reasoner"\r\n\r\nnone\r\n--b--\r\n',
            "field must come before its file",
        ),
        ({"Content-Type": FORM}, b"no boundary here", "the form is malformed"),
        (
            {"Content-Type": FORM},
            b'--b\r\nContent-Disposition: form-data; name="reasoner"\r\n\r\nnone\r\n'
            + FILE_START
            + TURTLE
            + b"\r\n--b--\r\n",
            "the form has more than one 'reasoner' field",
        ),
        ({"Content-Type": FORM}, FILE_START + TURTLE, "the upload was cut short"),
    ],
)
def test_refused_request(client: TestClient, app: FastAPI, headers: dict[str, str], body: bytes, reason: str) -> None:
    """Check that a request that isn't a well-formed upload form is refused, and starts no job."""
    response = client.post(UPLOAD, content=body, headers=headers)
    assert response.status_code == 400
    assert escape(reason) in response.text
    assert app.state.jobs.state.generation is None


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (
            LONG
            + b'\r\n--b\r\nContent-Disposition: form-data; name="ontology"; filename="p.ttl"\r\n\r\n'
            + TURTLE
            + b"\r\n--b--\r\n",
            "more than one",
        ),
        (LONG, "the upload was cut short"),
        (
            LONG + b'\r\n--b\r\nContent-Disposition: form-data; name="reasoner"\r\n\r\nnone\r\n--b--\r\n',
            "the form has more than one 'reasoner' field",
        ),
    ],
)
def test_failed_after_claim(
    client: TestClient, app: FastAPI, data_dir: Path, monkeypatch: pytest.MonkeyPatch, body: bytes, reason: str
) -> None:
    """Check that a form found invalid once its job is claimed fails the job in the uploading stage (A-6).

    The page cache was still cleared, and the partial file is removed.
    """
    cleared = record_clears(app, monkeypatch)
    response = client.post(UPLOAD, content=FILE_START + body, headers={"Content-Type": FORM})
    assert response.status_code == 400
    assert escape(reason) in response.text
    state = app.state.jobs.state
    assert (state.generation, state.failed_stage, state.reasoner) == (1, Stage.UPLOADING, Reasoner.ELK)
    assert reason in (state.reason or "")
    assert "<strong>Upload Failed</strong>" in response.text
    assert not upload_path(data_dir, 1, ".ttl").parent.exists()
    assert (state.upload_name, state.upload_syntax) == ("o.ttl", "Turtle")
    assert state.upload_bytes == len(LONG)
    assert "received before the failure" in response.text
    assert cleared == [(1, False)]


@pytest.mark.parametrize(("code", "status_code"), [(errno.ENOSPC, 507), (errno.EDQUOT, 507), (errno.EIO, 500)])
def test_storage_error(
    client: TestClient, app: FastAPI, data_dir: Path, monkeypatch: pytest.MonkeyPatch, code: int, status_code: int
) -> None:
    """Check that a failed write, e.g. a full disk, fails the job saying why, and removes the partial file."""
    opened = browl.upload._open

    class Failing:
        def __init__(self, path: Path) -> None:
            self.file = opened(path)

        def write(self, data: bytes) -> int:
            raise OSError(code, os.strerror(code))

        def close(self) -> None:
            self.file.close()

    monkeypatch.setattr(browl.upload, "_open", Failing)
    response = post(client)
    assert response.status_code == status_code
    reason = f"couldn't store the uploaded file: {os.strerror(code)}"
    assert escape(reason) in response.text
    state = app.state.jobs.state
    assert (state.failed_stage, state.reason) == (Stage.UPLOADING, reason)
    assert not upload_path(data_dir, 1, ".ttl").parent.exists()


def test_open_fails(client: TestClient, app: FastAPI, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a file that can't be opened, its directory already made, fails the job and leaves no directory."""

    def failing(path: Path) -> IO[bytes]:
        path.parent.mkdir(parents=True)
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))

    monkeypatch.setattr(browl.upload, "_open", failing)
    assert post(client).status_code == 500
    assert app.state.jobs.state.failed_stage == Stage.UPLOADING
    assert not upload_path(data_dir, 1, ".ttl").parent.exists()


async def test_cancelled_while_opening(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that an upload cancelled while its file opens closes and removes the file once the open has finished."""
    entered, release = threading.Event(), threading.Event()
    opened: list[IO[bytes]] = []
    open_file = browl.upload._open

    def slow(path: Path) -> IO[bytes]:
        entered.set()
        release.wait(timeout=5)
        opened.append(open_file(path))
        return opened[-1]

    monkeypatch.setattr(browl.upload, "_open", slow)
    messages: list[Message] = [{"type": "http.request", "body": FILE_START + LONG, "more_body": True}]

    async def receive() -> Message:
        if not messages:
            await asyncio.Event().wait()
        return messages.pop(0)

    runner = await JobRunner.open(make_settings())
    try:
        task = asyncio.create_task(receive_upload(asgi_request(receive), runner))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        for _ in range(5):  # let the cancellation reach the upload before the open finishes
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await runner.close()
    assert opened[0].closed
    assert not upload_path(data_dir, 1, ".ttl").parent.exists()
    assert runner.state.failed_stage == Stage.UPLOADING


@pytest.mark.parametrize(
    ("overrides", "free", "status_code", "reason"),
    [
        ({"MAX_UPLOAD_BYTES": 100}, 10**12, 413, "the upload is larger than the limit of 100 bytes (MAX_UPLOAD_BYTES)"),
        ({}, browl.upload.FREE_SPACE_MARGIN_BYTES + 100, 507, "the jobs volume has too little free space"),
    ],
)
def test_refused_too_large(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    free: int,
    status_code: int,
    reason: str,
) -> None:
    """Check that an upload whose size, from `Content-Length`, is over the limit or the free space starts no job."""
    usage = shutil.disk_usage(data_dir)
    monkeypatch.setattr(browl.upload.shutil, "disk_usage", lambda path: usage._replace(free=free))
    app = make_app(**overrides)
    with TestClient(app) as client:
        response = post(client)
    assert response.status_code == status_code
    assert escape(reason) in response.text
    assert app.state.jobs.state.generation is None


@pytest.mark.parametrize("uploads", [True, False], ids=["uploads", "no-uploads"])
async def test_space_checked_on_uploads(data_dir: Path, monkeypatch: pytest.MonkeyPatch, uploads: bool) -> None:
    """Check that the free space checked is the uploads directory's (the jobs volume), or DATA_DIR's without it."""
    if uploads:
        (data_dir / UPLOADS_DIR).mkdir()
    checked: list[Path] = []
    usage = shutil.disk_usage(data_dir)

    def disk_usage(path: Path) -> shutil._ntuple_diskusage:
        checked.append(path)
        return usage._replace(free=10**12)

    monkeypatch.setattr(browl.upload.shutil, "disk_usage", disk_usage)
    await check_space(1, 100, data_dir)
    assert checked == [data_dir / UPLOADS_DIR if uploads else data_dir]


@pytest.mark.parametrize(("max_bytes", "claimed"), [(100, False), (len(LONG), True)], ids=["unclaimed", "claimed"])
async def test_streamed_over_limit(data_dir: Path, max_bytes: int, claimed: bool) -> None:
    """Check that a file sent without `Content-Length` is refused once it's larger than `MAX_UPLOAD_BYTES`.

    Found before the claim, it starts no job; after, its job fails.
    """
    messages: list[Message] = [
        {"type": "http.request", "body": FILE_START + LONG, "more_body": True},
        {"type": "http.request", "body": TURTLE, "more_body": False},
    ]

    async def receive() -> Message:
        return messages.pop(0)

    request = asgi_request(receive)
    request.app.state.settings = make_settings(MAX_UPLOAD_BYTES=max_bytes)
    runner = await JobRunner.open(request.app.state.settings)
    try:
        with pytest.raises(UploadError, match="larger than the limit") as raised:
            await receive_upload(request, runner)
    finally:
        await runner.close()
    assert raised.value.status_code == 413
    assert runner.state.failed_stage == (Stage.UPLOADING if claimed else None)
    assert not upload_path(data_dir, 1, ".ttl").parent.exists()


@pytest.mark.parametrize(("site", "status_code"), [("cross-site", 403), ("same-site", 403), ("same-origin", 303)])
def test_cross_site_refused(client: TestClient, app: FastAPI, site: str, status_code: int) -> None:
    """Check that an upload a browser sent from another site's page is refused before it's read (CSRF)."""
    response = client.post(
        UPLOAD,
        data={"reasoner": "none"},
        files={"ontology": ("onto.ttl", TURTLE)},
        headers={"Sec-Fetch-Site": site},
        follow_redirects=False,
    )
    assert response.status_code == status_code
    assert (app.state.jobs.state.generation is None) == (status_code == 403)


def test_refused_while_running(client: TestClient, app: FastAPI, data_dir: Path, loading: threading.Event) -> None:
    """Check that an upload while a job runs is refused with 409, leaving that job and its upload alone."""
    post(client)
    assert loading.wait(timeout=5)
    response = post(client, content=b"other")
    assert response.status_code == 409
    assert "already running" in response.text
    assert app.state.jobs.state.generation == 1
    assert upload_path(data_dir, 1, ".ttl").read_bytes() == TURTLE


def test_upload_page_while_running_polls(client: TestClient, loading: threading.Event) -> None:
    """Check that while a job runs, the status polls (or without JS, the page refreshes), and the site is loading."""
    post(client)
    assert loading.wait(timeout=5)
    text = client.get(UPLOAD).text
    assert 'hx-trigger="every' in text
    assert f'<noscript><meta http-equiv="refresh" content="2; url={UPLOAD}"></noscript>' in text
    response = client.get("/example.org/onto")
    assert response.status_code == 503
    assert "<h1>Loading the ontology</h1>" in response.text


def test_form_disabled_while_running(client: TestClient, loading: threading.Event) -> None:
    """Check that while a job runs, the form's controls are disabled, with a note saying why."""
    post(client)
    assert loading.wait(timeout=5)
    text = client.get(UPLOAD).text
    assert '<fieldset class="upload-controls" disabled aria-describedby="upload-wait">' in text
    assert f'<p id="upload-wait" class="note">{UPLOAD_WAIT}</p>' in text


@pytest.mark.parametrize("ended", [False, True])
def test_form_enabled_unless_running(client: TestClient, app: FastAPI, ended: bool) -> None:
    """Check that with no job, or once it has ended, the form's controls are enabled, without the note."""
    if ended:
        post(client)
        finish(client, app)
    text = client.get(UPLOAD).text
    assert '<fieldset class="upload-controls">' in text
    assert "disabled" not in text
    assert UPLOAD_WAIT not in text


def asgi_request(receive: Receive, boundary: bytes = b"b") -> Request:
    """Return an upload request, its body read from `receive`, for an app with test settings."""
    app = FastAPI()
    app.state.settings = make_settings()
    content_type = b"multipart/form-data; boundary=" + boundary
    return Request(
        {"type": "http", "method": "POST", "headers": [(b"content-type", content_type)], "app": app}, receive
    )


@pytest.mark.parametrize("chunk_bytes", [1000, 10_000, 3 * STREAM_WRITE_BYTES])
async def test_streamed(data_dir: Path, monkeypatch: pytest.MonkeyPatch, chunk_bytes: int) -> None:
    """Check that the file is written as it arrives: between chunks, less than `WRITE_BYTES` of it is held in memory.

    The bound holds however the body is chunked, so it's checked as each chunk is asked for, with chunks smaller than
    a write and larger than one.
    """
    monkeypatch.setattr(browl.upload, "WRITE_BYTES", STREAM_WRITE_BYTES)
    content = TURTLE + os.urandom(1 << 20)
    body = FILE_START + content + b"\r\n--b--\r\n"
    chunks = [body[i : i + chunk_bytes] for i in range(0, len(body), chunk_bytes)]
    path = upload_path(data_dir, 1, ".ttl")
    sent = 0
    held: list[int] = []  # how much of the file had arrived but wasn't on disk, each time a chunk was asked for

    async def receive() -> Message:
        nonlocal sent
        arrived = min(max(sent - len(FILE_START), 0), len(content))
        held.append(arrived - (path.stat().st_size if path.exists() else 0))
        chunk = chunks.pop(0)
        sent += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    count = len(chunks)
    runner = await JobRunner.open(make_settings())
    try:
        await receive_upload(asgi_request(receive), runner)
        await runner.wait()
    finally:
        await runner.close()
    assert path.read_bytes() == content
    assert len(held) == count
    assert 0 <= max(held) < STREAM_WRITE_BYTES


async def test_small_chunks(data_dir: Path) -> None:
    """Check that a form fed a few bytes at a time, splitting headers and data across chunks, is stored whole."""
    content = TURTLE * (2 * SNIFF_BYTES // len(TURTLE))  # claimed mid-file, at the checked part's end
    body = (
        b'--boundary\r\nContent-Disposition: form-data; name="reasoner"\r\n\r\nhermit\r\n'
        b'--boundary\r\nContent-Disposition: form-data; name="ontology"; filename="o.ttl"\r\n'
        b"Content-Type: text/turtle\r\n\r\n" + content + b"\r\n--boundary--\r\n"
    )
    chunks = [body[i : i + 7] for i in range(0, len(body), 7)]

    async def receive() -> Message:
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    runner = await JobRunner.open(make_settings())
    try:
        context = await receive_upload(asgi_request(receive, b"boundary"), runner)
        await runner.wait()
    finally:
        await runner.close()
    assert context.reasoner == Reasoner.HERMIT
    assert context.upload == upload_path(data_dir, 1, ".ttl")
    assert context.upload.read_bytes() == content
    assert runner.state.upload_bytes == len(content)
    assert runner.state.stage == Stage.READY


async def test_client_disconnect(data_dir: Path) -> None:
    """Check that a client disconnecting mid-file fails the job in the uploading stage, saying so."""
    messages: list[Message] = [
        {
            "type": "http.request",
            "body": FILE_START + LONG,
            "more_body": True,
        },
        {"type": "http.disconnect"},
    ]

    async def receive() -> Message:
        return messages.pop(0)

    runner = await JobRunner.open(make_settings())
    try:
        with pytest.raises(UploadError, match="client disconnected"):
            await receive_upload(asgi_request(receive), runner)
    finally:
        await runner.close()
    assert runner.state.failed_stage == Stage.UPLOADING
    assert "client disconnected" in (runner.state.reason or "")
    assert not upload_path(data_dir, 1, ".ttl").parent.exists()


@pytest.mark.parametrize(("file", "claimed"), [(TURTLE, False), (LONG, True)], ids=["unclaimed", "claimed"])
async def test_stalled(data_dir: Path, monkeypatch: pytest.MonkeyPatch, file: bytes, claimed: bool) -> None:
    """Check that an upload that stops sending data fails and frees the runner; a claimed job fails saying why."""
    monkeypatch.setattr(browl.upload, "IDLE_S", 0.05)
    messages: list[Message] = [{"type": "http.request", "body": FILE_START + file, "more_body": True}]

    async def receive() -> Message:
        if not messages:
            await asyncio.Event().wait()
        return messages.pop(0)

    reason = "the upload stalled: no data arrived for 0.05 s"
    runner = await JobRunner.open(make_settings())
    try:
        with pytest.raises(UploadError) as raised:
            async with asyncio.timeout(5):  # so a stall that isn't caught fails the test rather than hanging it
                await receive_upload(asgi_request(receive), runner)
        assert str(raised.value) == reason
        if claimed:
            assert (runner.state.failed_stage, runner.state.reason) == (Stage.UPLOADING, reason)
            assert not upload_path(data_dir, 1, ".ttl").parent.exists()
        else:
            assert runner.state.generation is None
        await runner.start()  # the runner is free
    finally:
        await runner.close()


async def test_other_timeout_unmapped(data_dir: Path) -> None:
    """Check that a `TimeoutError` from reading the request isn't reported as the upload stalling."""
    error = TimeoutError("from the server")

    async def receive() -> Message:
        raise error

    runner = await JobRunner.open(make_settings())
    try:
        with pytest.raises(TimeoutError) as raised:
            await receive_upload(asgi_request(receive), runner)
    finally:
        await runner.close()
    assert raised.value is error


@pytest.mark.parametrize("error", [ValueError("bad state"), PermissionError(errno.EACCES, "no access")])
async def test_other_errors_unmapped(data_dir: Path, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Check that errors from outside the form's parsing and the file's storage aren't reported as either."""
    messages: list[Message] = [{"type": "http.request", "body": FILE_START + LONG, "more_body": False}]

    async def receive() -> Message:
        return messages.pop(0)

    def claim(*args: object) -> None:
        raise error

    runner = await JobRunner.open(make_settings())
    monkeypatch.setattr(runner._store, "claim", claim)
    try:
        with pytest.raises(type(error)) as raised:
            await receive_upload(asgi_request(receive), runner)
    finally:
        await runner.close()
    assert raised.value is error


async def test_upload_progress_streamed(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that the size recorded while a file streams in follows each chunk, not just the claim and the end."""
    monkeypatch.setattr(browl.jobs, "PROGRESS_S", 0)
    file_chunks = [LONG, TURTLE, TURTLE]
    messages = [FILE_START + file_chunks[0], *file_chunks[1:], b"\r\n--b--\r\n"]
    sent = 0
    runner = await JobRunner.open(make_settings())

    async def receive() -> Message:
        nonlocal sent
        if sent:  # the claim is made in the first chunk
            async with asyncio.timeout(2):
                while runner.state.upload_bytes != sum(map(len, file_chunks[:sent])):
                    await asyncio.sleep(0.005)
        sent += 1
        return {"type": "http.request", "body": messages.pop(0), "more_body": bool(messages)}

    try:
        await receive_upload(asgi_request(receive), runner)
        await runner.wait()
    finally:
        await runner.close()
    assert runner.state.upload_bytes == sum(map(len, file_chunks))


@pytest.mark.parametrize(
    ("sample", "syntax"),
    [
        (TURTLE, "Turtle"),
        (b"\xef\xbb\xbf# a comment\n\nPREFIX ex: <http://example.org/>\n", "Turtle"),
        (b"<http://example.org/a> a <http://example.org/B> .", "Turtle"),
        (b"ex:a a ex:B .", None),
        (RDFXML, "RDF/XML"),
        (b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">', "RDF/XML"),
        (
            (
                b'<?xml version="1.0"?>\n<!DOCTYPE rdf:RDF [\n<!ENTITY owl "http://www.w3.org/2002/07/owl#">\n]>\n'
                b"<!-- c -->\n<rdf:RDF"
            ),
            "RDF/XML",
        ),
        (b'<?xml version="1.0"?>\n<!DOCTYPE rdf:RDF [\n<!ENTITY owl "http://www.w3.org/2002/07/owl#">', "RDF/XML"),
        (b'<owl:Ontology rdf:about="http://example.org/onto"/>', "RDF/XML"),
        (b'<?xml version="1.0"?>\n<Ontology xmlns="http://www.w3.org/2002/07/owl#" ontologyIRI="x">', "OWL/XML"),
        (b'<owl:Ontology xmlns:owl="http://www.w3.org/2002/07/owl#" ontologyIRI="x">', "OWL/XML"),
        (OFN, "OWL Functional Syntax"),
        (b"# comment\nOntology (<http://example.org/onto>)", "OWL Functional Syntax"),
        (b'<!DOCTYPE Ontology SYSTEM "o.dtd">\n<Ontology ontologyIRI="[x]">', "OWL/XML"),
        (b'<!DOCTYPE rdf:RDF [<!ENTITY a "b">]>\n<?pi x?>\n<!-- c -->\n<Ontology>', "OWL/XML"),
        (b'<!DOCTYPE rdf:RDF [<!ENTITY a "b">]', "RDF/XML"),
        (b"<!-- unclosed", "RDF/XML"),
        (b"PK\x03\x04", "compressed"),
        (b"", None),
    ],
)
def test_sniff_syntax(sample: bytes, syntax: str | None) -> None:
    """Check the syntax the start of a file is taken to be."""
    assert sniff_syntax(sample) == syntax


@pytest.mark.parametrize(
    "sample",
    [
        b'<?xml version="1.0"?><!DOCTYPE' + b"[]" * 24,  # ~1 s with the old regex; more pairs would hang it, not fail
        b"<!--" * SNIFF_BYTES,
        b"<?" * SNIFF_BYTES,
        b"<!DOCTYPE" * SNIFF_BYTES,
        b"<!DOCTYPE[" + b"]" * SNIFF_BYTES,
        b"#\n" * SNIFF_BYTES,
        b"<" + b"a" * SNIFF_BYTES,
    ],
    ids=["doctype-subsets", "comments", "pis", "doctypes", "subset-ends", "comment-lines", "long-tag"],
)
def test_sniff_syntax_fast(sample: bytes) -> None:
    """Check that no sample makes the check slow: it runs on the event loop, before any job is claimed.

    A backtracking regex once took time exponential in the first sample's `[]` pairs, so a 100-byte upload froze the
    server.
    """
    started = time.perf_counter()
    sniff_syntax(sample[:SNIFF_BYTES])
    assert time.perf_counter() - started < 0.05


@pytest.mark.parametrize(
    ("sample", "syntax"), [(b"ex:a a ex:B .", Syntax.TURTLE), (RDFXML, Syntax.RDF_XML), (OFN, Syntax.OFN)]
)
def test_check_syntax_passes(sample: bytes, syntax: Syntax) -> None:
    """Check that a file passes when it looks like its name's syntax, or, as Turtle, isn't distinctive."""
    check_syntax(sample, syntax)


@pytest.mark.parametrize("syntax", [Syntax.RDF_XML, Syntax.OFN])
def test_check_syntax_indistinct(syntax: Syntax) -> None:
    """Check that an RDF/XML or OWL Functional Syntax file must start distinctively."""
    with pytest.raises(UploadError, match="isn't recognizable"):
        check_syntax(b"ex:a a ex:B .", syntax)


def test_upload_progress_shown(client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that while a file uploads, the status shows how much has arrived so far."""
    monkeypatch.setattr(browl.jobs, "PROGRESS_S", 0)
    assert client.portal is not None
    with client.portal.wrap_async_context_manager(app.state.jobs.upload(Reasoner.NONE, "big.ttl", "Turtle")) as context:
        context.upload_bytes = 2_500_000
        client.portal.call(app.state.jobs.record_upload_progress, context)
        text = " ".join(client.get(UPLOAD).text.split())
    assert '<span title="2,500,000 bytes">2.5 MB</span> received so far.' in text
    assert "File <code>big.ttl</code>, Turtle" in text
