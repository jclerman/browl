"""Tests for the upload page and the streaming upload (A-2, A-3, A-4)."""

import asyncio
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from markupsafe import escape
from starlette.requests import Request
from starlette.types import Message

import browl.upload
from browl.jobs import JobContext, JobRunner, Reasoner, Stage
from browl.upload import UPLOADS_DIR, UploadError, receive_upload, upload_path
from tests.helpers import make_app, make_settings

UPLOAD = "/_browl/admin/upload"
pytestmark = pytest.mark.anyio

TURTLE = b"@prefix ex: <http://example.org/> .\nex:a a ex:B .\n"


@pytest.fixture
def app(data_dir: Path) -> FastAPI:
    """Return an app with default settings."""
    return make_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Yield a test client for the app, its lifespan running, with no job run yet."""
    with TestClient(app) as test_client:
        yield test_client


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
    ("filename", "suffix"),
    [("onto.ttl", ".ttl"), ("onto.rdf", ".rdf"), ("uberon.OWL", ".rdf"), ("onto.ofn", ".ofn")],
)
def test_upload(client: TestClient, app: FastAPI, data_dir: Path, filename: str, suffix: str) -> None:
    """Check that an upload is stored with the suffix for its format, and starts a job that reaches ready."""
    response = post(client, filename)
    assert response.status_code == 303
    assert response.headers["location"] == UPLOAD
    finish(client, app)
    state = app.state.jobs.state
    assert (state.generation, state.stage, state.reasoner) == (1, Stage.READY, Reasoner.NONE)
    assert upload_path(data_dir, 1, suffix).read_bytes() == TURTLE


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


def test_upload_large(client: TestClient, app: FastAPI, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a file larger than the write buffer arrives whole, in several writes."""
    monkeypatch.setattr(browl.upload, "WRITE_BYTES", 1 << 16)
    content = os.urandom(3 << 20)
    assert post(client, content=content).status_code == 303
    finish(client, app)
    assert upload_path(data_dir, 1, ".ttl").read_bytes() == content


def test_upload_clears_page_cache(client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that an upload clears the page cache once its job has started."""
    cleared: list[int] = []
    monkeypatch.setattr(browl.upload, "clear_page_cache", lambda: cleared.append(app.state.jobs.state.generation))
    post(client)
    finish(client, app)
    assert cleared == [1]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"filename": "onto.owx"}, "unsupported file type '.owx'"),
        ({"filename": "onto"}, "unsupported file type '(none)'"),
        ({"reasoner": "pellet"}, "unknown reasoner 'pellet'"),
        ({"reasoner": "x" * 100}, "field is too long"),
        ({"reasoner": None}, "field must come before its file"),
    ],
)
def test_refused_before_claim(client: TestClient, app: FastAPI, kwargs: dict[str, object], reason: str) -> None:
    """Check that an invalid form is refused with its reason, and starts no job."""
    response = post(client, **kwargs)  # type: ignore[arg-type]
    assert response.status_code == 400
    assert escape(reason) in response.text
    assert '<form class="upload"' in response.text
    assert app.state.jobs.state.generation is None


def test_refused_reasoner_after_file(client: TestClient, app: FastAPI) -> None:
    """Check that a form whose file comes before its reasoner field is refused, and starts no job."""
    body = (
        b'--b\r\nContent-Disposition: form-data; name="ontology"; filename="o.ttl"\r\n\r\n' + TURTLE + b"\r\n"
        b'--b\r\nContent-Disposition: form-data; name="reasoner"\r\n\r\nnone\r\n--b--\r\n'
    )
    response = client.post(UPLOAD, content=body, headers={"Content-Type": "multipart/form-data; boundary=b"})
    assert response.status_code == 400
    assert "field must come before its file" in response.text
    assert app.state.jobs.state.generation is None


@pytest.mark.parametrize(
    ("headers", "body", "reason"),
    [
        ({"Content-Type": "application/x-www-form-urlencoded"}, b"reasoner=none", "isn't a multipart form"),
        ({"Content-Type": "multipart/form-data"}, b"", "isn't a multipart form"),
        (
            {"Content-Type": "multipart/form-data; boundary=b"},
            b'--b\r\nContent-Disposition: form-data; name="reasoner"\r\n\r\nnone\r\n--b--\r\n',
            "no file was chosen",
        ),
        (
            {"Content-Type": "multipart/form-data; boundary=b"},
            b"no boundary here",
            "the form is malformed",
        ),
    ],
)
def test_refused_request(client: TestClient, app: FastAPI, headers: dict[str, str], body: bytes, reason: str) -> None:
    """Check that a request that isn't a well-formed upload form is refused, and starts no job."""
    response = client.post(UPLOAD, content=body, headers=headers)
    assert response.status_code == 400
    assert escape(reason) in response.text
    assert app.state.jobs.state.generation is None


def test_refused_no_file_chosen(client: TestClient, app: FastAPI) -> None:
    """Check that a form sent with no file chosen, as a browser sends it (an empty file name), starts no job."""
    response = post(client, filename="", content=b"")
    assert response.status_code == 400
    assert "no file was chosen" in response.text
    assert app.state.jobs.state.generation is None


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"", "the file is empty"),
        (TURTLE + b'\r\n--b\r\nContent-Disposition: form-data; name="ontology"; filename="p.ttl"', "more than one"),
        (TURTLE, "the upload was cut short"),
    ],
)
def test_failed_after_claim(client: TestClient, app: FastAPI, body: bytes, reason: str) -> None:
    """Check that a form found invalid once its file has started fails the job in the uploading stage (A-6)."""
    start = b'--b\r\nContent-Disposition: form-data; name="reasoner"\r\n\r\nelk\r\n'
    start += b'--b\r\nContent-Disposition: form-data; name="ontology"; filename="o.ttl"\r\n\r\n'
    end = b"\r\n--b--\r\n" if reason != "the upload was cut short" else b""
    if reason == "more than one":
        body += b"\r\n\r\n" + TURTLE
    response = client.post(
        UPLOAD, content=start + body + end, headers={"Content-Type": "multipart/form-data; boundary=b"}
    )
    assert response.status_code == 400
    assert escape(reason) in response.text
    state = app.state.jobs.state
    assert (state.generation, state.failed_stage, state.reasoner) == (1, Stage.UPLOADING, Reasoner.ELK)
    assert reason in (state.reason or "")
    assert "Failed in stage <strong>uploading</strong>" in response.text


def test_refused_while_running(client: TestClient, app: FastAPI, data_dir: Path) -> None:
    """Check that an upload while a job runs is refused with 409, leaving that job and its upload alone."""
    loading = threading.Event()

    async def forever(context: JobContext) -> None:
        loading.set()
        await asyncio.Event().wait()

    app.state.jobs.register(Stage.LOADING, forever)
    post(client)
    assert loading.wait(timeout=5)
    response = post(client, content=b"other")
    assert response.status_code == 409
    assert "already running" in response.text
    assert app.state.jobs.state.generation == 1
    assert upload_path(data_dir, 1, ".ttl").read_bytes() == TURTLE


def test_upload_page_while_running_polls(client: TestClient, app: FastAPI) -> None:
    """Check that while a job runs, the page's status polls, and without JS the page refreshes itself."""
    loading = threading.Event()

    async def forever(context: JobContext) -> None:
        loading.set()
        await asyncio.Event().wait()

    app.state.jobs.register(Stage.LOADING, forever)
    post(client)
    assert loading.wait(timeout=5)
    text = client.get(UPLOAD).text
    assert 'hx-trigger="every' in text
    assert '<noscript><meta http-equiv="refresh"' in text


async def test_small_chunks(data_dir: Path) -> None:
    """Check that a form fed a few bytes at a time, splitting headers and data across chunks, is stored whole."""
    content = TURTLE * 50
    body = (
        b'--boundary\r\nContent-Disposition: form-data; name="reasoner"\r\n\r\nhermit\r\n'
        b'--boundary\r\nContent-Disposition: form-data; name="ontology"; filename="o.ttl"\r\n'
        b"Content-Type: text/turtle\r\n\r\n" + content + b"\r\n--boundary--\r\n"
    )
    chunks = [body[i : i + 7] for i in range(0, len(body), 7)]

    async def receive() -> Message:
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    settings = make_settings()
    runner = await JobRunner.open(settings)
    app = FastAPI()
    app.state.settings = settings
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"content-type", b"multipart/form-data; boundary=boundary")],
        "app": app,
    }
    try:
        context = await receive_upload(Request(scope, receive), runner)
        await runner.wait()
    finally:
        await runner.close()
    assert context.reasoner == Reasoner.HERMIT
    assert context.upload == upload_path(data_dir, 1, ".ttl")
    assert context.upload.read_bytes() == content
    assert runner.state.stage == Stage.READY


async def test_client_disconnect(data_dir: Path) -> None:
    """Check that a client disconnecting mid-file fails the job in the uploading stage, saying so."""
    messages: list[Message] = [
        {
            "type": "http.request",
            "body": b'--b\r\nContent-Disposition: form-data; name="reasoner"\r\n\r\nnone\r\n'
            b'--b\r\nContent-Disposition: form-data; name="ontology"; filename="o.ttl"\r\n\r\n' + TURTLE,
            "more_body": True,
        },
        {"type": "http.disconnect"},
    ]

    async def receive() -> Message:
        return messages.pop(0)

    settings = make_settings()
    runner = await JobRunner.open(settings)
    app = FastAPI()
    app.state.settings = settings
    scope = {"type": "http", "method": "POST", "headers": [(b"content-type", b"multipart/form-data; boundary=b")]}
    scope["app"] = app
    try:
        with pytest.raises(UploadError, match="client disconnected"):
            await receive_upload(Request(scope, receive), runner)
    finally:
        await runner.close()
    assert runner.state.failed_stage == Stage.UPLOADING
    assert "client disconnected" in (runner.state.reason or "")
