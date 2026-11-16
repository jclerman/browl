"""Async SPARQL client for Fuseki: SELECT results read as TSV, DESCRIBE and CONSTRUCT results as N-Triples.

Every response is checked strictly, because a query that fails after Fuseki has sent HTTP 200 still ends normally,
with the error appended as text (DESIGN.md §2.14). A query fails, raising `SparqlError`, when Fuseki answers with any
status other than 2xx, when the connection fails or the body ends early, when it doesn't answer in time, and when the
result doesn't parse. A failed query never looks like a short result. Bulk results that may break off cleanly at a row
boundary need their own completeness check (P-8).
"""

import asyncio
import codecs
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx2

from browl.rdf import RDFSyntaxError, Term, check_ntriples_triple, parse_tsv_header, parse_tsv_row

QUERY_TIMEOUT_S = 30.0
"""Time limit for a whole buffered query (`select`, `construct`): ample for a page's queries (2–4 ms each, S-4)."""

STREAM_READ_TIMEOUT_S = 600.0
"""Time limit for each wait for data while streaming, whose total time depends on the ontology's size."""

CONSTRUCT_MAX_BYTES = 64 * 2**20
"""Largest result `construct` holds. Its result is parsed by the OWLAPI, and the JVM exits on running out of heap, so
a huge fragment could crash the app again on every retry. The default heap (`APP_JVM_MAX_HEAP`, 1g) parsed 160 MB of
N-Triples, all of Uberon, even at 384 MB (research/P-4); a page's fragment is a few thousand triples at most."""

MAX_LINE_CHARS = 16 * 2**20
"""Longest line accepted in any result, so a malformed one can't grow without bound."""

TSV = "text/tab-separated-values"
NTRIPLES = "application/n-triples"
_ERROR_TEXT_LIMIT = 1000  # characters of an error response kept in the exception
_HTTP_TIMEOUT = httpx2.Timeout(QUERY_TIMEOUT_S, read=STREAM_READ_TIMEOUT_S)
# Fuseki's datasets have plain names; anything else could reach other endpoints, such as the unauthenticated admin
# protocol's under "/$/".
_DATASET = re.compile(r"[A-Za-z0-9_-]+")


class SparqlError(Exception):
    """A SPARQL query failed: Fuseki refused it or failed, the connection broke, or the result is malformed."""


class SparqlTimeoutError(SparqlError):
    """A SPARQL query didn't finish in time."""


@dataclass(frozen=True, slots=True)
class SelectResult:
    """A SELECT query's whole result.

    Attributes:
        variables: The result's variables, without '?', in column order.
        rows: One tuple per solution, in column order; None for an unbound variable.
    """

    variables: tuple[str, ...]
    rows: list[tuple[Term | None, ...]]


class SelectStream:
    """A SELECT result read as it arrives: iterate it for the rows, each a tuple in column order.

    Attributes:
        variables: The result's variables, without '?', in column order.
    """

    def __init__(self, variables: tuple[str, ...], batches: AsyncIterator[list[str]]) -> None:
        """Wrap the batches of lines after the header line."""
        self.variables = variables
        self._batches = batches

    async def __aiter__(self) -> AsyncIterator[tuple[Term | None, ...]]:
        """Yield each row, parsed.

        Raises:
            SparqlError: The rest of the result fails or doesn't parse.
        """
        width = len(self.variables)
        async for batch in self._batches:
            for line in batch:
                try:
                    row = parse_tsv_row(line, width)
                except RDFSyntaxError as error:
                    raise SparqlError(f"malformed SELECT result: {error}") from error
                yield row


class FusekiClient:
    """Sends SPARQL queries to Fuseki's datasets, over the app's HTTP client for Fuseki."""

    def __init__(self, http: httpx2.AsyncClient) -> None:
        """Use an HTTP client whose base URL is Fuseki's (`FUSEKI_URL`)."""
        self._http = http

    async def select(self, dataset: str, query: str, timeout_s: float = QUERY_TIMEOUT_S) -> SelectResult:
        """Run a SELECT query on a dataset, returning its whole result.

        Raises:
            SparqlError: The query failed, or its result is malformed.
            SparqlTimeoutError: The query took longer than `timeout_s`.
        """
        async with _time_limit(timeout_s), self.select_stream(dataset, query) as stream:
            return SelectResult(stream.variables, [row async for row in stream])

    async def construct(
        self, dataset: str, query: str, timeout_s: float = QUERY_TIMEOUT_S, max_bytes: int = CONSTRUCT_MAX_BYTES
    ) -> bytes:
        """Run a DESCRIBE or CONSTRUCT query on a dataset, returning its result as checked N-Triples.

        Raises:
            SparqlError: The query failed, its result isn't N-Triples, or it's larger than `max_bytes`.
            SparqlTimeoutError: The query took longer than `timeout_s`.
        """
        lines = []
        size = 0
        async with _time_limit(timeout_s), self.construct_stream(dataset, query) as stream:
            async for line in stream:
                encoded = line.encode()
                size += len(encoded) + 1
                if size > max_bytes:
                    raise SparqlError(f"the result is larger than {max_bytes} bytes")
                lines.append(encoded)
        lines.append(b"")  # for the last line's end
        return b"\n".join(lines)

    @asynccontextmanager
    async def select_stream(self, dataset: str, query: str) -> AsyncIterator[SelectStream]:
        """Run a SELECT query on a dataset, reading its result as it arrives, for results too large to hold.

        Each wait for data has a time limit (`STREAM_READ_TIMEOUT_S`), but the whole query has none.

        Raises:
            SparqlError: The query failed, or its result is malformed (also while iterating).
            SparqlTimeoutError: Fuseki sent no data for `STREAM_READ_TIMEOUT_S` (also while iterating).
        """
        async with self._query(dataset, query, TSV) as batches:
            first = await anext(batches, None)
            if first is None:
                raise SparqlError("empty SELECT result: no header line")
            header, *rows = first
            try:
                variables = parse_tsv_header(header)
            except RDFSyntaxError as error:
                raise SparqlError(f"malformed SELECT result: {error}") from error
            yield SelectStream(variables, _prepend(rows, batches))

    @asynccontextmanager
    async def construct_stream(self, dataset: str, query: str) -> AsyncIterator[AsyncIterator[str]]:
        """Run a DESCRIBE or CONSTRUCT query on a dataset, yielding its N-Triples lines, each checked, as they arrive.

        Each wait for data has a time limit (`STREAM_READ_TIMEOUT_S`), but the whole query has none.

        Raises:
            SparqlError: The query failed, or its result isn't N-Triples (also while iterating).
            SparqlTimeoutError: Fuseki sent no data for `STREAM_READ_TIMEOUT_S` (also while iterating).
        """

        async def checked(batches: AsyncIterator[list[str]]) -> AsyncIterator[str]:
            async for batch in batches:
                for line in batch:
                    try:
                        check_ntriples_triple(line)
                    except RDFSyntaxError as error:
                        raise SparqlError(f"malformed N-Triples result: {error}") from error
                    yield line

        async with self._query(dataset, query, NTRIPLES) as batches:
            yield checked(batches)

    @asynccontextmanager
    async def _query(self, dataset: str, query: str, accept: str) -> AsyncIterator[AsyncIterator[list[str]]]:
        """Send a query and yield the response's lines in batches, once its status and content type are checked.

        Raises:
            ValueError: The dataset's name isn't letters, digits, '_' and '-'.
        """
        if not _DATASET.fullmatch(dataset):
            raise ValueError(f"not a dataset name: {dataset!r}")
        request = self._http.build_request(
            "POST", f"/{dataset}/sparql", data={"query": query}, headers={"Accept": accept}, timeout=_HTTP_TIMEOUT
        )
        try:
            response = await self._http.send(request, stream=True)
        except httpx2.TimeoutException as error:
            raise SparqlTimeoutError(f"Fuseki didn't answer in time: {error!r}") from error
        except httpx2.HTTPError as error:
            raise SparqlError(f"couldn't reach Fuseki: {error!r}") from error
        try:
            if not response.is_success:
                raise SparqlError(f"Fuseki answered {response.status_code}: {await _error_text(response)}")
            content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
            if content_type != accept:
                raise SparqlError(f"Fuseki sent {content_type or 'no content type'}, not {accept}")
            yield _batches(response)
        finally:
            await response.aclose()


@asynccontextmanager
async def _time_limit(timeout_s: float) -> AsyncIterator[None]:
    """Limit the whole of a block's time, not each phase of an HTTP call as httpx2's timeouts do."""
    try:
        async with asyncio.timeout(timeout_s):
            yield
    except TimeoutError as error:
        raise SparqlTimeoutError(f"query took longer than {timeout_s} s") from error


async def _batches(response: httpx2.Response) -> AsyncIterator[list[str]]:
    """Yield the body's lines as they arrive, decoded as UTF-8, without their line ends: each chunk's complete lines.

    Lines end only at LF (or CRLF): unlike `aiter_lines`, no other character counts as a line end, so a literal with a
    raw U+2028 stays on its line. Yielding a chunk's lines together, never an empty list, halves the async generator
    steps per line, which bulk results of millions of lines notice.

    Raises:
        SparqlError: The body ends early, the connection breaks, the body isn't UTF-8, or a line is longer than
            `MAX_LINE_CHARS`.
        SparqlTimeoutError: Fuseki sent no data for the read time limit.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    unfinished: list[str] = []  # the pieces of the line that the chunks so far end in, joined once it ends
    unfinished_chars = 0
    try:
        async for chunk in response.aiter_bytes():
            *lines, rest = decoder.decode(chunk).split("\n")
            if lines:
                unfinished.append(lines[0])
                lines[0] = "".join(unfinished)
                unfinished = []
                unfinished_chars = 0
            if lines:
                if max(map(len, lines)) > MAX_LINE_CHARS:
                    raise _line_too_long()
                yield [line.removesuffix("\r") for line in lines]
            unfinished.append(rest)
            unfinished_chars += len(rest)
            if unfinished_chars > MAX_LINE_CHARS:
                raise _line_too_long()
        unfinished.append(decoder.decode(b"", final=True))
    except httpx2.TimeoutException as error:
        raise SparqlTimeoutError(f"Fuseki stopped sending the result: {error!r}") from error
    except httpx2.HTTPError as error:  # e.g. the body ended before its last chunk: Fuseki exited or restarted
        raise SparqlError(f"the result broke off: {error!r}") from error
    except UnicodeDecodeError as error:
        raise SparqlError(f"the result isn't UTF-8: {error}") from error
    if last := "".join(unfinished):
        yield [last.removesuffix("\r")]


async def _prepend(batch: list[str], batches: AsyncIterator[list[str]]) -> AsyncIterator[list[str]]:
    """Yield a batch, then the rest."""
    yield batch
    async for rest in batches:
        yield rest


def _line_too_long() -> SparqlError:
    return SparqlError(f"the result has a line longer than {MAX_LINE_CHARS} characters")


async def _error_text(response: httpx2.Response) -> str:
    """Return the start of an error response's body, or a note that it couldn't be read."""
    try:
        body = await response.aread()
    except httpx2.HTTPError as error:
        return f"(error body not read: {error!r})"
    return body.decode("utf-8", errors="replace")[:_ERROR_TEXT_LIMIT].strip()
