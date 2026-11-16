"""Tests for the Fuseki client, against a stubbed endpoint."""

import asyncio
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx2
import pytest

from browl.fuseki import NTRIPLES, TSV, FusekiClient, SelectResult, SparqlError, SparqlTimeoutError
from browl.rdf import IRI, RDF_LANG_STRING, Literal

pytestmark = pytest.mark.anyio

A = "<http://example.org/a>"
P = "<http://example.org/p>"
TRIPLE = f'{A} {P} "x" .'
TIMEOUT_TEXT = "## Query cancelled due to timeout during execution ##"


@pytest.fixture
def anyio_backend() -> str:
    """Run the async tests on asyncio, as the app does."""
    return "asyncio"


Handler = Callable[[httpx2.Request], Awaitable[httpx2.Response]]


def stub(handler: Handler) -> FusekiClient:
    """Return a client whose requests go to a stubbed Fuseki."""
    return FusekiClient(httpx2.AsyncClient(base_url="http://fuseki:3030", transport=httpx2.MockTransport(handler)))


def answer(
    body: bytes | str | list[bytes], content_type: str = TSV, status: int = 200, error: Exception | None = None
) -> FusekiClient:
    """Return a client whose stubbed Fuseki answers every query with this body, sent in chunks if it's a list.

    Args:
        body: The body, or its chunks.
        content_type: The Content-Type header; charset added.
        status: The HTTP status.
        error: Raised after the last chunk, as when the connection breaks mid-response.
    """
    chunks = body if isinstance(body, list) else [body.encode() if isinstance(body, str) else body]

    async def stream() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk
        if error:
            raise error

    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, headers={"Content-Type": f"{content_type}; charset=utf-8"}, content=stream())

    return stub(handler)


async def test_select_request() -> None:
    """Check that a SELECT query is POSTed to the dataset's SPARQL endpoint as a form, asking for TSV."""
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, headers={"Content-Type": TSV}, content=b"?s\n")

    await stub(handler).select("gen-3", "SELECT ?s WHERE { ?s ?p ?o }")
    [request] = requests
    assert request.method == "POST"
    assert str(request.url) == "http://fuseki:3030/gen-3/sparql"
    assert request.headers["Accept"] == TSV
    assert urllib.parse.parse_qs(request.content.decode()) == {"query": ["SELECT ?s WHERE { ?s ?p ?o }"]}


async def test_select() -> None:
    """Check that a SELECT result's header and rows are parsed, with CRLF line ends and unbound values."""
    result = await answer(f'?s\t?label\r\n{A}\t"x"@en\r\n{A}\t\r\n').select("ds", "SELECT")
    a = IRI("http://example.org/a")
    assert result == SelectResult(("s", "label"), [(a, Literal("x", RDF_LANG_STRING, "en")), (a, None)])


async def test_select_no_rows() -> None:
    """Check that a result with only its header has no rows."""
    assert await answer("?s\n").select("ds", "SELECT") == SelectResult(("s",), [])


async def test_select_chunks_split_lines_and_characters() -> None:
    """Check that rows split across chunks, even inside a UTF-8 character, are put back together."""
    body = '?s\n"café \u2028"\n'.encode()
    split = body.index(b"\xc3") + 1  # inside é
    result = await answer([body[:5], body[5:split], body[split:]]).select("ds", "SELECT")
    assert result.rows == [(Literal("café \u2028"),)]


async def test_select_stream() -> None:
    """Check that a streamed SELECT result gives its variables, then its rows as they arrive."""
    async with answer([b"?s\n", f"{A}\n".encode(), f"{A}\n".encode()]).select_stream("ds", "SELECT") as stream:
        assert stream.variables == ("s",)
        assert [row async for row in stream] == [(IRI("http://example.org/a"),)] * 2


async def test_construct() -> None:
    """Check that a DESCRIBE or CONSTRUCT result comes back as checked N-Triples."""
    body = f"{TRIPLE}\n{TRIPLE}"
    assert await answer(body, NTRIPLES).construct("ds", "DESCRIBE") == f"{TRIPLE}\n{TRIPLE}\n".encode()


async def test_construct_stream() -> None:
    """Check that a streamed DESCRIBE or CONSTRUCT result gives its lines as they arrive."""
    async with answer([f"{TRIPLE}\n{TRIPLE[:10]}".encode(), f"{TRIPLE[10:]}\n".encode()], NTRIPLES).construct_stream(
        "ds", "CONSTRUCT"
    ) as lines:
        assert [line async for line in lines] == [TRIPLE, TRIPLE]


@pytest.mark.parametrize(
    "body",
    [
        f"?s\n{A}\n{TIMEOUT_TEXT}\n",  # S-6: the text Fuseki appends after a timeout
        f"?s\n{A}\n<http://example.org/b\n\n{TIMEOUT_TEXT}\n",  # cut inside a term
        f"?s\t?o\n{A}\t{A}\nError 500: Java heap space\n",
        f"{TIMEOUT_TEXT}\n",  # no header
        "",
    ],
    ids=["appended", "cut-term", "two-columns", "no-header", "empty"],
)
async def test_select_truncated(body: str) -> None:
    """Check that a 200 result cut short, with Fuseki's error text appended, fails instead of looking short."""
    with pytest.raises(SparqlError, match=r"(malformed|empty) SELECT result"):
        await answer(body).select("ds", "SELECT")


async def test_select_stream_truncated() -> None:
    """Check that a streamed result fails at the appended error text, after the rows before it."""
    rows = []
    with pytest.raises(SparqlError, match="malformed SELECT result"):
        async with answer(f"?s\n{A}\n{TIMEOUT_TEXT}\n").select_stream("ds", "SELECT") as stream:
            async for row in stream:
                rows.append(row)
    assert rows == [(IRI("http://example.org/a"),)]


@pytest.mark.parametrize(
    "body",
    [f"{TRIPLE}\n{TIMEOUT_TEXT}\n", f"{TRIPLE}\n{TRIPLE[:20]}", "Error 500: Server Error\n"],
    ids=["appended", "cut", "error-only"],
)
async def test_construct_truncated(body: str) -> None:
    """Check that an N-Triples result with error text appended, or cut short, fails."""
    with pytest.raises(SparqlError, match="malformed N-Triples result"):
        await answer(body, NTRIPLES).construct("ds", "DESCRIBE")


@pytest.mark.parametrize("method", ["select", "construct"])
async def test_body_ends_early(method: str) -> None:
    """Check that the HTTP client's error for a body that ends early (Fuseki exiting mid-response) isn't discarded."""
    content_type = TSV if method == "select" else NTRIPLES
    body = f"?s\n{A}\n" if method == "select" else f"{TRIPLE}\n"
    error = httpx2.RemoteProtocolError("peer closed connection without sending complete message body")
    client = answer(body, content_type, error=error)
    with pytest.raises(SparqlError, match="the result broke off") as raised:
        await getattr(client, method)("ds", "QUERY")
    assert raised.value.__cause__ is error


async def test_not_utf8() -> None:
    """Check that a body that isn't UTF-8 fails."""
    with pytest.raises(SparqlError, match="isn't UTF-8"):
        await answer(b"?s\n\xff\n").select("ds", "SELECT")


@pytest.mark.parametrize(
    ("status", "body"),
    [(400, "Parse error: Encountered <VAR1> at line 1"), (404, "Not Found"), (500, "Server Error"), (301, "")],
)
async def test_status(status: int, body: str) -> None:
    """Check that any status other than 2xx fails, with Fuseki's error text in the message."""
    with pytest.raises(SparqlError, match=f"Fuseki answered {status}") as raised:
        await answer(body, "text/plain", status).select("ds", "SELECT")
    assert body in str(raised.value)


async def test_no_content_type() -> None:
    """Check that a success with no content type, such as 204, fails."""

    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(204)

    with pytest.raises(SparqlError, match="no content type"):
        await stub(handler).select("ds", "SELECT")


async def test_error_body_broken() -> None:
    """Check that an error status still fails clearly when its body can't be read."""
    client = answer("partial", "text/plain", 500, error=httpx2.RemoteProtocolError("closed"))
    with pytest.raises(SparqlError, match="Fuseki answered 500: \\(error body not read"):
        await client.select("ds", "SELECT")


async def test_error_body_shortened() -> None:
    """Check that a long error body is shortened in the message."""
    with pytest.raises(SparqlError) as raised:
        await answer("x" * 10_000, "text/plain", 500).select("ds", "SELECT")
    assert len(str(raised.value)) < 1100


async def test_wrong_content_type() -> None:
    """Check that a result in another format than the one asked for fails."""
    with pytest.raises(SparqlError, match="Fuseki sent text/html, not text/tab-separated-values"):
        await answer("<html></html>", "text/html").select("ds", "SELECT")


async def test_unreachable() -> None:
    """Check that a connection failure is a SparqlError."""
    client = FusekiClient(httpx2.AsyncClient(base_url="http://127.0.0.1:1"))
    with pytest.raises(SparqlError, match="couldn't reach Fuseki"):
        await client.select("ds", "SELECT")


async def test_connect_timeout() -> None:
    """Check that an httpx2 timeout while connecting is a SparqlTimeoutError."""

    async def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectTimeout("timed out")

    with pytest.raises(SparqlTimeoutError, match="didn't answer in time"):
        await stub(handler).select("ds", "SELECT")


async def test_read_timeout_while_streaming() -> None:
    """Check that an httpx2 read timeout mid-result is a SparqlTimeoutError."""
    with pytest.raises(SparqlTimeoutError, match="stopped sending"):
        await answer("?s\n", error=httpx2.ReadTimeout("timed out")).select("ds", "SELECT")


@pytest.mark.parametrize("method", ["select", "construct"])
async def test_whole_query_time_limit(method: str) -> None:
    """Check that the time limit covers the whole query, including a body that trickles in."""

    async def trickle() -> AsyncIterator[bytes]:
        yield b"?s\n" if method == "select" else f"{TRIPLE}\n".encode()
        await asyncio.sleep(1)
        yield b""

    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, headers={"Content-Type": TSV if method == "select" else NTRIPLES}, content=trickle()
        )

    with pytest.raises(SparqlTimeoutError, match=r"longer than 0\.05 s"):
        await getattr(stub(handler), method)("ds", "QUERY", timeout_s=0.05)
